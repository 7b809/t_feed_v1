# services/sr_backfill_service.py
"""Intraday SR backfill — seeds the SR engine from today's 1-min candles.

Runs ONLY when ALL of the following are true:

  * SR_BACKFILL_ENABLED is True
  * LIVE_SR_ENABLED is True
  * Current time is in [SR_BACKFILL_START_HOUR:MINUTE,
                        SR_BACKFILL_END_HOUR:MINUTE] IST
                        (default 09:15 — 23:59)
  * Today is Mon–Fri (not a weekend)
  * Today is NOT a trading holiday (per Upstox holiday API)

This gate is enforced in ``should_run()`` which is consulted by:

  * ``services/live_feed_service.LiveFeedService._run_sr_backfill``
  * ``routes/live_sr_routes.trigger_backfill``
  * ``SRBackfillService.run`` (defensive — callers may already have
    checked, but ``run`` re-checks so a direct call cannot bypass
    the window)

Called from
-----------
* ``services.live_feed_service.LiveFeedService.start()`` — before
  spawning the WebSocket worker thread.
* ``POST /live-sr/backfill`` — manual trigger.

Telegram + log
--------------
A Telegram summary is sent on the ``startup`` / ``feed_start`` path
only. A log line at INFO level is always written (including the
``skipped`` case with the exact reason).

Safety
------
* Never raises for expected failures — per-instrument errors are logged
  and skipped.
* Uses ``SR_BACKFILL_MAX_WORKERS`` (default 4) to respect Upstox rate
  limits.
* Skips non-trading days entirely.
* If the intraday API returns no candles for an instrument, the
  instrument's existing state (if any) is preserved.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

_MARKET_TZ = ZoneInfo("Asia/Kolkata")


# --------------------------------------------------------------------------- #
# Small time helpers                                                          #
# --------------------------------------------------------------------------- #
def _now_market() -> datetime:
    return datetime.now(_MARKET_TZ)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_sdk():
    try:
        import upstox_client  # type: ignore
        return upstox_client
    except ImportError:
        return None


def _coerce_dict(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    to_dict = getattr(raw, "to_dict", None)
    if callable(to_dict):
        try:
            d = to_dict()
        except Exception:
            d = None
        if isinstance(d, dict):
            return d
    data_attr = getattr(raw, "data", None)
    if isinstance(data_attr, dict):
        return data_attr
    return None


def _fetch_intraday_candles(
    instrument_key: str,
) -> list[dict[str, Any]]:
    """Fetch today's intraday 1-min candles for one instrument.

    The intraday endpoint does not require an explicit Authorization
    header (matching the behaviour of ``first_candle_service``).
    """
    sdk = _load_sdk()
    if sdk is None:
        return []
    try:
        cfg = sdk.Configuration()
        api = sdk.HistoryV3Api(sdk.ApiClient(cfg))
        response = api.get_intra_day_candle_data(
            instrument_key, "minutes", "1",
        )
    except Exception as exc:
        logger.warning(
            "[sr-backfill] intraday fetch failed for %s: %s: %s",
            instrument_key, type(exc).__name__, exc,
        )
        return []

    body = _coerce_dict(response)
    if body is None:
        return []
    raw_candles = (body.get("data") or {}).get("candles") or []
    if not isinstance(raw_candles, list):
        return []

    out: list[dict[str, Any]] = []
    for raw in raw_candles:
        if not isinstance(raw, list) or len(raw) < 5:
            continue
        try:
            out.append({
                "time": str(raw[0]),
                "open": float(raw[1]),
                "high": float(raw[2]),
                "low": float(raw[3]),
                "close": float(raw[4]),
                "volume": float(raw[5]) if len(raw) > 5 else 0.0,
            })
        except (ValueError, TypeError, IndexError):
            continue
    return out


# --------------------------------------------------------------------------- #
# Gate helpers                                                                #
# --------------------------------------------------------------------------- #
def _is_trading_day() -> tuple[bool, str]:
    """Return (is_trading_day, reason).

    A weekend or a trading holiday both produce ``(False, reason)``.
    Provider errors are treated as "not a holiday" — a network outage
    must never silently skip a real trading day.
    """
    now = _now_market()

    # 1. Weekend check (Mon-Fri only)
    if now.weekday() > 4:
        return False, f"weekend ({now.strftime('%A')})"

    # 2. Holiday check
    try:
        from services.market_holiday_service import market_holiday_service
        payload = market_holiday_service.get_today()
        status = str(payload.get("status") or "").lower()
        if status == "holiday":
            holidays = payload.get("holidays") or []
            desc = (
                holidays[0].get("description")
                if holidays and isinstance(holidays[0], dict)
                else None
            )
            return False, f"holiday ({desc or 'unknown'})"
    except Exception:
        logger.exception(
            "[sr-backfill] holiday check failed; assuming trading day"
        )

    return True, "trading_day"


def _in_window() -> tuple[bool, str]:
    """Return (in_window, reason).

    ``in_window`` is True only when the current market-local time is
    between ``SR_BACKFILL_START_HOUR:MINUTE`` (default 09:15) and
    ``SR_BACKFILL_END_HOUR:MINUTE`` (default 23:59) inclusive.
    """
    now = _now_market().time()
    start = dtime(
        int(config.SR_BACKFILL_START_HOUR),
        int(config.SR_BACKFILL_START_MINUTE),
    )
    end = dtime(
        int(config.SR_BACKFILL_END_HOUR),
        int(config.SR_BACKFILL_END_MINUTE),
    )

    if start <= now <= end:
        return True, "in_window"

    return False, (
        f"outside_window (now={now.isoformat()}, "
        f"window={start.isoformat()}-{end.isoformat()})"
    )


# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class SRBackfillService:
    def __init__(self) -> None:
        self._last_status: dict[str, Any] = {"status": "never"}

    # ------------------------------------------------------------------ #
    # Public gate — called by every entry point                          #
    # ------------------------------------------------------------------ #
    def should_run(self) -> tuple[bool, str]:
        """Return (should_run, reason).

        Called by:
          * ``services.live_feed_service._run_sr_backfill``
          * ``routes/live_sr_routes.trigger_backfill``
          * ``SRBackfillService.run`` (defensive)

        Decision order (first failure wins):

          1. ``SR_BACKFILL_ENABLED`` is False             -> "disabled"
          2. ``LIVE_SR_ENABLED`` is False                 -> "sr_disabled"
          3. Weekend / trading holiday                     -> "weekend (...)" / "holiday (...)"
          4. Outside [09:15, 23:59] IST                    -> "outside_window (...)"

        Provider errors during the holiday check are treated as
        "not a holiday" so an Upstox outage does not silently skip a
        real trading day.
        """
        # 1. Feature toggles
        if not config.SR_BACKFILL_ENABLED:
            return False, "disabled"
        if not config.LIVE_SR_ENABLED:
            return False, "sr_disabled"

        # 2. Trading day (weekend / holiday)
        trading, reason = _is_trading_day()
        if not trading:
            return False, reason

        # 3. Time window (09:15 - 23:59 IST)
        in_win, win_reason = _in_window()
        if not in_win:
            return False, win_reason

        return True, "ok"

    # ------------------------------------------------------------------ #
    # Public entry point                                                 #
    # ------------------------------------------------------------------ #
    def run(
        self,
        reason: str = "feed_start",
        entries: list[dict[str, Any]] | None = None,
        send_telegram: bool = True,
    ) -> dict[str, Any]:
        # Re-check the gate here so a direct call (bypassing
        # should_run at the caller level) cannot bypass the window.
        should, why = self.should_run()
        if not should:
            summary = {
                "status": "skipped",
                "reason": reason,
                "skip_reason": why,
                "ran_at": _now_iso(),
                "instruments": 0,
                "candles_total": 0,
                "pivots_total": 0,
                "errors": 0,
            }
            logger.info("[sr-backfill] skipped — %s", why)
            self._last_status = summary
            return summary

        if entries is None:
            from services.instrument_registry import instrument_registry
            entries = list(instrument_registry.all())

        entries = [e for e in entries if isinstance(e, dict)]
        if not entries:
            summary = {
                "status": "no_instruments",
                "reason": reason,
                "ran_at": _now_iso(),
                "instruments": 0,
                "candles_total": 0,
                "pivots_total": 0,
                "errors": 0,
            }
            self._last_status = summary
            return summary

        from services.live_sr_levels_service import live_sr_levels_service

        workers = max(1, int(config.SR_BACKFILL_MAX_WORKERS))
        t0 = _now_market()
        logger.info(
            "[sr-backfill] START reason=%s instruments=%d workers=%d",
            reason, len(entries), workers,
        )

        errors = 0
        candles_total = 0
        pivots_total = 0
        seeded_instruments = 0

        def _one(entry: dict[str, Any]) -> dict[str, Any]:
            key = str(entry.get("instrument_key") or "").strip()
            if not key:
                return {"error": "missing_key"}
            candles = _fetch_intraday_candles(key)
            if not candles:
                return {"key": key, "candles": 0, "pivots": 0, "error": None}
            try:
                result = live_sr_levels_service.seed_candles(
                    key, entry, candles,
                )
                return {
                    "key": key,
                    "candles": int(result.get("candles_seeded") or 0),
                    "pivots": int(result.get("pivots_detected") or 0),
                    "error": result.get("error"),
                }
            except Exception as exc:
                logger.exception(
                    "[sr-backfill] seed failed for %s", key,
                )
                return {
                    "key": key,
                    "candles": 0,
                    "pivots": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                }

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_one, e) for e in entries]
            for fut in as_completed(futures):
                try:
                    r = fut.result()
                except Exception:
                    errors += 1
                    continue
                c = int(r.get("candles") or 0)
                p = int(r.get("pivots") or 0)
                if r.get("error"):
                    errors += 1
                if c > 0:
                    seeded_instruments += 1
                candles_total += c
                pivots_total += p

        elapsed = (_now_market() - t0).total_seconds()
        summary = {
            "status": "ok" if errors == 0 else "partial",
            "reason": reason,
            "ran_at": _now_iso(),
            "instruments_total": len(entries),
            "instruments_seeded": seeded_instruments,
            "candles_total": candles_total,
            "pivots_total": pivots_total,
            "errors": errors,
            "elapsed_seconds": round(elapsed, 2),
        }
        logger.info(
            "[sr-backfill] COMPLETE — instruments=%d seeded=%d candles=%d "
            "pivots=%d errors=%d elapsed=%.1fs",
            len(entries), seeded_instruments, candles_total,
            pivots_total, errors, elapsed,
        )

        if send_telegram:
            try:
                from services.telegram_service import telegram_service
                _DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                _THIN = "────────────────────────────────"
                msg = (
                    f"{_DIV}\n"
                    f"🎯 SR BACKFILL · {reason.upper()}\n"
                    f"{_DIV}\n"
                    f"🕒 Time:           {_now_market().strftime('%H:%M:%S IST')}\n"
                    f"📊 Instruments:    {len(entries)}\n"
                    f"✅ Seeded:         {seeded_instruments}\n"
                    f"🕯️ Candles:        {candles_total}\n"
                    f"📐 Pivots:         {pivots_total}\n"
                    f"❌ Errors:         {errors}\n"
                    f"⏱️ Elapsed:        {round(elapsed, 1)}s\n"
                    f"{_THIN}\n"
                    f"Live ticks will continue from here. Live feed starting…"
                )
                level = "SUCCESS" if errors == 0 else "WARNING"
                telegram_service.send_message(
                    "SR Backfill Completed", msg, level=level,
                )
            except Exception:
                logger.exception(
                    "[sr-backfill] Telegram notification failed"
                )

        self._last_status = summary
        return summary

    def last_status(self) -> dict[str, Any]:
        return dict(self._last_status)


sr_backfill_service = SRBackfillService()