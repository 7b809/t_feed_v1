"""Read-only loader for precomputed Supertrend data of a newly isolated option."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from core.config import (
    ISOLATION_SUPERTREND_BACKFILL_DIR,
    ISOLATION_SUPERTREND_MIN_MOVE_PERCENT,
    TIMEZONE,
)
from core.logger import get_logger

logger = get_logger(__file__)


def _strike_folder_name(strike: object, option_type: object) -> str:
    """Build the MEGA folder name, for example 25700_CE, from the isolated contract."""
    try:
        numeric_strike = float(strike)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid isolated strike: {strike!r}") from exc

    if numeric_strike <= 0:
        raise ValueError(f"Invalid isolated strike: {strike!r}")

    strike_text = (
        str(int(numeric_strike))
        if numeric_strike.is_integer()
        else format(numeric_strike, "f").rstrip("0").rstrip(".")
    )
    option = str(option_type or "").strip().upper()
    if option not in {"CE", "PE"}:
        raise ValueError(f"Invalid isolated option type: {option_type!r}")
    return f"{strike_text}_{option}"


def load_isolated_supertrend_backfill(record: dict) -> dict:
    """Load the supertrend.json file for the isolated contract.

    The full JSON document is returned under data and tagged as backfill
    data. No signal is emitted, replayed, qualified, or executed here.
    Missing, stale, mismatched, or invalid files are represented by a status
    object so the isolation workflow can continue safely.
    """
    now = datetime.now(ZoneInfo(TIMEZONE))
    instrument_key = str(record.get("instrument_key") or "")
    trade_date = str(record.get("trade_date_ist") or now.date().isoformat())

    result = {
        "status": "not_configured",
        "is_backfill": True,
        "source": "mega_supertrend_json",
        "instrument_key": instrument_key,
        "trade_date_ist": trade_date,
        "loaded_at_ist": now.isoformat(),
        "folder": None,
        "file_path": None,
        "signal_count": 0,
        "data": None,
    }

    if not ISOLATION_SUPERTREND_BACKFILL_DIR:
        logger.info(
            "Supertrend backfill directory not configured | instrument=%s",
            instrument_key,
        )
        return result

    try:
        folder_name = _strike_folder_name(
            record.get("strike"), record.get("option_type")
        )
    except ValueError as exc:
        result.update(status="invalid_instrument", error=str(exc))
        logger.warning(
            "Cannot resolve Supertrend backfill folder | instrument=%s | %s",
            instrument_key, exc,
        )
        return result

    root = Path(ISOLATION_SUPERTREND_BACKFILL_DIR).expanduser()
    folder = (root / folder_name).resolve()
    root_resolved = root.resolve()
    if folder.parent != root_resolved:
        result.update(status="invalid_path", folder=folder_name)
        logger.error("Rejected Supertrend backfill path outside configured root: %s", folder)
        return result

    source = folder / "supertrend.json"
    result.update(folder=folder_name, file_path=str(source))

    if not source.is_file():
        result["status"] = "missing"
        logger.info(
            "Supertrend backfill file not found | instrument=%s | path=%s",
            instrument_key, source,
        )
        return result

    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result.update(status="invalid_json", error=str(exc))
        logger.warning(
            "Could not read Supertrend backfill JSON | instrument=%s | path=%s | error=%s",
            instrument_key, source, exc,
        )
        return result

    if not isinstance(document, dict):
        result.update(status="invalid_format", error="Root JSON value must be an object")
        return result
    if str(document.get("indicator") or "").lower() != "supertrend":
        result.update(status="invalid_format", error="indicator must be 'supertrend'")
        return result
    if document.get("data_scope") != "today_only":
        result.update(status="invalid_scope", error="data_scope must be 'today_only'")
        return result
    if str(document.get("today_ist_date") or "") != trade_date:
        result.update(
            status="stale",
            error=(
                f"today_ist_date={document.get('today_ist_date')!r} "
                f"does not match isolation trade date {trade_date!r}"
            ),
        )
        return result
    document_instrument = str(document.get("instrument_key") or "")
    if document_instrument and instrument_key and document_instrument != instrument_key:
        result.update(
            status="instrument_mismatch",
            error=f"file instrument_key {document_instrument!r} does not match {instrument_key!r}",
        )
        return result

    signals = document.get("signals")
    if not isinstance(signals, list) or any(not isinstance(item, dict) for item in signals):
        result.update(status="invalid_format", error="signals must be a list of objects")
        return result

    result.update(status="loaded", signal_count=len(signals), data=document)
    logger.info(
        "Loaded Supertrend backfill data | instrument=%s | folder=%s | signals=%d | date=%s",
        instrument_key, folder_name, len(signals), trade_date,
    )
    return result


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
