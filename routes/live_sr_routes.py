# routes/live_sr_routes.py
"""Dashboard data endpoints for level-filtered instruments + SR levels.

    GET  /live-sr/market-status                -> today's trading status + next open
    GET  /live-sr/index/{underlying}           -> filtered instruments + OR + SR
    GET  /live-sr/summary                      -> all configured indexes at once
    POST /live-sr/backfill                     -> manual SR backfill (gated)

The manual backfill endpoint respects the same trading-day /
time-window gate as the automatic backfill:

    * SR_BACKFILL_ENABLED must be True
    * LIVE_SR_ENABLED must be True
    * current time must be in [09:15, 23:59] IST
    * today must be Mon–Fri
    * today must NOT be a trading holiday

Outside that window, ``sr_backfill_service.run()`` returns a summary
with ``status="skipped"`` and ``skip_reason`` set. This endpoint
surfaces the same summary, plus an ``allowed`` boolean so callers
can distinguish "skipped" from "ran but produced no candles".
"""

from __future__ import annotations

import json
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

router = APIRouter(prefix="/live-sr", tags=["live-sr"])

_MARKET_TZ = ZoneInfo("Asia/Kolkata")


# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #
def _now_market() -> datetime:
    return datetime.now(_MARKET_TZ)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("[live-sr-routes] unreadable %s", path)
        return None


def _next_open_dt(now: datetime) -> datetime:
    """Next 09:15 IST on a weekday that is not a holiday."""
    candidate = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    from services.market_holiday_service import market_holiday_service
    for _ in range(14):
        if candidate.weekday() <= 4:
            try:
                payload = market_holiday_service.get_holiday(
                    candidate.date().isoformat()
                )
                if str(payload.get("status") or "").lower() != "holiday":
                    return candidate
            except Exception:
                return candidate
        candidate += timedelta(days=1)
    return candidate


def _trading_status() -> dict[str, Any]:
    now = _now_market()
    is_weekend = now.weekday() > 4
    is_holiday = False
    holiday_desc: str | None = None
    holiday_error: str | None = None
    try:
        from services.market_holiday_service import market_holiday_service
        payload = market_holiday_service.get_today()
        status = str(payload.get("status") or "").lower()
        is_holiday = status == "holiday"
        holidays = payload.get("holidays") or []
        if holidays and isinstance(holidays[0], dict):
            holiday_desc = holidays[0].get("description") or None
        if status == "error":
            holiday_error = payload.get("error")
    except Exception as exc:
        holiday_error = f"{type(exc).__name__}: {exc}"

    is_trading_day = not is_weekend and not is_holiday
    in_session = (
        is_trading_day
        and dtime(9, 15) <= now.time() <= dtime(15, 30)
    )

    if is_weekend:
        reason = f"weekend ({now.strftime('%A')})"
    elif is_holiday:
        reason = f"holiday ({holiday_desc or 'unknown'})"
    else:
        reason = "trading_day"

    next_open = _next_open_dt(now)
    countdown_seconds = int((next_open - now).total_seconds())
    message = (
        f"Today is a non-trading day ({now.strftime('%A')}). "
        f"Next trading session: {next_open.strftime('%a %d %b %Y 09:15 IST')}."
        if not is_trading_day
        else f"Trading session active. Market closes at 15:30 IST."
    )

    return {
        "today": now.date().isoformat(),
        "day_of_week": now.strftime("%A"),
        "current_time_ist": now.isoformat(),
        "is_trading_day": is_trading_day,
        "is_weekend": is_weekend,
        "is_holiday": is_holiday,
        "holiday_description": holiday_desc,
        "holiday_error": holiday_error,
        "in_session": in_session,
        "reason": reason,
        "session_open_time_ist": "09:15 IST",
        "session_close_time_ist": "15:30 IST",
        "next_open_iso": next_open.isoformat(),
        "next_open_label": next_open.strftime("%a %d %b %Y 09:15 IST"),
        "countdown_seconds": max(0, countdown_seconds),
        "message": message,
        "or_sr_available_after": "09:16:10 IST",
        "generated_at": _now_iso(),
    }


# --------------------------------------------------------------------------- #
# Filesystem helpers                                                          #
# --------------------------------------------------------------------------- #
def _index_folder(underlying: str) -> Path:
    return Path(config.LIVE_DATA_DIR) / "index" / underlying.lower()


def _index_sr_path(underlying: str) -> Path:
    return _index_folder(underlying) / config.LIVE_SR_FILENAME


def _option_sr_path(underlying: str, strike: Any, strike_type: str) -> Path:
    try:
        strike_int = int(float(strike))
    except (TypeError, ValueError):
        strike_int = str(strike)
    return (
        Path(config.LIVE_DATA_DIR)
        / underlying.lower()
        / f"{strike_int}_{str(strike_type).upper()}"
        / config.LIVE_SR_FILENAME
    )


def _build_instrument_row(
    inst: dict[str, Any],
    underlying: str,
    or_levels: dict[str, Any],
) -> dict[str, Any]:
    key = str(inst.get("instrument_key") or "").strip()
    strike = inst.get("strike")
    strike_type = str(inst.get("type") or "").upper()
    levels = list(inst.get("levels") or [])
    reasons = list(inst.get("reasons") or [])

    level_price = None
    for name in levels:
        raw = or_levels.get(name)
        if raw is not None:
            try:
                level_price = float(raw)
                break
            except (TypeError, ValueError):
                pass

    sr_payload = _read_json(_option_sr_path(underlying, strike, strike_type))
    sr_levels = (sr_payload or {}).get("levels") or []
    sr_count = len(sr_levels)
    sr_latest = None
    if sr_levels:
        try:
            sr_latest = float(sr_levels[-1].get("price"))
        except (TypeError, ValueError):
            sr_latest = None

    ltp_snapshot = None
    try:
        from services.live_feed_service import live_feed_service
        ltp_snapshot = live_feed_service.get_latest_ltp(key)
    except Exception:
        ltp_snapshot = None

    return {
        "instrument_key": key,
        "trading_symbol": inst.get("trading_symbol"),
        "strike": strike,
        "type": strike_type,
        "expiry": inst.get("expiry"),
        "levels": levels,
        "reasons": reasons,
        "level_price": level_price,
        "sr_levels_count": sr_count,
        "sr_latest_level": sr_latest,
        "sr_available": bool(sr_payload),
        "ltp_server_snapshot": ltp_snapshot,
        "chart_url": f"/chart/{key.replace('|', '%7C').replace(' ', '%20')}",
    }


def _build_index_payload(underlying: str) -> dict[str, Any]:
    u = str(underlying or "").strip().upper()
    if not u:
        raise HTTPException(
            status_code=400,
            detail={"error": "underlying_required"},
        )

    folder = _index_folder(u)
    first_candle = _read_json(folder / "first_candle.json")
    level_instruments = _read_json(folder / "level_instruments.json")

    if level_instruments is None:
        return {
            "underlying": u,
            "status": "not_generated",
            "path": str(folder / "level_instruments.json"),
            "hint": (
                "level_instruments.json not yet written for today. "
                "POST /admin/ops/level-instruments/recalc to force."
            ),
            "generated_at": _now_iso(),
        }

    or_levels = (first_candle or {}).get("or_levels") or {}
    fc = (first_candle or {}).get("first_candle") or {}

    ce = [
        _build_instrument_row(i, u, or_levels)
        for i in (level_instruments.get("ce_instruments") or [])
    ]
    pe = [
        _build_instrument_row(i, u, or_levels)
        for i in (level_instruments.get("pe_instruments") or [])
    ]

    index_key = level_instruments.get("index_instrument_key")
    index_sr = _read_json(_index_sr_path(u))
    index_sr_count = len((index_sr or {}).get("levels") or [])
    index_ltp = None
    if index_key:
        try:
            from services.live_feed_service import live_feed_service
            index_ltp = live_feed_service.get_latest_ltp(index_key)
        except Exception:
            index_ltp = None

    return {
        "underlying": u,
        "status": "ok",
        "date": level_instruments.get("date"),
        "strike_step": level_instruments.get("strike_step"),
        "range_filter": level_instruments.get("range_filter"),
        "first_candle": fc,
        "or_levels": or_levels,
        "index_instrument_key": index_key,
        "index_sr_levels_count": index_sr_count,
        "index_ltp": index_ltp,
        "ce_instruments": ce,
        "pe_instruments": pe,
        "generated_at": _now_iso(),
    }


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #
@router.get("/market-status")
def market_status() -> dict[str, Any]:
    """Return the current trading-day status + next session info.

    Used by the dashboard to show the non-market-day banner and to gate
    the level-filtered section (which stays empty on non-trading days).
    """
    return _trading_status()


@router.get("/index/{underlying}")
def index_sr(underlying: str) -> dict[str, Any]:
    """Return the level-filtered CE/PE instruments for one index.

    Each instrument row carries its OR level(s), the raw level price,
    the count of live SR levels detected so far, and a chart URL.
    """
    return _build_index_payload(underlying)


@router.get("/summary")
def summary() -> dict[str, Any]:
    """Return the market status + every configured index's payload."""
    status = _trading_status()
    indexes: dict[str, Any] = {}
    for u in config.LEVEL_FILTER_INDEXES:
        uu = str(u or "").strip().upper()
        if not uu:
            continue
        try:
            indexes[uu] = _build_index_payload(uu)
        except HTTPException:
            raise
        except Exception as exc:
            indexes[uu] = {
                "underlying": uu,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
    return {
        "market_status": status,
        "indexes": indexes,
        "generated_at": _now_iso(),
    }


@router.post("/backfill")
def trigger_backfill(send_telegram: bool = False) -> dict[str, Any]:
    """Force a manual SR backfill (runs the intraday seed).

    GATE
    ----
    This endpoint honours the same trading-day / time-window gate as
    the automatic feed-start backfill:

      * ``SR_BACKFILL_ENABLED`` must be True
      * ``LIVE_SR_ENABLED`` must be True
      * current time must be in ``[09:15, 23:59]`` IST
      * today must be Mon–Fri
      * today must NOT be a trading holiday

    The response always includes ``allowed`` (the gate result) and
    ``skip_reason`` (the human-readable reason when skipped) so a
    caller can distinguish a "skipped" response from a run that
    simply produced no candles.
    """
    try:
        from services.sr_backfill_service import sr_backfill_service
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": f"{type(exc).__name__}: {exc}"},
        )

    # Gate check (same logic the service uses internally).
    allowed, skip_reason = sr_backfill_service.should_run()

    result = sr_backfill_service.run(
        reason="manual", send_telegram=send_telegram,
    )

    # Annotate the response so callers can tell the two cases apart.
    result["allowed"] = bool(allowed)
    result["skip_reason"] = (
        skip_reason if not allowed else result.get("skip_reason")
    )
    result["window_ist"] = (
        f"{int(config.SR_BACKFILL_START_HOUR):02d}:"
        f"{int(config.SR_BACKFILL_START_MINUTE):02d}"
        f"-"
        f"{int(config.SR_BACKFILL_END_HOUR):02d}:"
        f"{int(config.SR_BACKFILL_END_MINUTE):02d}"
    )
    return result