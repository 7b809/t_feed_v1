# app/services/ema_feed_service.py
#
# Job 2 orchestrator.
#
# ── Wall-clock aligned cycles ──────────────────────────────────────────
# Cycles are anchored to the wall clock at ``HH:MM:10`` (see
# ``CYCLE_OFFSET_SECONDS``). For the default EMA_POLL_SECONDS=60 this
# means:
#
#   candle 9:20→9:21 (closed at 9:21:00)  → processed at 9:21:10
#   candle 9:21→9:22 (closed at 9:22:00)  → processed at 9:22:10
#   candle 9:22→9:23 (closed at 9:23:00)  → processed at 9:23:10
#
# Cycle 1 fires immediately on boot (so heartbeats flow pre-market);
# every subsequent cycle snaps to the next ``:10`` boundary. If a cycle
# overruns its slot, the next cycle simply picks up the following slot
# — there is no catch-up burst.
#
# For EMA_POLL_SECONDS that is a multiple of 60, alignment is on
# (minute % (poll_seconds//60)) == 0 + CYCLE_OFFSET_SECONDS. For other
# values the target falls back to second-level alignment.
#
# ── Prev / New EMA tracking ────────────────────────────────────────────
# At the start of each cycle, every instrument's state is snapshotted into
# ``runtime["prev_state"]``. The dashboard reads ``ema_fast_prev`` /
# ``ema_slow_prev`` / ``last_ts_prev`` alongside the live values plus
# ``last_ts`` so the "Last → New" pair is expressed in **candle time**,
# not cycle time.
#
# ── Today's cross count ────────────────────────────────────────────────
# ``snapshot()`` exposes ``cross_count_today`` — count of persisted
# crosses whose IST calendar date equals today's IST date.
#
# ── Cold-start state recovery ──────────────────────────────────────────
# When ``_ema_state/<key>.json`` is missing, ``_ensure_subscribed``
# derives state from the last persisted cross so indexes show real EMA
# values instead of ``—``.
#
# ── Telegram notifications ─────────────────────────────────────────────
# * ``ema bootstrap complete``  (after warm-up / cached-state load)
# * ``ema live loop started``   (once, when start_live() actually starts)

from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, TYPE_CHECKING

from app.config import Settings
from app.services.ema_calculator import detect_crosses, parse_ts, update_one_candle
from app.services.ema_state_store import EmaStateStore
from app.services.feed_broadcaster import FeedBroadcaster
from app.services.instrument_store import InstrumentStore, InstrumentStoreError
from app.services.upstox_client_service import UpstoxError, UpstoxService

if TYPE_CHECKING:  # pragma: no cover
    from app.services.telegram_service import ProjectNotifier

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = dtime(9, 15)
LIVE_START = dtime(9, 16, 10)
MARKET_CLOSE = dtime(15, 30)

# Wall-clock anchor: cycles fire at HH:MM:10 (10 seconds after the
# completed 1-minute candle closed at HH:MM:00). Adjust if Upstox ever
# changes candle finalisation latency.
CYCLE_OFFSET_SECONDS = 10

DEFAULT_INDEX_METADATA: dict[str, dict[str, Any]] = {
    "NSE_INDEX|Nifty 50": {
        "instrument_key": "NSE_INDEX|Nifty 50",
        "underlying": "NIFTY",
        "strike_price": None,
        "strike_type": "INDEX",
        "expiry": None,
        "trading_symbol": "NIFTY",
    },
    "BSE_INDEX|SENSEX": {
        "instrument_key": "BSE_INDEX|SENSEX",
        "underlying": "SENSEX",
        "strike_price": None,
        "strike_type": "INDEX",
        "expiry": None,
        "trading_symbol": "SENSEX",
    },
}

class EmaFeedError(RuntimeError):
    pass

def _now_ist() -> datetime:
    return datetime.now(tz=IST)

def _cross_is_today(ts_value: Any, today_ist_date) -> bool:
    """True when ``ts_value`` parses to a datetime whose IST date == today."""
    ts = parse_ts(ts_value)
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    try:
        return ts.astimezone(IST).date() == today_ist_date
    except (ValueError, OSError):
        return False

class EmaFeedService:
    def __init__(
        self,
        config: Settings,
        broadcaster: FeedBroadcaster,
        instruments: InstrumentStore,
        upstox: UpstoxService,
        state_store: EmaStateStore,
        notifier: "ProjectNotifier | None" = None,
    ) -> None:
        self.config = config
        self.broadcaster = broadcaster
        self.instruments = instruments
        self.upstox = upstox
        self.state_store = state_store
        self.notifier = notifier

        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(config.ema_max_workers)
        self._runtime: dict[str, dict[str, Any]] = {}
        self._subscribed: set[str] = set()
        self._live_task: asyncio.Task | None = None
        self._last_cycle_start: datetime | None = None
        self._cycle_number: int = 0

    # ------------------------------------------------------------------ #
    # Notifier helper
    # ------------------------------------------------------------------ #
    async def _notify(self, method: str, **kwargs) -> None:
        if self.notifier is None:
            return
        try:
            await asyncio.to_thread(getattr(self.notifier, method), **kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("Telegram %s notification failed", method)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def bootstrap(self) -> None:
        for key in self.config.index_instrument_keys:
            try:
                await self._ensure_subscribed(key, seed_from_file_only=True)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to bootstrap index %s", key)

        now = _now_ist()
        post_market_open = now.time() >= MARKET_OPEN
        if post_market_open:
            await self.warmup_all()
        else:
            logger.info(
                "Bootstrap before market open (%s IST); using persisted state. "
                "Cycles start immediately, then align to HH:MM:%02ds. "
                "API polling begins at 09:16:10.",
                now.strftime("%H:%M:%S"), CYCLE_OFFSET_SECONDS,
            )

        await self._notify(
            "notify_ema_bootstrap",
            subscribed_keys=self.subscribed_keys(),
            post_market_open=post_market_open,
        )
        await self.start_live()

    async def start_live(self) -> None:
        if self._live_task and not self._live_task.done():
            return
        self._live_task = asyncio.create_task(self._live_loop(), name="ema-live-loop")
        logger.info(
            "EMA live loop started interval=%ss offset=:%02ds heartbeat_every=%s "
            "fetch_timeout=%ss cycle_log_every=%s",
            self.config.ema_poll_seconds,
            CYCLE_OFFSET_SECONDS,
            self.config.ema_heartbeat_cycles,
            self.config.ema_fetch_timeout_seconds,
            self.config.ema_cycle_log_every,
        )
        await self._notify(
            "notify_ema_live_started",
            poll_seconds=self.config.ema_poll_seconds,
        )

    async def shutdown(self) -> None:
        if self._live_task and not self._live_task.done():
            self._live_task.cancel()
            try:
                await self._live_task
            except asyncio.CancelledError:
                pass
        self._live_task = None

    # ------------------------------------------------------------------ #
    # Subscription
    # ------------------------------------------------------------------ #
    async def subscribe(self, instrument_key: str) -> dict[str, Any]:
        async with self._lock:
            if instrument_key in self._subscribed:
                return {"instrument_key": instrument_key, "status": "already_subscribed"}
        meta = self._resolve_meta(instrument_key)
        await self._ensure_subscribed(instrument_key, seed_from_file_only=False, meta=meta)
        return {"instrument_key": instrument_key, "status": "subscribed"}

    def _resolve_meta(self, instrument_key: str) -> dict[str, Any]:
        meta = self.instruments.get(instrument_key)
        if meta is not None:
            return meta

        synthetic = DEFAULT_INDEX_METADATA.get(instrument_key)
        if synthetic is None:
            raise EmaFeedError(
                f"Instrument {instrument_key} is not present in all_instruments.json"
            )

        logger.info(
            "Seeding known index metadata for %s (catalog file has no entry; "
            "this is normal on a cold install before Job 1 extraction)",
            instrument_key,
        )
        try:
            return self.instruments.upsert(instrument_key, synthetic)
        except InstrumentStoreError as exc:
            logger.warning(
                "Could not persist seeded index %s to catalog (%s); using in-memory record",
                instrument_key,
                exc,
            )
            return dict(synthetic)

    async def _ensure_subscribed(
        self,
        instrument_key: str,
        *,
        seed_from_file_only: bool,
        meta: dict[str, Any] | None = None,
    ) -> None:
        if meta is None:
            meta = self._resolve_meta(instrument_key)

        persisted_state = self.state_store.load_state(instrument_key)
        persisted_crosses = self.state_store.load_crosses(instrument_key, meta)

        # Cold-start fallback: derive state from the last persisted cross.
        if not persisted_state:
            last_crosses = (persisted_crosses or {}).get("crosses") or []
            if last_crosses:
                last = last_crosses[-1]
                persisted_state = {
                    "ema_fast": last.get("fast_ema"),
                    "ema_slow": last.get("slow_ema"),
                    "last_ts": last.get("timestamp"),
                    "last_close": last.get("close"),
                }
                logger.info(
                    "Derived %s state from last persisted cross (ts=%s)",
                    instrument_key, last.get("timestamp"),
                )

        runtime = {
            "instrument_key": instrument_key,
            "meta": meta,
            "state": persisted_state or {},
            "prev_state": None,
            "crosses": list((persisted_crosses or {}).get("crosses", [])),
            "candle_count": int((persisted_crosses or {}).get("candle_count", 0)),
            "channels": self._channels_for(instrument_key, meta),
            "last_payload": None,
        }
        async with self._lock:
            self._runtime[instrument_key] = runtime
            self._subscribed.add(instrument_key)
        logger.info(
            "Subscribed %s (state=%s crosses=%d)",
            instrument_key,
            "warm" if persisted_state else "cold",
            len(runtime["crosses"]),
        )
        if not seed_from_file_only and not persisted_state:
            await self._warm_up_instrument(instrument_key)

    # ------------------------------------------------------------------ #
    # Warm-up
    # ------------------------------------------------------------------ #
    async def warmup_all(self) -> None:
        keys = list(self._subscribed)
        await self._run_parallel([self._warm_up_instrument(k) for k in keys])

    async def _warm_up_instrument(self, instrument_key: str) -> None:
        runtime = self._runtime.get(instrument_key)
        if runtime is None:
            return
        meta = runtime["meta"]
        try:
            async with self._semaphore:
                historical = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.upstox.historical_window,
                        instrument_key,
                        self.config.ema_history_days,
                    ),
                    timeout=self.config.ema_fetch_timeout_seconds * 4,
                )
                intraday = await asyncio.wait_for(
                    asyncio.to_thread(self.upstox.intraday_1m, instrument_key),
                    timeout=self.config.ema_fetch_timeout_seconds,
                )
        except asyncio.TimeoutError:
            logger.warning(
                "Warm-up fetch timed out for %s — will retry on next cycle",
                instrument_key,
            )
            return
        except UpstoxError as exc:
            logger.warning("Warm-up fetch failed for %s: %s", instrument_key, exc)
            return

        merged = self._merge_candles(historical, intraday)
        if not merged:
            logger.info("No candles available for %s during warm-up", instrument_key)
            return

        crosses, final_state = detect_crosses(
            merged, self.config.ema_fast_period, self.config.ema_slow_period
        )
        runtime["state"] = {
            "ema_fast": final_state.get("ema_fast"),
            "ema_slow": final_state.get("ema_slow"),
            "last_ts": final_state.get("last_ts"),
            "last_close": final_state.get("last_close"),
        }
        runtime["crosses"] = crosses[-self.config.ema_candle_count :]
        runtime["candle_count"] = len(merged)
        self.state_store.save_state(instrument_key, runtime["state"])
        self.state_store.save_crosses(
            instrument_key, meta, runtime["candle_count"], runtime["crosses"]
        )
        logger.info(
            "Warm-up complete %s candles=%d crosses=%d",
            instrument_key, len(merged), len(crosses),
        )

    # ------------------------------------------------------------------ #
    # Wall-clock aligned cycle target
    # ------------------------------------------------------------------ #
    def _next_cycle_target(self, now: datetime) -> datetime:
        """
        Return the next cycle start time.

        * First cycle (``_last_cycle_start is None``) fires **now** so
          heartbeats start on boot.
        * Subsequent cycles snap to ``HH:MM:10`` (for the default
          60s interval). For poll intervals that are a multiple of 60
          seconds the offset is applied to the aligned minute.
        * Cycles never fire closer than ``interval / 2`` seconds apart
          (prevents the "immediate cycle 1 at 10:01:08 → cycle 2 at
          10:01:10" duplicate when startup lands mid-slot).
        """
        if self._last_cycle_start is None:
            return now

        interval = max(1, self.config.ema_poll_seconds)
        min_gap = max(5, interval // 2)
        gap_since_last = (now - self._last_cycle_start).total_seconds()

        if interval >= 60 and interval % 60 == 0:
            minutes = interval // 60
            base = now.replace(second=0, microsecond=0)
            m = base.minute
            slot_minute = (m // minutes) * minutes
            slot = base.replace(minute=slot_minute)

            next_slot = slot + timedelta(
                minutes=minutes, seconds=CYCLE_OFFSET_SECONDS
            )
            target_this_slot = slot + timedelta(seconds=CYCLE_OFFSET_SECONDS)

            # Just fired → skip this slot's boundary, wait for next.
            if gap_since_last < min_gap:
                return next_slot

            # Before this slot's offset → fire on this slot's boundary.
            if now < target_this_slot:
                return target_this_slot

            # Past this slot's boundary → next slot.
            return next_slot

        # Sub-minute intervals: align to second-level boundary + offset.
        epoch = now.timestamp()
        last_epoch = self._last_cycle_start.timestamp()
        periods = math.ceil(
            (last_epoch + min_gap - CYCLE_OFFSET_SECONDS) / interval
        )
        next_epoch = periods * interval + CYCLE_OFFSET_SECONDS
        return datetime.fromtimestamp(next_epoch, tz=now.tzinfo)

    # ------------------------------------------------------------------ #
    # Live loop — wall-clock aligned
    # ------------------------------------------------------------------ #
    async def _live_loop(self) -> None:
        try:
            while True:
                now = _now_ist()
                target = self._next_cycle_target(now)
                wait = (target - now).total_seconds()
                if wait > 0.05:
                    logger.debug(
                        "Sleeping %.2fs until cycle target %s (current %s)",
                        wait,
                        target.strftime("%H:%M:%S"),
                        now.strftime("%H:%M:%S"),
                    )
                    await asyncio.sleep(wait)
                try:
                    await self._run_cycle()
                except Exception:  # noqa: BLE001
                    logger.exception("Live cycle failed")
        except asyncio.CancelledError:
            logger.info("EMA live loop cancelled")
            raise

    async def _run_cycle(self) -> None:
        cycle_start = _now_ist()
        prev_cycle_start = self._last_cycle_start
        self._last_cycle_start = cycle_start

        self._cycle_number += 1
        cycle_num = self._cycle_number
        keys = list(self._subscribed)
        if not keys:
            logger.debug("Cycle %d: no instruments subscribed — skipping", cycle_num)
            return

        # Snapshot prev state BEFORE polling.
        for key in keys:
            rt = self._runtime.get(key)
            if rt is None:
                continue
            st = rt["state"]
            if st.get("ema_fast") is None:
                rt["prev_state"] = None
            else:
                rt["prev_state"] = {
                    "ema_fast": st.get("ema_fast"),
                    "ema_slow": st.get("ema_slow"),
                    "last_ts": st.get("last_ts"),
                    "last_close": st.get("last_close"),
                    "snapshot_time": (
                        prev_cycle_start.isoformat() if prev_cycle_start else None
                    ),
                }

        cycle_timeout = max(5.0, self.config.ema_poll_seconds - 5.0)
        coros = [self._poll_instrument(k) for k in keys]
        timed_out = False
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*coros, return_exceptions=True),
                timeout=cycle_timeout,
            )
        except asyncio.TimeoutError:
            timed_out = True
            logger.warning(
                "Cycle %d exceeded %.1fs — broadcasting partial results",
                cycle_num, cycle_timeout,
            )
            results = [None] * len(keys)

        cycle_end = _now_ist()
        duration = (cycle_end - cycle_start).total_seconds()

        ok_count = 0
        failed_count = 0
        summaries: list[tuple[str, dict[str, Any]]] = []

        for i, key in enumerate(keys):
            runtime = self._runtime.get(key)
            if runtime is None:
                continue

            result = results[i] if not timed_out and i < len(results) else None

            if timed_out:
                status = "timeout"
                error = f"cycle exceeded {cycle_timeout:.0f}s"
                failed_count += 1
            elif isinstance(result, asyncio.CancelledError):
                status = "timeout"
                error = "cancelled"
                failed_count += 1
            elif isinstance(result, BaseException):
                status = "failed"
                error = str(result)[:200] or type(result).__name__
                failed_count += 1
            else:
                status = "ok"
                error = None
                ok_count += 1

            if isinstance(result, dict):
                for payload in result.get("emissions", []):
                    await self.broadcaster.broadcast(payload, runtime["channels"])

            state = runtime["state"]
            last_cross = runtime["crosses"][-1] if runtime["crosses"] else None
            summary = {
                "event": "heartbeat",
                "cycle": cycle_num,
                "cycle_started_at": cycle_start.isoformat(),
                "cycle_completed_at": cycle_end.isoformat(),
                "duration_seconds": round(duration, 3),
                "instrument_key": key,
                "trading_symbol": runtime["meta"].get("trading_symbol"),
                "status": status,
                "error": error,
                "ema9": _round_or_none(state.get("ema_fast")),
                "ema21": _round_or_none(state.get("ema_slow")),
                "last_close": _round_or_none(state.get("last_close")),
                "last_ts": state.get("last_ts"),
                "last_cross_time": last_cross.get("timestamp") if last_cross else None,
            }
            summaries.append((key, summary))

        heartbeat_every = self.config.ema_heartbeat_cycles
        emit_heartbeat = heartbeat_every > 0 and (cycle_num % heartbeat_every == 0)

        if emit_heartbeat:
            for key, summary in summaries:
                runtime = self._runtime.get(key)
                if runtime is None:
                    continue
                runtime["last_payload"] = summary
                await self.broadcaster.broadcast(summary, runtime["channels"])

        logger.info(
            "Cycle %d complete at %s duration=%.2fs instruments=%d ok=%d failed=%d heartbeat=%s",
            cycle_num, cycle_start.strftime("%H:%M:%S"),
            duration, len(keys), ok_count, failed_count,
            "yes" if emit_heartbeat else "no",
            extra={"cycle_complete": True, "cycle_number": cycle_num},
        )

    async def _poll_instrument(self, instrument_key: str) -> dict[str, Any]:
        runtime = self._runtime.get(instrument_key)
        if runtime is None:
            return {"emissions": [], "status": "skipped", "error": None}

        emissions: list[dict[str, Any]] = []

        now = _now_ist()
        if now.time() < LIVE_START:
            emissions.append(self._build_no_cross_payload(instrument_key, runtime))
            return {"emissions": emissions, "status": "ok", "error": None}

        try:
            async with self._semaphore:
                intraday = await asyncio.wait_for(
                    asyncio.to_thread(self.upstox.intraday_1m, instrument_key),
                    timeout=self.config.ema_fetch_timeout_seconds,
                )
        except asyncio.TimeoutError:
            logger.warning(
                "Intraday fetch timed out for %s after %ss",
                instrument_key, self.config.ema_fetch_timeout_seconds,
            )
            emissions.append(self._build_no_cross_payload(instrument_key, runtime))
            raise
        except UpstoxError:
            emissions.append(self._build_no_cross_payload(instrument_key, runtime))
            raise

        state = runtime["state"]
        last_ts = parse_ts(state.get("last_ts")) if state.get("last_ts") else None

        new_candles: list[dict] = []
        for candle in intraday:
            ts = parse_ts(candle.get("timestamp"))
            if ts is None:
                continue
            if last_ts is None or ts > last_ts:
                new_candles.append(candle)

        if not new_candles:
            emissions.append(self._build_no_cross_payload(instrument_key, runtime))
            return {"emissions": emissions, "status": "ok", "error": None}

        for candle in new_candles:
            cross = update_one_candle(
                state, candle, self.config.ema_fast_period, self.config.ema_slow_period
            )
            runtime["candle_count"] += 1
            if cross:
                runtime["crosses"].append(cross)
                if len(runtime["crosses"]) > self.config.ema_candle_count:
                    runtime["crosses"] = runtime["crosses"][-self.config.ema_candle_count :]
                emissions.append(self._build_cross_payload(instrument_key, runtime, cross))
            else:
                emissions.append(self._build_no_cross_payload(instrument_key, runtime))

        self.state_store.save_state(instrument_key, state)
        self.state_store.save_crosses(
            instrument_key, runtime["meta"], runtime["candle_count"], runtime["crosses"]
        )
        return {"emissions": emissions, "status": "ok", "error": None}

    # ------------------------------------------------------------------ #
    # Payload builders
    # ------------------------------------------------------------------ #
    def _build_cross_payload(
        self, instrument_key: str, runtime: dict, cross: dict
    ) -> dict[str, Any]:
        state = runtime["state"]
        return {
            "event": "cross",
            "instrument_key": instrument_key,
            "trading_symbol": runtime["meta"].get("trading_symbol"),
            "close": _round_or_none(state.get("last_close")),
            "ema9": _round_or_none(state.get("ema_fast")),
            "ema21": _round_or_none(state.get("ema_slow")),
            "last_cross_time": cross.get("timestamp"),
        }

    def _build_no_cross_payload(self, instrument_key: str, runtime: dict) -> dict[str, Any]:
        last_cross_time = None
        if runtime["crosses"]:
            last_cross_time = runtime["crosses"][-1].get("timestamp")
        return {
            "event": "no_cross",
            "instrument_key": instrument_key,
            "trading_symbol": runtime["meta"].get("trading_symbol"),
            "last_cross_time": last_cross_time,
        }

    # ------------------------------------------------------------------ #
    # Snapshot for REST / WS / dashboard
    # ------------------------------------------------------------------ #
    def snapshot(self, instrument_key: str) -> dict[str, Any] | None:
        runtime = self._runtime.get(instrument_key)
        if runtime is None:
            return None
        state = runtime["state"]
        prev = runtime.get("prev_state") or {}
        last_cross = runtime["crosses"][-1] if runtime["crosses"] else None

        today_ist_date = _now_ist().date()
        crosses_today = sum(
            1 for c in runtime["crosses"]
            if _cross_is_today(c.get("timestamp"), today_ist_date)
        )

        return {
            "instrument_key": instrument_key,
            "meta": runtime["meta"],
            "ema_fast": state.get("ema_fast"),
            "ema_slow": state.get("ema_slow"),
            "last_ts": state.get("last_ts"),
            "last_close": state.get("last_close"),
            "ema_fast_prev": prev.get("ema_fast"),
            "ema_slow_prev": prev.get("ema_slow"),
            "last_ts_prev": prev.get("last_ts"),
            "last_close_prev": prev.get("last_close"),
            "prev_snapshot_time": prev.get("snapshot_time"),
            "cycle_time": (
                self._last_cycle_start.isoformat()
                if self._last_cycle_start else None
            ),
            "cycle_number": self._cycle_number,
            "candle_count": runtime["candle_count"],
            "cross_count": len(runtime["crosses"]),
            "cross_count_today": crosses_today,
            "today_ist_date": today_ist_date.isoformat(),
            "last_cross": last_cross,
            "last_cross_time": last_cross.get("timestamp") if last_cross else None,
            "last_payload": runtime["last_payload"],
        }

    def subscribed_keys(self) -> list[str]:
        return sorted(self._subscribed)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _channels_for(self, instrument_key: str, meta: dict[str, Any]) -> list[str]:
        channels = [f"instrument:{instrument_key}"]
        underlying = (meta.get("underlying") or "").lower()
        strike_type = (meta.get("strike_type") or "").upper()
        if strike_type == "INDEX" or not strike_type:
            if underlying:
                channels.append(f"index:{underlying}")
            return channels
        strike = meta.get("strike_price")
        if strike is not None and underlying:
            strike_str = str(int(strike)) if float(strike).is_integer() else str(strike)
            channels.append(f"options:{underlying}:{strike_str}{strike_type}")
        return channels

    @staticmethod
    def _merge_candles(*collections: list[dict]) -> list[dict]:
        merged: dict[str, dict] = {}
        for collection in collections:
            for candle in collection:
                merged[str(candle.get("timestamp"))] = candle
        return sorted(merged.values(), key=lambda c: str(c.get("timestamp")))

    async def _run_parallel(self, coros: list) -> None:
        if not coros:
            return
        await asyncio.gather(*coros, return_exceptions=True)

# ---------------------------------------------------------------------------
def _round_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None

# ---------------------------------------------------------------------------
_OPTIONS_RE = re.compile(r"^(\d+(?:\.\d+)?)(CE|PE)$", re.IGNORECASE)

def parse_options_channel(underlying: str, contract: str) -> tuple[str, str]:
    """'21000CE' or '21000_CE' -> ('21000', 'CE')."""
    cleaned = contract.replace("_", "").upper()
    match = _OPTIONS_RE.match(cleaned)
    if not match:
        raise EmaFeedError(f"Invalid options contract '{contract}'")
    strike = match.group(1)
    strike_type = match.group(2).upper()
    return strike, strike_type