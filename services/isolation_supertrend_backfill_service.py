"""Evaluate pre-isolation Supertrend price movement from a supplied feed snapshot."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core.config import (
    ISOLATION_SUPERTREND_MIN_MOVE_PERCENT,
    TIMEZONE,
)
from core.logger import get_logger

logger = get_logger(__file__)


def _parse_ist_datetime(value: object) -> datetime | None:
    """Parse an ISO timestamp as IST; naive timestamps are interpreted as IST."""
    if value in (None, ""):
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).strip().replace("Z", "+00:00")
        )
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=ZoneInfo(TIMEZONE))
        return parsed.astimezone(ZoneInfo(TIMEZONE))
    except (TypeError, ValueError, OverflowError):
        return None


def _positive_number(value: object) -> float | None:
    try:
        number = float(value)
        if number > 0 and number == number and number not in (float("inf"), float("-inf")):
            return number
    except (TypeError, ValueError):
        pass
    return None


def evaluate_pre_isolation_price_trend(
    backfill: dict,
    isolation_time: object,
    candles: list[dict],
    *,
    min_move_percent: float | None = None,
) -> dict:
    """Classify net option-price movement from the first signal to isolation.

    Only confirmed_time_price is treated as a market price. The Supertrend
    line value in signals[].price is retained as evidence, not compared as LTP.
    The endpoint is the last completed one-minute candle close at isolation.
    """
    result = {
        "status": "no_valid_signal",
        "method": "first_confirmed_market_price_to_isolation_candle_close",
        "evaluated_at_ist": datetime.now(ZoneInfo(TIMEZONE)).isoformat(),
        "isolation_time_ist": None,
        "direction": None,
        "reason": None,
        "eligible_signal_count": 0,
        "signal_evidence": [],
        "baseline": None,
        "isolation_candle": None,
        "price_change": None,
        "price_change_percent": None,
        "min_move_percent": (ISOLATION_SUPERTREND_MIN_MOVE_PERCENT if min_move_percent is None else float(min_move_percent)),
    }
    if not isinstance(backfill, dict) or backfill.get("status") != "loaded":
        result.update(status="data_unavailable", reason="Supertrend backfill is not loaded")
        return result
    document = backfill.get("data")
    signals = document.get("signals") if isinstance(document, dict) else None
    if not isinstance(signals, list):
        result.update(status="data_unavailable", reason="Supertrend signals are unavailable")
        return result
    isolation_dt = _parse_ist_datetime(isolation_time)
    if isolation_dt is None:
        result.update(status="data_unavailable", reason="Isolation timestamp is invalid")
        return result
    result["isolation_time_ist"] = isolation_dt.isoformat()
    eligible = []
    for index, item in enumerate(signals):
        if not isinstance(item, dict):
            continue
        stamp = _parse_ist_datetime(item.get("confirmed_time"))
        price = _positive_number(item.get("confirmed_time_price"))
        signal_name = str(item.get("signal") or "").strip().lower()
        if stamp is None or stamp > isolation_dt or price is None or signal_name not in {"buy", "sell"}:
            continue
        eligible.append({
            "index": index,
            "signal": signal_name,
            "confirmed_time": stamp,
            "confirmed_time_price": price,
            "supertrend_line_price": _positive_number(item.get("price")),
        })
    eligible.sort(key=lambda item: (item["confirmed_time"], item["index"]))
    result["eligible_signal_count"] = len(eligible)
    result["signal_evidence"] = [{
        "signal": item["signal"],
        "confirmed_time": item["confirmed_time"].isoformat(),
        "confirmed_time_price": item["confirmed_time_price"],
        "supertrend_line_price": item["supertrend_line_price"],
    } for item in eligible]
    if not eligible:
        result.update(status="no_pre_isolation_signals", reason="No pre-isolation signal has a valid confirmed_time_price")
        return result
    baseline = eligible[0]
    result["baseline"] = {
        "signal": baseline["signal"],
        "confirmed_time_ist": baseline["confirmed_time"].isoformat(),
        "confirmed_time_price": baseline["confirmed_time_price"],
    }
    completed = []
    for candle in candles or []:
        if not isinstance(candle, dict):
            continue
        candle_time = _parse_ist_datetime(candle.get("timestamp"))
        close = _positive_number(candle.get("close"))
        if candle_time is None or close is None:
            continue
        # Upstox candle timestamps mark the start of the one-minute interval.
        if candle_time + timedelta(minutes=1) <= isolation_dt:
            completed.append((candle_time, close))
    if not completed:
        result.update(status="candle_unavailable", reason="No fully completed one-minute candle exists at or before isolation")
        return result
    candle_time, candle_close = max(completed, key=lambda item: item[0])
    change = candle_close - baseline["confirmed_time_price"]
    change_percent = (change / baseline["confirmed_time_price"]) * 100.0
    threshold = max(0.0, float(result["min_move_percent"]))
    result["isolation_candle"] = {
        "timestamp": candle_time.isoformat(),
        "completed_at_ist": (candle_time + timedelta(minutes=1)).isoformat(),
        "close": candle_close,
        "source": "upstox_intraday_1m",
    }
    result["price_change"] = round(change, 6)
    result["price_change_percent"] = round(change_percent, 6)
    result["latest_signal_time_ist"] = eligible[-1]["confirmed_time"].isoformat()
    result["latest_signal_direction"] = eligible[-1]["signal"]
    result["latest_signal_confirmed_price"] = eligible[-1]["confirmed_time_price"]
    if change_percent > threshold:
        result.update(status="valid_bullish", direction="BULLISH", reason="Isolation candle close is above the first confirmed market price")
    elif change_percent < -threshold:
        result.update(status="valid_bearish", direction="BEARISH", reason="Isolation candle close is below the first confirmed market price")
    else:
        result.update(status="no_valid_signal", reason="Net price movement is within the configured neutral threshold")
    logger.info(
        "Pre-isolation Supertrend price evaluation | status=%s | baseline=%.2f | isolation_close=%.2f | change=%.4f%% | signals=%d",
        result["status"], baseline["confirmed_time_price"], candle_close, change_percent, len(eligible),
    )
    return result
