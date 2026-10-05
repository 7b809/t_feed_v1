"""
Session scheduler for the EMA app.

Timeline (IST, Mon–Fri):

    09:14  → arm: register instruments + subscribe to streamer
    09:15  → session start: backfill EMA state + opening range + all
                     intraday crosses so far
    09:15 to 15:30 → every minute, finalize the previous minute's bar at
                     minute + finalize_delay_sec
    15:30  → session end: finalize the last bar, log summary

Restart-safe: if the app is started mid-session, the startup backfill
reconstructs the opening-range levels and re-detects every intraday cross
that already happened today.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.logger import get_logger
from ema_app.config import ema_config
from ema_app.service import ema_service

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> datetime:
    return datetime.now(IST)


def _today_at(hhmm: str) -> datetime:
    hh, mm = hhmm.split(":")
    now = _now_ist()
    return now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)


def _seconds_until(target: datetime) -> float:
    return max(0.0, (target - _now_ist()).total_seconds())


def _short(exc: BaseException) -> str:
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if len(msg) > 160:
        msg = msg[:160] + "…"
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


# >>> CHANGE #6: configurable subscribe mode for the EMA engine.
# EMA needs OHLC to aggregate minute bars, so the default is "full".
# Operators can override via EMA_APP_SUBSCRIBE_MODE.
def _ema_subscribe_mode() -> str:
    return str(getattr(ema_config, "subscribe_mode", None) or "full")


# ---------------------------------------------------------------------------
# Finalize loop
# ---------------------------------------------------------------------------
async def _finalize_loop(stop_event: asyncio.Event) -> None:
    delay = ema_config.finalize_delay_sec

    while not stop_event.is_set():
        try:
            ema_service.finalize_current_minute()

            now = _now_ist()
            next_minute = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
            target = next_minute + timedelta(seconds=delay)
            wait = max(1.0, (target - _now_ist()).total_seconds())

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
        except Exception as exc:
            logger.exception("EMA finalize loop error: %s", exc)
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Session loop
# ---------------------------------------------------------------------------
async def _session_loop(stop_event: asyncio.Event) -> None:
    if not ema_config.enabled:
        logger.info("EMA app scheduler disabled by config")
        return

    while not stop_event.is_set():
        now = _now_ist()

        # Skip weekends
        if now.weekday() >= 5:
            await _sleep_short(stop_event, 1800)
            continue

        arm_time = _today_at(ema_config.market_open) - timedelta(
            minutes=ema_config.arm_offset_min
        )
        open_time = _today_at(ema_config.market_open)
        close_time = _today_at(ema_config.market_close)

        # ---- Before arm time: sleep until arm ---------------------------
        if now < arm_time:
            wait = _seconds_until(arm_time)
            logger.info("EMA app: sleeping %.0fs until arm time", wait)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait)
                continue
            except asyncio.TimeoutError:
                pass

        # ---- Arm --------------------------------------------------------
        logger.info("EMA app: arming session")
        try:
            await _arm_session()
        except Exception as exc:
            logger.exception("EMA arm failed: %s", exc)

        # ---- Backfill (restart-safe, opening range + all crosses so far) -
        if ema_config.backfill_on_start:
            try:
                await _backfill_all()
            except Exception as exc:
                logger.exception("EMA backfill failed: %s", exc)

        logger.info("EMA app: session running until 15:30 IST")

        # ---- Finalize loop until close ---------------------------------
        try:
            await asyncio.wait_for(
                _finalize_loop(stop_event),
                timeout=max(1.0, _seconds_until(close_time)),
            )
        except asyncio.TimeoutError:
            logger.info("EMA app: session ended (15:30 IST)")

        try:
            ema_service.finalize_current_minute()
        except Exception as exc:
            logger.warning("EMA final finalize failed: %s", exc)

        await _sleep_short(stop_event, 60)


async def _sleep_short(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


# ---------------------------------------------------------------------------
# Arm + backfill
# ---------------------------------------------------------------------------


# >>> CHANGE #1, #2, #4: rewritten _arm_session.
#
# What was wrong before:
#   from upstox_app.streamer.streamer_manager import streamer_manager
# `upstox_app/streamer/streamer_manager.py` exports module-level functions
# (auto_connect_enabled, subscribe_enabled_indexes, ...), NOT a symbol
# called `streamer_manager`. So the import raised ImportError every arm,
# logged as "EMA streamer unavailable", and left subscribed=0.
#
# The correct call path per README is:
#   market_streamer.subscribe(keys, mode)
#
# We also try `subscribe_instruments` on the market_streamer instance as a
# fallback for older wrapper versions, and log the outcome.
async def _arm_session() -> None:
    instruments = _load_instruments()
    ema_service.register_instruments(instruments)
    ema_service.load_persisted_crosses_for_today()
    ema_service.start()

    keys = ema_service.instrument_keys()
    if not keys:
        logger.info("EMA app: armed | instruments=0 | nothing to subscribe")
        return

    mode = _ema_subscribe_mode()
    subscribed = 0
    rejected = 0
    error: Optional[str] = None

    # ---- Preferred path: market_streamer.subscribe ---------------------
    try:
        from upstox_app.market.market_streamer import market_streamer  # type: ignore
    except Exception as exc:  # noqa: BLE001
        logger.warning("EMA arm: market_streamer import failed | %s", _short(exc))
        market_streamer = None  # type: ignore

    if market_streamer is not None:
        fn = getattr(market_streamer, "subscribe", None)
        if callable(fn):
            try:
                applied, skipped = fn(keys, mode)
                subscribed = len(applied or [])
                rejected = len(skipped or [])
            except Exception as exc:  # noqa: BLE001
                error = _short(exc)
                logger.warning(
                    "EMA arm: market_streamer.subscribe failed | %s", error
                )
        else:
            # Older wrappers used subscribe_instruments.
            alt = getattr(market_streamer, "subscribe_instruments", None)
            if callable(alt):
                try:
                    alt(keys, mode)
                    subscribed = len(keys)
                except Exception as exc:  # noqa: BLE001
                    error = _short(exc)
                    logger.warning(
                        "EMA arm: market_streamer.subscribe_instruments failed | %s",
                        error,
                    )
            else:
                error = "market_streamer has no subscribe()"
                logger.warning("EMA arm: %s", error)

    logger.info(
        "EMA app: armed | instruments=%d | mode=%s | subscribed=%d | rejected=%d",
        len(keys), mode, subscribed, rejected,
    )

    # >>> CHANGE #4: loud warning when nothing got subscribed. Without
    # ticks flowing, EMA detection will never fire and the operator needs
    # to know why.
    if subscribed == 0:
        logger.warning(
            "EMA app: NO instruments subscribed upstream — no live ticks will "
            "reach the EMA engine. Check that the market streamer is connected "
            "and that market_streamer.subscribe(keys, %r) succeeded. error=%s",
            mode, error or "none",
        )


async def _backfill_all() -> None:
    """
    Backfill runs in a worker thread so it never blocks the event loop.
    Uses a cooldown so startup + arm don't hit Upstox twice in quick
    succession.
    """
    keys = ema_service.instrument_keys()
    if not keys:
        return

    logger.info("EMA app: backfilling %d instruments", len(keys))
    done = await asyncio.to_thread(ema_service.backfill_all, False)
    logger.info("EMA app: backfill done | instruments=%d", done)


# ---------------------------------------------------------------------------
# Instrument universe
# ---------------------------------------------------------------------------


# >>> CHANGE #3, #5: rewritten _load_instruments.
#
# Previously called option_service.get_contracts(idx) — the older API.
# README latent issue #1 flagged the API drift. We now try the newer
# `get_contracts_for_index(idx)` first and fall back to `get_contracts`.
# The runtime-snapshot fallback now handles both shapes of the file.
def _load_instruments() -> list:
    # 1) Web helper (merges every enabled index)
    try:
        from web.service import load_all_option_instruments  # type: ignore

        instruments = load_all_option_instruments()
        if instruments:
            return instruments
    except Exception:
        pass

    # 2) Option service — try new API, then old.
    out: list = []
    try:
        from upstox_app.common.config import MAIN_INDEXES  # type: ignore
        from upstox_app.option.option_service import option_service  # type: ignore

        indexes = (
            list(MAIN_INDEXES.keys())
            if isinstance(MAIN_INDEXES, dict)
            else list(MAIN_INDEXES or [])
        )
        for idx in indexes:
            idx_name = str(idx).upper()

            contracts = None

            # Preferred: get_contracts_for_index
            getter = getattr(option_service, "get_contracts_for_index", None)
            if callable(getter):
                try:
                    contracts = getter(idx_name)
                except Exception as exc:
                    logger.debug(
                        "EMA _load_instruments: get_contracts_for_index(%s) failed: %s",
                        idx_name, _short(exc),
                    )

            # Fallback: get_contracts
            if not contracts:
                getter = getattr(option_service, "get_contracts", None)
                if callable(getter):
                    try:
                        contracts = getter(idx_name)
                    except Exception as exc:
                        logger.debug(
                            "EMA _load_instruments: get_contracts(%s) failed: %s",
                            idx_name, _short(exc),
                        )

            if contracts:
                out.extend(contracts)
    except Exception:
        pass

    if out:
        return out

    # 3) Runtime snapshots
    import json
    from pathlib import Path

    runtime = (
        Path(__file__).resolve().parent.parent / "data" / "runtime" / "options"
    )
    if runtime.exists():
        for f in runtime.glob("*.json"):
            # >>> CHANGE #5: skip metadata explicitly, not just by "_" prefix.
            if f.name.startswith("_") or f.name == "_meta.json":
                continue
            try:
                with f.open("r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except Exception:
                continue

            # Support both {"contracts": [...]} and a bare list.
            raw = payload.get("contracts") if isinstance(payload, dict) else payload
            if isinstance(raw, list):
                out.extend([c for c in raw if isinstance(c, dict)])

    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
_scheduler_task: Optional[asyncio.Task] = None
_scheduler_stop: Optional[asyncio.Event] = None


async def start_scheduler() -> None:
    global _scheduler_task, _scheduler_stop
    if _scheduler_task is not None and not _scheduler_task.done():
        return
    _scheduler_stop = asyncio.Event()
    _scheduler_task = asyncio.create_task(_session_loop(_scheduler_stop))
    logger.info("EMA app scheduler started")


async def stop_scheduler() -> None:
    global _scheduler_task, _scheduler_stop
    if _scheduler_stop is not None:
        _scheduler_stop.set()
    if _scheduler_task is not None:
        try:
            await asyncio.wait_for(_scheduler_task, timeout=5)
        except Exception:
            pass
    _scheduler_task = None
    _scheduler_stop = None
    logger.info("EMA app scheduler stopped")