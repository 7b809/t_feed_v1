# services/sr_backfill_service.py
"""Intraday SR backfill — seeds the SR engine from candles.

## 2026-10-26 change
The service now (by default) fetches **historical + intraday** merged
candles for each instrument, matching the on-demand calc at
``/live-sr/calc``. Previously it seeded from today's intraday only,
which caused the live engine to miss pivots at the start of the day
(those that require 15 bars *before* the first tick, i.e. yesterday's
tail).

Config: ``SR_BACKFILL_INCLUDE_HISTORICAL`` (default true).

Runs when ALL of the following hold:
  * ``SR_BACKFILL_ENABLED`` is True
  * ``LIVE_SR_ENABLED`` is True
  * current time is in the configured window (default 09:15–23:59 IST)
  * today is Mon-Fri
  * today is NOT a trading holiday

Called from:
  * ``services.live_feed_service.LiveFeedService.start()`` — before
    spawning the WebSocket worker thread.
  * ``POST /live-sr/backfill`` — manual trigger.
  * The periodic scheduler job ``live_sr_refresh_from_api`` (every
    ``LIVE_SR_REFRESH_FROM_API_MINUTES`` minutes).

Telegram: sent only on the ``startup`` / ``feed_start`` path. The
periodic refresh passes ``send_telegram=False`` to avoid flooding.
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

def _fetch_merged_candles(
    entry: dict[str, Any],
    lookback_days: int,
) -> list[dict[str, Any]]:
    """Fetch historical + intraday merged candles for one instrument.

    Uses the shared loader / merger from ``live_sr_calc_service`` so the
    backfill path and the calc endpoint stay in lockstep. Never raises —
    a failure on either source returns whatever the other produced.
    """
    try:
        from services.live_sr_calc_service import live_sr_calc_service
    except Exception:
        logger.exception(
            "[sr-backfill] live_sr_calc_service import failed; "
            "falling back to intraday-only"
        )
        return _fetch_intraday_candles(entry.get("instrument_key") or "")

    key = str(entry.get("instrument_key") or "").strip()
    if not key:
        return []

    try:
        historical = live_sr_calc_service.load_historical_candles_for_entry(
            entry, lookback_days,
        )
    except Exception:
        logger.exception(
            "[sr-backfill] historical load failed for %s", key,
        )
        historical = []

    try:
        intraday = live_sr_calc_service.fetch_intraday_candles_for_entry(key)
    except Exception:
        logger.exception(
            "[sr-backfill] intraday fetch failed for %s", key,
        )
        intraday = []

    if historical and intraday:
        return live_sr_calc_service.merge_candles(historical, intraday)
    return historical or intraday

# --------------------------------------------------------------------------- #
# Gate helpers                                                                #
# --------------------------------------------------------------------------- #
def _is_trading_day() -> tuple[bool, str]:
    now = _now_market()
    if now.weekday() > 4:
        return False, f"weekend ({now.strftime('%A')})"

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

    def should_run(self) -> tuple[bool, str]:
        if not config.SR_BACKFILL_ENABLED:
            return False, "disabled"
        if not config.LIVE_SR_ENABLED:
            return False, "sr_disabled"
        trading, reason = _is_trading_day()
        if not trading:
            return False, reason
        in_win, win_reason = _in_window()
        if not in_win:
            return False, win_reason
        return True, "ok"

    def run(
        self,
        reason: str = "feed_start",
        entries: list[dict[str, Any]] | None = None,
        send_telegram: bool = True,
    ) -> dict[str, Any]:
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

        include_historical = bool(
            getattr(config, "SR_BACKFILL_INCLUDE_HISTORICAL", True)
        )
        lookback_days = int(
            getattr(config, "LIVE_SR_CALC_DEFAULT_LOOKBACK_DAYS", 5)
        )
        workers = max(1, int(config.SR_BACKFILL_MAX_WORKERS))
        t0 = _now_market()
        logger.info(
            "[sr-backfill] START reason=%s instruments=%d workers=%d "
            "include_historical=%s lookback_days=%d",
            reason, len(entries), workers, include_historical, lookback_days,
        )

        errors = 0
        candles_total = 0
        pivots_total = 0
        seeded_instruments = 0

        def _one(entry: dict[str, Any]) -> dict[str, Any]:
            key = str(entry.get("instrument_key") or "").strip()
            if not key:
                return {"error": "missing_key"}

            if include_historical:
                candles = _fetch_merged_candles(entry, lookback_days)
            else:
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
            "include_historical": include_historical,
            "lookback_days": lookback_days,
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
                    f"📜 Historical:     "
                    f"{'included' if include_historical else 'disabled'}\n"
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