import json
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import upstox_client
from upstox_client.rest import ApiException

from core import config
from core.logger import get_logger
from services.option_service import options_cache
from services.telegram_service import telegram_service

logger = get_logger(__file__)

# ============================================================
# Thread-Safe Runtime Cache
# ============================================================

_opening_range_cache_lock = Lock()

opening_range_cache = {
    "last_run_at": None,
    "date": None,
    "status": "not_started",
    "message": "Opening range calculation has not run yet.",
    "source": "intraday_api",
    "interval": None,
    "opening_range_candle_count": 0,
    "market_open_time": None,
    "fetch_time": None,
    "total_instruments": 0,
    "success_count": 0,
    "failed_count": 0,
    "empty_count": 0,
    "insufficient_data_count": 0,
    "output_file_path": None,
    "latest_main_index_ltp": None,
    "latest_main_index_ltp_source": None,
    "latest_main_index_ltp_updated_at": None,
    "touch_events_count": 0,
    "pending_touch_events_count": 0,
    "alert_sent_keys_count": 0,
    "data": {},
    "touch_events": [],
    "errors": {},
}

_touch_lock = Lock()

_pending_touch_events = deque()

_touch_events = deque(
    maxlen=int(getattr(config, "OPENING_RANGE_MAX_EVENTS_IN_MEMORY", 5000))
)

_alert_sent_keys = set()

_latest_main_index_ltp = None
_latest_main_index_ltp_source = None
_latest_main_index_ltp_updated_at = None
_last_touch_alert_sent_at = None


# ============================================================
# Disabled Selected OR Compatibility State
# ============================================================
# New requirement:
# - No first touched instrument should be selected.
# - No selected OR EMA Telegram alert should be sent.
# - These objects/functions are kept only for API backward compatibility.

_selected_or_lock = Lock()

_selected_or_instrument_state = {
    "selected": False,
    "instrument_key": None,
    "selected_level": None,
    "level_value": None,
    "trigger_price": None,
    "trigger_field": None,
    "touch_time": None,
    "touch_source": None,
    "selected_at": None,
    "contract_info": None,
    "range": None,
    "levels": None,
    "latest_live_data": None,
    "latest_main_index_ltp": None,
    "ema_alerts_count": 0,
    "last_ema_alert": None,
    "disabled": True,
    "message": "Selected Opening Range instrument flow is disabled.",
}

_selected_or_ema_alerts = deque(
    maxlen=int(getattr(config, "OPENING_RANGE_MAX_EVENTS_IN_MEMORY", 5000))
)


# ============================================================
# Config Defaults
# ============================================================

DEFAULT_OPENING_RANGE_ENABLED = bool(getattr(config, "OPENING_RANGE_ENABLED", True))

DEFAULT_OPENING_RANGE_INTERVAL = getattr(
    config,
    "OPENING_RANGE_INTERVAL",
    "1minute",
)

DEFAULT_OPENING_RANGE_CANDLE_COUNT = int(
    getattr(config, "OPENING_RANGE_CANDLE_COUNT", 1)
)

DEFAULT_MARKET_OPEN_HOUR = int(getattr(config, "OPENING_RANGE_MARKET_OPEN_HOUR", 9))

DEFAULT_MARKET_OPEN_MINUTE = int(
    getattr(config, "OPENING_RANGE_MARKET_OPEN_MINUTE", 15)
)

DEFAULT_FETCH_HOUR = int(getattr(config, "OPENING_RANGE_FETCH_HOUR", 9))

DEFAULT_FETCH_MINUTE = int(getattr(config, "OPENING_RANGE_FETCH_MINUTE", 18))

DEFAULT_INTRADAY_UNIT = getattr(
    config,
    "OPENING_RANGE_INTRADAY_UNIT",
    "minutes",
)

DEFAULT_INTRADAY_INTERVAL = getattr(
    config,
    "OPENING_RANGE_INTRADAY_INTERVAL",
    "1",
)

DEFAULT_MAX_WORKERS = int(getattr(config, "OPENING_RANGE_MAX_WORKERS", 5))

DEFAULT_SLEEP_SECONDS = float(
    getattr(config, "OPENING_RANGE_REQUEST_SLEEP_SECONDS", 0.15)
)

DEFAULT_SAVE_FILE = bool(getattr(config, "OPENING_RANGE_SAVE_FILE", True))

DEFAULT_OUTPUT_FILE = getattr(
    config,
    "OPENING_RANGE_OUTPUT_FILE",
    "data/opening_range_results.json",
)

DEFAULT_BACKFILL_SCAN_ENABLED = bool(
    getattr(config, "OPENING_RANGE_BACKFILL_TOUCH_SCAN_ENABLED", True)
)

DEFAULT_TOUCH_ALERT_ENABLED = bool(
    getattr(config, "OPENING_RANGE_TOUCH_ALERT_ENABLED", True)
)

DEFAULT_BACKFILL_TOUCH_ALERT_ENABLED = bool(
    getattr(config, "OPENING_RANGE_BACKFILL_TOUCH_ALERT_ENABLED", True)
)

DEFAULT_LIVE_TOUCH_ALERT_ENABLED = bool(
    getattr(config, "OPENING_RANGE_LIVE_TOUCH_ALERT_ENABLED", True)
)

DEFAULT_TOUCH_ALERT_MAX_INSTRUMENTS = int(
    getattr(config, "OPENING_RANGE_TOUCH_ALERT_MAX_INSTRUMENTS", 5)
)

DEFAULT_TOUCH_ALERT_BATCH_SECONDS = int(
    getattr(config, "OPENING_RANGE_TOUCH_ALERT_BATCH_SECONDS", 10)
)

DEFAULT_TOUCH_ALERT_ONCE_PER_LEVEL = bool(
    getattr(config, "OPENING_RANGE_TOUCH_ALERT_ONCE_PER_LEVEL", True)
)

DEFAULT_TOUCH_ALERT_OPTIONS_ONLY = bool(
    getattr(config, "OPENING_RANGE_TOUCH_ALERT_OPTIONS_ONLY", True)
)

DEFAULT_SORT_BY_NEAREST_INDEX = bool(
    getattr(config, "OPENING_RANGE_TOUCH_ALERT_SORT_BY_NEAREST_INDEX", True)
)

DEFAULT_MAIN_INDEX_KEY = getattr(
    config,
    "OPENING_RANGE_TOUCH_ALERT_MAIN_INDEX_KEY",
    getattr(config, "MAIN_NIFTY_SECURITY", "NSE_INDEX|Nifty 50"),
)

DEFAULT_TOUCH_CHECK_MODE = getattr(
    config,
    "OPENING_RANGE_TOUCH_CHECK_MODE",
    "high_low",
)

DEFAULT_TOUCH_EVENTS_OUTPUT_FILE = getattr(
    config,
    "OPENING_RANGE_TOUCH_EVENTS_OUTPUT_FILE",
    "data/opening_range_touch_events.json",
)

DEFAULT_TOUCH_EVENTS_SAVE_TEST_FILE = bool(
    getattr(config, "OPENING_RANGE_TOUCH_EVENTS_SAVE_TEST_FILE", True)
)

DEFAULT_LEGACY_TOUCH_TELEGRAM_ENABLED = bool(
    getattr(config, "OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED", False)
)

DEFAULT_EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS = bool(
    getattr(config, "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS", True)
)


# ============================================================
# Basic Helpers
# ============================================================


def is_opening_range_enabled() -> bool:
    """Returns whether opening range calculation is enabled."""

    return bool(getattr(config, "OPENING_RANGE_ENABLED", True))


def get_market_timezone():
    """
    Loads market timezone from config.

    Default:
        Asia/Kolkata
    """

    timezone_name = getattr(config, "MARKET_TIMEZONE", "Asia/Kolkata")

    try:
        return ZoneInfo(timezone_name)

    except ZoneInfoNotFoundError:
        logger.error(
            f"Invalid MARKET_TIMEZONE configured: {timezone_name}. "
            "Falling back to Asia/Kolkata."
        )
        return ZoneInfo("Asia/Kolkata")


def get_now_market_time() -> datetime:
    """Returns current datetime in configured market timezone."""

    return datetime.now(get_market_timezone())


def safe_float(value, default: float = 0.0) -> float:
    """Safely converts value to float."""

    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value, default: int = 0) -> int:
    """Safely converts value to int."""

    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def response_to_dict(api_response: Any) -> dict:
    """Converts Upstox SDK response object to dictionary safely."""

    if api_response is None:
        return {}

    if hasattr(api_response, "to_dict"):
        return api_response.to_dict()

    if isinstance(api_response, dict):
        return api_response

    try:
        return dict(api_response)
    except Exception:
        return {}


def extract_candles_from_response(api_response: Any) -> list:
    """Extracts candle list from Upstox intraday candle API response."""

    response_dict = response_to_dict(api_response)
    data = response_dict.get("data", {})

    if isinstance(data, dict):
        candles = data.get("candles", [])
        return candles if isinstance(candles, list) else []

    return []


def parse_candle_timestamp(timestamp_value) -> datetime | None:
    """Parses Upstox candle timestamp into timezone-aware datetime."""

    if timestamp_value is None:
        return None

    market_tz = get_market_timezone()

    try:
        if isinstance(timestamp_value, (int, float)):
            return datetime.fromtimestamp(
                int(timestamp_value) / 1000,
                tz=market_tz,
            )

        text = str(timestamp_value).strip()

        if not text:
            return None

        if text.isdigit():
            return datetime.fromtimestamp(
                int(text) / 1000,
                tz=market_tz,
            )

        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=market_tz)
        else:
            parsed = parsed.astimezone(market_tz)

        return parsed

    except Exception:
        return None


def normalize_candle(candle: list) -> dict | None:
    """
    Normalizes Upstox candle array.

    Expected candle shape:
        [timestamp, open, high, low, close, volume, oi]
    """

    if not isinstance(candle, list) or len(candle) < 5:
        return None

    candle_dt = parse_candle_timestamp(candle[0])

    if not candle_dt:
        return None

    return {
        "timestamp": candle_dt.isoformat(),
        "datetime": candle_dt,
        "open": safe_float(candle[1]),
        "high": safe_float(candle[2]),
        "low": safe_float(candle[3]),
        "close": safe_float(candle[4]),
        "volume": safe_int(candle[5]) if len(candle) > 5 else 0,
        "oi": safe_int(candle[6]) if len(candle) > 6 else 0,
    }


def normalize_candles(candles: list) -> list:
    """Normalizes, filters, deduplicates, and sorts candle arrays."""

    normalized = []

    for candle in candles:
        item = normalize_candle(candle)

        if item:
            normalized.append(item)

    seen = set()
    unique = []

    for item in normalized:
        ts = item.get("timestamp")

        if ts not in seen:
            seen.add(ts)
            unique.append(item)

    try:
        return sorted(unique, key=lambda x: x.get("datetime"))
    except Exception:
        return unique


def serialize_candle(candle: dict) -> dict:
    """Serializes normalized candle for JSON output."""

    return {
        "timestamp": candle.get("timestamp"),
        "open": candle.get("open"),
        "high": candle.get("high"),
        "low": candle.get("low"),
        "close": candle.get("close"),
        "volume": candle.get("volume"),
        "oi": candle.get("oi"),
    }


def get_subscribed_instrument_keys() -> list:
    """Returns subscribed instrument keys from options_cache."""

    subscribed_keys = options_cache.get("subscribed_keys", [])

    if not subscribed_keys:
        return []

    return list(dict.fromkeys(subscribed_keys))


def get_contract_info_by_key(instrument_key: str) -> dict:
    """Returns contract metadata for an instrument key."""

    main_key = getattr(config, "MAIN_NIFTY_SECURITY", "NSE_INDEX|Nifty 50")

    if instrument_key == main_key:
        return {
            "instrument_key": instrument_key,
            "instrument_type": "INDEX",
            "strike_price": None,
            "expiry": None,
            "trading_symbol": "NIFTY 50",
            "underlying_type": "INDEX",
            "underlying_symbol": "NIFTY 50",
        }

    for item in options_cache.get("data", []):
        if item.get("instrument_key") == instrument_key:
            return item

    return {
        "instrument_key": instrument_key,
    }


def is_option_contract(contract_info: dict | None) -> bool:
    """Returns True if contract is CE or PE option."""

    if not contract_info:
        return False

    instrument_type = str(contract_info.get("instrument_type", "")).upper()

    return instrument_type in ["CE", "PE"]


# ============================================================
# Opening Range Candle Selection
# ============================================================


def get_market_open_datetime(target_date=None) -> datetime:
    """Builds market open datetime in market timezone."""

    market_tz = get_market_timezone()
    now_market = get_now_market_time()

    if target_date is None:
        target_date = now_market.date()

    return datetime.combine(
        target_date,
        dt_time(
            hour=DEFAULT_MARKET_OPEN_HOUR,
            minute=DEFAULT_MARKET_OPEN_MINUTE,
        ),
        tzinfo=market_tz,
    )


def get_opening_range_end_datetime(
    candle_count: int = DEFAULT_OPENING_RANGE_CANDLE_COUNT,
    target_date=None,
) -> datetime:
    """Returns OR completion time."""

    candle_count = max(1, int(candle_count or 1))
    market_open_dt = get_market_open_datetime(target_date=target_date)

    return market_open_dt + timedelta(minutes=candle_count)


def select_opening_range_candles(
    candles: list,
    candle_count: int = DEFAULT_OPENING_RANGE_CANDLE_COUNT,
) -> list:
    """Selects first N candles from market open."""

    if not candles:
        return []

    candle_count = max(1, int(candle_count or 1))
    market_open_dt = get_market_open_datetime()

    selected = []

    for candle in candles:
        candle_dt = candle.get("datetime")

        if not candle_dt:
            continue

        if candle_dt >= market_open_dt:
            selected.append(candle)

        if len(selected) >= candle_count:
            break

    return selected


def select_post_opening_range_candles(
    candles: list,
    candle_count: int = DEFAULT_OPENING_RANGE_CANDLE_COUNT,
) -> list:
    """Selects candles after OR completion time."""

    if not candles:
        return []

    or_end_dt = get_opening_range_end_datetime(candle_count=candle_count)

    post_or_candles = []

    for candle in candles:
        candle_dt = candle.get("datetime")

        if not candle_dt:
            continue

        if candle_dt >= or_end_dt:
            post_or_candles.append(candle)

    return post_or_candles


# ============================================================
# Opening Range Formula
# ============================================================


def calculate_opening_range_levels(selected_candles: list) -> dict:
    """Calculates opening range levels using Pine Script-compatible formula."""

    if not selected_candles:
        return {
            "status": "empty",
            "message": "No opening range candles selected.",
            "range": None,
            "levels": None,
        }

    range_open = safe_float(selected_candles[0].get("open"))
    range_close = safe_float(selected_candles[-1].get("close"))
    range_high = max(safe_float(item.get("high")) for item in selected_candles)
    range_low = min(safe_float(item.get("low")) for item in selected_candles)

    range_avg = (range_high + range_low) / 2

    high_avg_diff = abs(range_high - range_avg)
    low_avg_diff = abs(range_avg - range_low)

    sub_resistance = range_avg + (high_avg_diff / 2)
    sub_support = range_avg - (low_avg_diff / 2)

    resistance2 = range_high + high_avg_diff
    support2 = range_low - low_avg_diff

    resistance3 = resistance2 + high_avg_diff
    support3 = support2 - low_avg_diff

    r3_threshold = (resistance2 + resistance3) / 2
    s3_threshold = (support2 + support3) / 2

    return {
        "status": "success",
        "message": "Opening range levels calculated successfully.",
        "range": {
            "open": round(range_open, 4),
            "high": round(range_high, 4),
            "low": round(range_low, 4),
            "close": round(range_close, 4),
            "average": round(range_avg, 4),
            "selected_candles_count": len(selected_candles),
            "first_candle_time": selected_candles[0].get("timestamp"),
            "last_candle_time": selected_candles[-1].get("timestamp"),
        },
        "levels": {
            "r1": round(sub_resistance, 4),
            "s1": round(sub_support, 4),
            "r2": round(resistance2, 4),
            "s2": round(support2, 4),
            "r3": round(resistance3, 4),
            "s3": round(support3, 4),
            "sub_resistance": round(sub_resistance, 4),
            "sub_support": round(sub_support, 4),
            "resistance2": round(resistance2, 4),
            "support2": round(support2, 4),
            "resistance3": round(resistance3, 4),
            "support3": round(support3, 4),
            "r3_threshold": round(r3_threshold, 4),
            "s3_threshold": round(s3_threshold, 4),
        },
    }


# ============================================================
# Touch Event Helpers
# ============================================================


def build_alert_key(instrument_key: str, level: str) -> str:
    """Builds unique alert key for duplicate prevention."""

    return f"{instrument_key}_{str(level).upper()}"


def calculate_distance_from_index(strike_price, index_ltp) -> float | None:
    """Calculates distance of option strike from latest NIFTY index LTP."""

    try:
        if strike_price is None or index_ltp is None:
            return None

        return round(abs(float(strike_price) - float(index_ltp)), 4)

    except Exception:
        return None


def update_latest_main_index_ltp(
    ltp,
    source: str = "unknown",
    updated_at: str | None = None,
):
    """Updates latest main index LTP used for nearest strike ranking."""

    global _latest_main_index_ltp
    global _latest_main_index_ltp_source
    global _latest_main_index_ltp_updated_at

    value = safe_float(ltp, default=0.0)

    if value <= 0:
        return

    updated_at = updated_at or get_now_market_time().isoformat()

    with _touch_lock:
        _latest_main_index_ltp = value
        _latest_main_index_ltp_source = source
        _latest_main_index_ltp_updated_at = updated_at

    with _opening_range_cache_lock:
        opening_range_cache["latest_main_index_ltp"] = value
        opening_range_cache["latest_main_index_ltp_source"] = source
        opening_range_cache["latest_main_index_ltp_updated_at"] = updated_at


def get_latest_main_index_ltp() -> float | None:
    """Returns latest main index LTP."""

    with _touch_lock:
        return _latest_main_index_ltp


def create_touch_event(
    instrument_key: str,
    level: str,
    level_value: float,
    trigger_price: float,
    trigger_field: str,
    touch_time: str,
    source: str,
    contract_info: dict,
    candle: dict | None = None,
) -> dict:
    """Creates normalized Opening Range R3/S3 touch event."""

    index_ltp = get_latest_main_index_ltp()
    strike_price = contract_info.get("strike_price") if contract_info else None

    distance_from_index = calculate_distance_from_index(
        strike_price=strike_price,
        index_ltp=index_ltp,
    )

    return {
        "type": "opening_range_touch",
        "instrument_key": instrument_key,
        "level": str(level).upper(),
        "level_value": round(safe_float(level_value), 4),
        "trigger_price": round(safe_float(trigger_price), 4),
        "trigger_field": trigger_field,
        "touch_time": touch_time,
        "source": source,
        "date": get_now_market_time().date().isoformat(),
        "main_index_ltp": index_ltp,
        "distance_from_index": distance_from_index,
        "alert_key": build_alert_key(instrument_key, level),
        "contract_info": contract_info or {},
        "candle": serialize_candle(candle) if candle else None,
        "created_at": get_now_market_time().isoformat(),
    }


def should_skip_touch_alert(
    instrument_key: str,
    level: str,
    contract_info: dict | None = None,
) -> bool:
    """Checks whether touch event should be skipped."""

    if not DEFAULT_TOUCH_ALERT_ENABLED:
        return True

    if DEFAULT_TOUCH_ALERT_OPTIONS_ONLY and not is_option_contract(contract_info):
        return True

    alert_key = build_alert_key(instrument_key, level)

    with _touch_lock:
        if DEFAULT_TOUCH_ALERT_ONCE_PER_LEVEL and alert_key in _alert_sent_keys:
            return True

    return False


def mark_touch_alert_sent(event: dict):
    """Marks instrument-level touch as already tracked."""

    alert_key = event.get("alert_key")

    if not alert_key:
        return

    with _touch_lock:
        _alert_sent_keys.add(alert_key)


def queue_touch_event(event: dict):
    """Queues touch event for internal tracking and optional legacy Telegram alert."""

    if not isinstance(event, dict):
        return

    with _touch_lock:
        _touch_events.append(event)

        if DEFAULT_LEGACY_TOUCH_TELEGRAM_ENABLED:
            _pending_touch_events.append(event)

    with _opening_range_cache_lock:
        opening_range_cache["touch_events_count"] = len(_touch_events)
        opening_range_cache["pending_touch_events_count"] = len(_pending_touch_events)
        opening_range_cache["alert_sent_keys_count"] = len(_alert_sent_keys)
        opening_range_cache["touch_events"] = list(_touch_events)


def get_default_touch_status() -> dict:
    """Returns default touch status."""

    return {
        "r3_touched": False,
        "s3_touched": False,
        "r3_touch_time": None,
        "s3_touch_time": None,
        "r3_alert_sent": False,
        "s3_alert_sent": False,
        "first_touch_level": None,
        "first_touch_source": None,
        "first_touch_time": None,
        "events": [],
    }


def build_touch_status_from_events(events: list) -> dict:
    """Builds touch status from detected events."""

    status = get_default_touch_status()

    for event in events:
        level = str(event.get("level", "")).upper()

        if level == "R3":
            status["r3_touched"] = True
            status["r3_touch_time"] = event.get("touch_time")
            status["r3_alert_sent"] = True

        elif level == "S3":
            status["s3_touched"] = True
            status["s3_touch_time"] = event.get("touch_time")
            status["s3_alert_sent"] = True

        if not status.get("first_touch_level"):
            status["first_touch_level"] = level
            status["first_touch_source"] = event.get("source")
            status["first_touch_time"] = event.get("touch_time")

        status.setdefault("events", []).append(event)

    return status


def update_touch_status_in_cache(
    instrument_key: str,
    event: dict,
):
    """Updates touch status for one instrument inside opening_range_cache."""

    level = str(event.get("level", "")).upper()

    with _opening_range_cache_lock:
        data = opening_range_cache.get("data", {})
        item = data.get(instrument_key)

        if not item:
            return

        touch_status = item.setdefault(
            "touch_status",
            get_default_touch_status(),
        )

        if level == "R3":
            touch_status["r3_touched"] = True
            touch_status["r3_touch_time"] = event.get("touch_time")
            touch_status["r3_alert_sent"] = True

        elif level == "S3":
            touch_status["s3_touched"] = True
            touch_status["s3_touch_time"] = event.get("touch_time")
            touch_status["s3_alert_sent"] = True

        if not touch_status.get("first_touch_level"):
            touch_status["first_touch_level"] = level
            touch_status["first_touch_source"] = event.get("source")
            touch_status["first_touch_time"] = event.get("touch_time")

        touch_status.setdefault("events", []).append(event)

        item["touch_status"] = touch_status
        data[instrument_key] = item
        opening_range_cache["data"] = data


def detect_touch_from_candle(
    instrument_key: str,
    candle: dict,
    levels: dict,
    contract_info: dict,
    source: str,
) -> list:
    """Detects R3/S3 touch from intraday candle high/low."""

    if not candle or not levels:
        return []

    events = []

    r3 = safe_float(levels.get("r3"))
    s3 = safe_float(levels.get("s3"))

    candle_high = safe_float(candle.get("high"))
    candle_low = safe_float(candle.get("low"))
    candle_close = safe_float(candle.get("close"))
    candle_time = candle.get("timestamp") or get_now_market_time().isoformat()

    if r3 > 0 and candle_high >= r3:
        if not should_skip_touch_alert(instrument_key, "R3", contract_info):
            events.append(
                create_touch_event(
                    instrument_key=instrument_key,
                    level="R3",
                    level_value=r3,
                    trigger_price=candle_high,
                    trigger_field="high",
                    touch_time=candle_time,
                    source=source,
                    contract_info=contract_info,
                    candle=candle,
                )
            )

    if s3 > 0 and candle_low <= s3:
        if not should_skip_touch_alert(instrument_key, "S3", contract_info):
            events.append(
                create_touch_event(
                    instrument_key=instrument_key,
                    level="S3",
                    level_value=s3,
                    trigger_price=candle_low,
                    trigger_field="low",
                    touch_time=candle_time,
                    source=source,
                    contract_info=contract_info,
                    candle=candle,
                )
            )

    if candle_high <= 0 and candle_low <= 0 and candle_close > 0:
        if r3 > 0 and candle_close >= r3:
            if not should_skip_touch_alert(instrument_key, "R3", contract_info):
                events.append(
                    create_touch_event(
                        instrument_key=instrument_key,
                        level="R3",
                        level_value=r3,
                        trigger_price=candle_close,
                        trigger_field="close",
                        touch_time=candle_time,
                        source=source,
                        contract_info=contract_info,
                        candle=candle,
                    )
                )

        if s3 > 0 and candle_close <= s3:
            if not should_skip_touch_alert(instrument_key, "S3", contract_info):
                events.append(
                    create_touch_event(
                        instrument_key=instrument_key,
                        level="S3",
                        level_value=s3,
                        trigger_price=candle_close,
                        trigger_field="close",
                        touch_time=candle_time,
                        source=source,
                        contract_info=contract_info,
                        candle=candle,
                    )
                )

    return events


def scan_backfill_touches(
    instrument_key: str,
    candles: list,
    levels: dict,
    contract_info: dict,
    candle_count: int,
) -> list:
    """Scans post-OR intraday candles for already touched R3/S3."""

    if not DEFAULT_BACKFILL_SCAN_ENABLED:
        return []

    post_or_candles = select_post_opening_range_candles(
        candles=candles,
        candle_count=candle_count,
    )

    events = []

    for candle in post_or_candles:
        detected = detect_touch_from_candle(
            instrument_key=instrument_key,
            candle=candle,
            levels=levels,
            contract_info=contract_info,
            source="intraday_backfill_scan",
        )

        for event in detected:
            events.append(event)

            if DEFAULT_TOUCH_ALERT_ONCE_PER_LEVEL:
                mark_touch_alert_sent(event)

            update_touch_status_in_cache(instrument_key, event)

    return events


def extract_feed_values(tick_data: dict) -> dict:
    """Extracts ltp/high/low from Upstox full feed."""

    if not isinstance(tick_data, dict):
        return {}

    raw_feed_obj = tick_data.get("raw_feed", tick_data)
    full_feed = raw_feed_obj.get("fullFeed", raw_feed_obj)
    ff_wrapper = full_feed.get("ff", full_feed)

    ff = (
        ff_wrapper.get("marketFF")
        or ff_wrapper.get("indexFF")
        or full_feed.get("marketFF")
        or full_feed.get("indexFF")
        or full_feed
    )

    ltpc = ff.get("ltpc") or {}
    ltp = safe_float(ltpc.get("ltp"))

    ohlc_list = []

    if "marketOHLC" in ff:
        ohlc_list = ff.get("marketOHLC", {}).get("ohlc", [])
    elif "optionOHLC" in ff:
        ohlc_list = ff.get("optionOHLC", {}).get("ohlc", [])

    latest_i1 = None

    if isinstance(ohlc_list, list):
        matching = [
            item for item in ohlc_list if str(item.get("interval", "")).upper() == "I1"
        ]

        if matching:
            try:
                latest_i1 = max(matching, key=lambda item: int(item.get("ts", 0)))
            except Exception:
                latest_i1 = matching[-1]

    high_value = 0.0
    low_value = 0.0
    close_value = ltp
    timestamp = get_now_market_time().isoformat()

    if latest_i1:
        high_value = safe_float(latest_i1.get("high"))
        low_value = safe_float(latest_i1.get("low"))
        close_value = safe_float(latest_i1.get("close"), default=ltp)

        ts_raw = latest_i1.get("ts")
        parsed_ts = parse_candle_timestamp(ts_raw)

        if parsed_ts:
            timestamp = parsed_ts.isoformat()

    return {
        "ltp": ltp,
        "high": high_value,
        "low": low_value,
        "close": close_value,
        "timestamp": timestamp,
    }


def process_live_tick_for_opening_range(
    instrument_key: str,
    tick_data: dict,
    contract_info: dict | None = None,
) -> list:
    """
    Processes live tick for Opening Range R3/S3 touch detection.

    New behavior:
    - No first touched instrument is selected.
    - No other instruments are ignored after touch.
    - Touch events are tracked for all option instruments.
    - Legacy Telegram touch alert remains disabled unless explicitly enabled.
    """

    if not DEFAULT_LIVE_TOUCH_ALERT_ENABLED:
        return []

    if not instrument_key or not isinstance(tick_data, dict):
        return []

    feed_values = extract_feed_values(tick_data)
    ltp = safe_float(feed_values.get("ltp"))

    if instrument_key == DEFAULT_MAIN_INDEX_KEY and ltp > 0:
        update_latest_main_index_ltp(
            ltp=ltp,
            source="live_tick",
            updated_at=feed_values.get("timestamp"),
        )
        return []

    if not contract_info:
        contract_info = get_contract_info_by_key(instrument_key)

    if DEFAULT_TOUCH_ALERT_OPTIONS_ONLY and not is_option_contract(contract_info):
        return []

    with _opening_range_cache_lock:
        item = opening_range_cache.get("data", {}).get(instrument_key)

    if not item:
        return []

    if item.get("status") != "success":
        return []

    levels = item.get("levels") or {}

    if not levels:
        return []

    r3 = safe_float(levels.get("r3"))
    s3 = safe_float(levels.get("s3"))

    high_value = safe_float(feed_values.get("high"))
    low_value = safe_float(feed_values.get("low"))
    close_value = safe_float(feed_values.get("close"), default=ltp)
    event_time = feed_values.get("timestamp") or get_now_market_time().isoformat()

    events = []

    if DEFAULT_TOUCH_CHECK_MODE == "ltp":
        r3_trigger = ltp
        s3_trigger = ltp
        r3_condition = r3 > 0 and ltp >= r3
        s3_condition = s3 > 0 and ltp <= s3
        r3_field = "ltp"
        s3_field = "ltp"
    else:
        r3_trigger = high_value if high_value > 0 else close_value
        s3_trigger = low_value if low_value > 0 else close_value
        r3_condition = r3 > 0 and r3_trigger >= r3
        s3_condition = s3 > 0 and s3_trigger <= s3
        r3_field = "high" if high_value > 0 else "close"
        s3_field = "low" if low_value > 0 else "close"

    if r3_condition:
        if not should_skip_touch_alert(instrument_key, "R3", contract_info):
            events.append(
                create_touch_event(
                    instrument_key=instrument_key,
                    level="R3",
                    level_value=r3,
                    trigger_price=r3_trigger,
                    trigger_field=r3_field,
                    touch_time=event_time,
                    source="live_tick",
                    contract_info=contract_info,
                )
            )

    if s3_condition:
        if not should_skip_touch_alert(instrument_key, "S3", contract_info):
            events.append(
                create_touch_event(
                    instrument_key=instrument_key,
                    level="S3",
                    level_value=s3,
                    trigger_price=s3_trigger,
                    trigger_field=s3_field,
                    touch_time=event_time,
                    source="live_tick",
                    contract_info=contract_info,
                )
            )

    for event in events:
        if DEFAULT_TOUCH_ALERT_ONCE_PER_LEVEL:
            mark_touch_alert_sent(event)

        update_touch_status_in_cache(instrument_key, event)
        queue_touch_event(event)

    return events


# ============================================================
# Legacy Telegram Touch Alert Helpers
# ============================================================


def get_sorted_touch_events_for_alert(events: list) -> list:
    """Sorts touch events by distance from latest NIFTY index LTP."""

    if not events:
        return []

    if not DEFAULT_SORT_BY_NEAREST_INDEX:
        return events

    def sort_key(event):
        distance = event.get("distance_from_index")

        if distance is None:
            return 999999999

        return float(distance)

    return sorted(events, key=sort_key)


def format_touch_event_line(index: int, event: dict) -> str:
    """Formats one touch event for Telegram."""

    info = event.get("contract_info") or {}

    strike = info.get("strike_price", "N/A")
    itype = str(info.get("instrument_type", "N/A")).upper()
    symbol = (
        info.get("trading_symbol")
        or info.get("instrument_key")
        or event.get("instrument_key")
    )

    level = event.get("level")
    level_value = event.get("level_value")
    trigger_price = event.get("trigger_price")
    trigger_field = event.get("trigger_field")
    touch_time = event.get("touch_time")
    distance = event.get("distance_from_index")

    return (
        f"{index}. {strike} {itype}\n"
        f"   Symbol: {symbol}\n"
        f"   Level: {level}\n"
        f"   Level Value: {level_value}\n"
        f"   Trigger {trigger_field}: {trigger_price}\n"
        f"   Touch Time: {touch_time}\n"
        f"   Distance From Index: {distance}"
    )


def send_touch_events_telegram_alert(
    events: list,
    source: str,
    force: bool = False,
) -> bool:
    """
    Sends legacy Telegram alert for max top nearest touch events.

    New requirement keeps this disabled by default.
    """

    global _last_touch_alert_sent_at

    if not DEFAULT_LEGACY_TOUCH_TELEGRAM_ENABLED:
        return False

    if not DEFAULT_TOUCH_ALERT_ENABLED:
        return False

    if not events:
        return False

    now_ts = time.time()

    if not force and _last_touch_alert_sent_at is not None:
        elapsed = now_ts - _last_touch_alert_sent_at

        if elapsed < DEFAULT_TOUCH_ALERT_BATCH_SECONDS:
            return False

    sorted_events = get_sorted_touch_events_for_alert(events)
    selected_events = sorted_events[:DEFAULT_TOUCH_ALERT_MAX_INSTRUMENTS]

    index_ltp = get_latest_main_index_ltp()

    lines = [
        format_touch_event_line(index + 1, event)
        for index, event in enumerate(selected_events)
    ]

    message = (
        f"Source: {source}\n"
        f"NIFTY LTP: {index_ltp if index_ltp is not None else 'not_available'}\n"
        f"Total Touched Instruments: {len(events)}\n"
        f"Alerted Instruments: {len(selected_events)}\n\n" + "\n\n".join(lines)
    )

    sent = telegram_service.send_message(
        title="Opening Range R3/S3 Touch Alert",
        message=message,
        level="REFRESH",
    )

    if sent:
        _last_touch_alert_sent_at = now_ts

    return sent


def flush_pending_touch_alerts(force: bool = False, source: str = "live_tick") -> bool:
    """
    Flushes pending live touch events into one Telegram message.

    This sends only if OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED=True.
    """

    if not DEFAULT_LEGACY_TOUCH_TELEGRAM_ENABLED:
        return False

    with _touch_lock:
        pending = list(_pending_touch_events)
        _pending_touch_events.clear()

    if not pending:
        return False

    sent = send_touch_events_telegram_alert(
        events=pending,
        source=source,
        force=force,
    )

    if not sent:
        with _touch_lock:
            for event in pending:
                _pending_touch_events.appendleft(event)

    with _opening_range_cache_lock:
        opening_range_cache["pending_touch_events_count"] = len(_pending_touch_events)

    return sent


# ============================================================
# Disabled Selected OR Compatibility Helpers
# ============================================================


def is_selected_or_instrument_locked() -> bool:
    """Selected OR flow is disabled."""

    return False


def get_selected_or_instrument_key() -> str | None:
    """Selected OR flow is disabled."""

    return None


def get_selected_or_instrument_state() -> dict:
    """Returns disabled selected OR state for backward compatibility."""

    with _selected_or_lock:
        return dict(_selected_or_instrument_state)


def get_selected_or_ema_alerts(limit: int = 100) -> list:
    """Selected OR EMA alerts are disabled."""

    return []


def process_selected_or_ema_cross_alert(ema_event: dict) -> bool:
    """
    Selected OR EMA Telegram alert is disabled.

    Kept only so older imports do not break during transition.
    """

    return False


# ============================================================
# EMA WebSocket Opening Range Enrichment Helper
# ============================================================


def get_opening_range_levels_for_ema_event(instrument_key: str) -> dict:
    """
    Returns lightweight Opening Range payload for EMA crossover events.

    Output:

    {
        "opening_range": {...},
        "touch_status": {...},
        "latest_intraday_close": ...,
        "latest_main_index_ltp": ...,
        "processed_at": ...
    }
    """

    if not DEFAULT_EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS:
        return {
            "opening_range": {},
            "touch_status": get_default_touch_status(),
            "latest_intraday_close": None,
            "latest_main_index_ltp": None,
            "processed_at": None,
        }

    if not instrument_key:
        return {
            "opening_range": {},
            "touch_status": get_default_touch_status(),
            "latest_intraday_close": None,
            "latest_main_index_ltp": None,
            "processed_at": None,
        }

    with _opening_range_cache_lock:
        item = opening_range_cache.get("data", {}).get(instrument_key)
        latest_main_index_ltp = opening_range_cache.get("latest_main_index_ltp")

    if not item:
        return {
            "opening_range": {},
            "touch_status": get_default_touch_status(),
            "latest_intraday_close": None,
            "latest_main_index_ltp": latest_main_index_ltp,
            "processed_at": None,
        }

    levels = item.get("levels") or {}

    compact_levels = {
        "r1": levels.get("r1"),
        "s1": levels.get("s1"),
        "r2": levels.get("r2"),
        "s2": levels.get("s2"),
        "r3": levels.get("r3"),
        "s3": levels.get("s3"),
        "sub_resistance": levels.get("sub_resistance"),
        "sub_support": levels.get("sub_support"),
    }

    return {
        "opening_range": compact_levels,
        "touch_status": item.get(
            "touch_status",
            get_default_touch_status(),
        ),
        "latest_intraday_close": item.get("latest_intraday_close"),
        "latest_main_index_ltp": latest_main_index_ltp,
        "processed_at": item.get("processed_at"),
    }


# ============================================================
# Storage Helpers
# ============================================================


def save_touch_events_to_file_if_enabled():
    """Saves touch events to file only if TEST_FLAG=True."""

    if not bool(getattr(config, "TEST_FLAG", False)):
        return None

    if not DEFAULT_TOUCH_EVENTS_SAVE_TEST_FILE:
        return None

    try:
        file_path = Path(DEFAULT_TOUCH_EVENTS_OUTPUT_FILE)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "generated_at": get_now_market_time().isoformat(),
            "events_count": len(_touch_events),
            "events": list(_touch_events),
            "selected_or_instrument": get_selected_or_instrument_state(),
            "selected_or_ema_alerts_count": 0,
            "selected_or_ema_alerts": [],
        }

        with open(file_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=4, default=str)

        return str(file_path)

    except Exception as ex:
        logger.error(
            f"Failed saving opening range touch events: {type(ex).__name__}: {ex}"
        )
        return None


def save_opening_range_results_to_file(
    summary: dict,
    output_file: str = DEFAULT_OUTPUT_FILE,
) -> str:
    """Saves opening range summary to JSON file."""

    file_path = Path(output_file)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    with open(file_path, "w", encoding="utf-8") as file:
        json.dump(summary, file, indent=4, default=str)

    return str(file_path)


# ============================================================
# Intraday Fetch
# ============================================================


def fetch_intraday_candles_for_instrument(
    instrument_key: str,
    unit: str = DEFAULT_INTRADAY_UNIT,
    interval: str = DEFAULT_INTRADAY_INTERVAL,
) -> dict:
    """Fetches today's intraday candles using Upstox HistoryV3Api."""

    try:
        api_instance = upstox_client.HistoryV3Api()

        logger.info(
            f"Opening range intraday request: "
            f"instrument_key={instrument_key}, unit={unit}, interval={interval}"
        )

        api_response = api_instance.get_intra_day_candle_data(
            instrument_key,
            unit,
            interval,
        )

        raw_candles = extract_candles_from_response(api_response)
        normalized_candles = normalize_candles(raw_candles)

        logger.info(
            f"Opening range intraday completed: "
            f"instrument_key={instrument_key}, candles_count={len(normalized_candles)}"
        )

        return {
            "status": "success" if normalized_candles else "empty",
            "instrument_key": instrument_key,
            "unit": unit,
            "interval": interval,
            "candles": normalized_candles,
            "candles_count": len(normalized_candles),
            "error": None,
        }

    except ApiException as ex:
        error_body = getattr(ex, "body", str(ex))

        logger.error(
            f"ApiException in opening range intraday fetch for "
            f"{instrument_key}: {error_body}"
        )

        return {
            "status": "failed",
            "instrument_key": instrument_key,
            "unit": unit,
            "interval": interval,
            "candles": [],
            "candles_count": 0,
            "error": error_body,
        }

    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"

        logger.error(
            f"Exception in opening range intraday fetch for "
            f"{instrument_key}: {error_message}"
        )

        return {
            "status": "failed",
            "instrument_key": instrument_key,
            "unit": unit,
            "interval": interval,
            "candles": [],
            "candles_count": 0,
            "error": error_message,
        }


# ============================================================
# Single Instrument Opening Range
# ============================================================


def calculate_opening_range_for_instrument(
    instrument_key: str,
    candle_count: int = DEFAULT_OPENING_RANGE_CANDLE_COUNT,
) -> dict:
    """
    Fetches intraday candles for one instrument and calculates opening range levels.
    Also scans post-OR candles for already touched R3/S3 edge case.
    """

    processed_at = get_now_market_time().isoformat()
    contract_info = get_contract_info_by_key(instrument_key)

    intraday_result = fetch_intraday_candles_for_instrument(
        instrument_key=instrument_key,
        unit=DEFAULT_INTRADAY_UNIT,
        interval=DEFAULT_INTRADAY_INTERVAL,
    )

    base_payload = {
        "instrument_key": instrument_key,
        "source": "intraday_api",
        "date": get_now_market_time().date().isoformat(),
        "interval": DEFAULT_OPENING_RANGE_INTERVAL,
        "unit": DEFAULT_INTRADAY_UNIT,
        "intraday_interval": DEFAULT_INTRADAY_INTERVAL,
        "opening_range_candle_count": candle_count,
        "contract_info": contract_info,
        "processed_at": processed_at,
    }

    if intraday_result.get("status") == "failed":
        return {
            **base_payload,
            "status": "failed",
            "message": "Intraday candle fetch failed.",
            "candles_count": 0,
            "selected_candles_count": 0,
            "latest_intraday_close": None,
            "range": None,
            "levels": None,
            "selected_candles": [],
            "post_or_candles_count": 0,
            "touch_status": get_default_touch_status(),
            "backfill_touch_events": [],
            "error": intraday_result.get("error"),
        }

    candles = intraday_result.get("candles", [])

    if not candles:
        return {
            **base_payload,
            "status": "empty",
            "message": "No intraday candles returned.",
            "candles_count": 0,
            "selected_candles_count": 0,
            "latest_intraday_close": None,
            "range": None,
            "levels": None,
            "selected_candles": [],
            "post_or_candles_count": 0,
            "touch_status": get_default_touch_status(),
            "backfill_touch_events": [],
            "error": None,
        }

    latest_intraday_close = safe_float(candles[-1].get("close"))

    if instrument_key == DEFAULT_MAIN_INDEX_KEY and latest_intraday_close > 0:
        update_latest_main_index_ltp(
            ltp=latest_intraday_close,
            source="intraday_api",
            updated_at=candles[-1].get("timestamp"),
        )

    selected_candles = select_opening_range_candles(
        candles=candles,
        candle_count=candle_count,
    )

    if len(selected_candles) < int(candle_count):
        return {
            **base_payload,
            "status": "insufficient_data",
            "message": (
                f"Need {candle_count} opening range candles, "
                f"but only {len(selected_candles)} available."
            ),
            "candles_count": len(candles),
            "selected_candles_count": len(selected_candles),
            "latest_intraday_close": latest_intraday_close,
            "range": None,
            "levels": None,
            "selected_candles": [serialize_candle(item) for item in selected_candles],
            "post_or_candles_count": 0,
            "touch_status": get_default_touch_status(),
            "backfill_touch_events": [],
            "error": None,
        }

    calculation = calculate_opening_range_levels(selected_candles)
    levels = calculation.get("levels") or {}

    post_or_candles = select_post_opening_range_candles(
        candles=candles,
        candle_count=candle_count,
    )

    backfill_touch_events = []

    if calculation.get("status") == "success":
        backfill_touch_events = scan_backfill_touches(
            instrument_key=instrument_key,
            candles=candles,
            levels=levels,
            contract_info=contract_info,
            candle_count=candle_count,
        )

    touch_status = build_touch_status_from_events(backfill_touch_events)

    return {
        **base_payload,
        "status": calculation.get("status"),
        "message": calculation.get("message"),
        "candles_count": len(candles),
        "selected_candles_count": len(selected_candles),
        "latest_intraday_close": latest_intraday_close,
        "range": calculation.get("range"),
        "levels": levels,
        "selected_candles": [serialize_candle(item) for item in selected_candles],
        "post_or_candles_count": len(post_or_candles),
        "touch_status": touch_status,
        "backfill_touch_events": backfill_touch_events,
        "error": None,
    }


# ============================================================
# All Subscribed Instruments Opening Range
# ============================================================


def calculate_opening_range_for_all_subscribed(
    candle_count: int = DEFAULT_OPENING_RANGE_CANDLE_COUNT,
    save_data: bool = DEFAULT_SAVE_FILE,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict:
    """Fetches intraday candles and calculates opening range levels for all subscribed instruments."""

    if not is_opening_range_enabled():
        logger.info("Opening range calculation skipped because it is disabled.")

        return {
            "status": "disabled",
            "message": "Opening range calculation is disabled.",
            "total_instruments": 0,
            "success_count": 0,
            "failed_count": 0,
            "empty_count": 0,
            "insufficient_data_count": 0,
        }

    subscribed_keys = get_subscribed_instrument_keys()

    if not subscribed_keys:
        logger.warning(
            "Opening range calculation skipped. No subscribed instruments found."
        )

        return {
            "status": "skipped",
            "message": "No subscribed instruments found.",
            "total_instruments": 0,
            "success_count": 0,
            "failed_count": 0,
            "empty_count": 0,
            "insufficient_data_count": 0,
        }

    try:
        candle_count = max(1, int(candle_count))
    except Exception:
        candle_count = DEFAULT_OPENING_RANGE_CANDLE_COUNT

    try:
        max_workers = max(1, int(max_workers))
    except Exception:
        max_workers = DEFAULT_MAX_WORKERS

    now_market = get_now_market_time()
    started_at = now_market.isoformat()
    current_date = now_market.date().isoformat()

    logger.info(
        "================ OPENING RANGE INTRADAY FETCH STARTED ================"
    )

    logger.info(
        f"Calculating opening range for {len(subscribed_keys)} instruments. "
        f"date={current_date}, candle_count={candle_count}, "
        f"interval={DEFAULT_OPENING_RANGE_INTERVAL}, "
        f"unit={DEFAULT_INTRADAY_UNIT}, intraday_interval={DEFAULT_INTRADAY_INTERVAL}, "
        f"market_open={DEFAULT_MARKET_OPEN_HOUR:02d}:{DEFAULT_MARKET_OPEN_MINUTE:02d}, "
        f"max_workers={max_workers}"
    )

    results = {}
    errors = {}

    success_count = 0
    failed_count = 0
    empty_count = 0
    insufficient_data_count = 0

    total_backfill_touch_events = 0
    backfill_touch_events = []

    completed_count = 0
    total_instruments = len(subscribed_keys)

    def worker(instrument_key: str) -> dict:
        result = calculate_opening_range_for_instrument(
            instrument_key=instrument_key,
            candle_count=candle_count,
        )

        if DEFAULT_SLEEP_SECONDS and DEFAULT_SLEEP_SECONDS > 0:
            time.sleep(DEFAULT_SLEEP_SECONDS)

        return result

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_instrument = {
            executor.submit(worker, instrument_key): instrument_key
            for instrument_key in subscribed_keys
        }

        for future in as_completed(future_to_instrument):
            instrument_key = future_to_instrument[future]
            completed_count += 1

            logger.info(
                f"Opening range progress: {completed_count}/{total_instruments} "
                f"instrument_key={instrument_key}"
            )

            try:
                result = future.result()
                result_status = result.get("status")

                results[instrument_key] = result

                instrument_backfill_events = result.get("backfill_touch_events", [])

                if instrument_backfill_events:
                    total_backfill_touch_events += len(instrument_backfill_events)
                    backfill_touch_events.extend(instrument_backfill_events)

                    for event in instrument_backfill_events:
                        queue_touch_event(event)

                if result_status == "success":
                    success_count += 1
                elif result_status == "empty":
                    empty_count += 1
                elif result_status == "insufficient_data":
                    insufficient_data_count += 1
                else:
                    failed_count += 1
                    errors[instrument_key] = result.get("error") or result.get(
                        "message"
                    )

            except Exception as ex:
                error_message = f"{type(ex).__name__}: {ex}"

                logger.error(
                    f"Opening range calculation failed for "
                    f"instrument_key={instrument_key}: {error_message}"
                )

                failed_count += 1
                errors[instrument_key] = error_message

                results[instrument_key] = {
                    "instrument_key": instrument_key,
                    "status": "failed",
                    "message": "Opening range worker failed.",
                    "source": "intraday_api",
                    "date": current_date,
                    "interval": DEFAULT_OPENING_RANGE_INTERVAL,
                    "opening_range_candle_count": candle_count,
                    "latest_intraday_close": None,
                    "range": None,
                    "levels": None,
                    "touch_status": get_default_touch_status(),
                    "backfill_touch_events": [],
                    "error": error_message,
                    "contract_info": get_contract_info_by_key(instrument_key),
                    "processed_at": get_now_market_time().isoformat(),
                }

    completed_at = get_now_market_time().isoformat()

    if failed_count == 0:
        overall_status = "success"
    elif success_count > 0 or empty_count > 0 or insufficient_data_count > 0:
        overall_status = "partial_success"
    else:
        overall_status = "failed"

    summary = {
        "status": overall_status,
        "message": "Opening range calculation completed.",
        "source": "intraday_api",
        "date": current_date,
        "started_at": started_at,
        "completed_at": completed_at,
        "interval": DEFAULT_OPENING_RANGE_INTERVAL,
        "unit": DEFAULT_INTRADAY_UNIT,
        "intraday_interval": DEFAULT_INTRADAY_INTERVAL,
        "opening_range_candle_count": candle_count,
        "market_open_time": f"{DEFAULT_MARKET_OPEN_HOUR:02d}:{DEFAULT_MARKET_OPEN_MINUTE:02d}",
        "opening_range_end_time": get_opening_range_end_datetime(
            candle_count=candle_count
        ).strftime("%H:%M"),
        "scheduled_fetch_time": f"{DEFAULT_FETCH_HOUR:02d}:{DEFAULT_FETCH_MINUTE:02d}",
        "max_workers": max_workers,
        "total_instruments": total_instruments,
        "success_count": success_count,
        "failed_count": failed_count,
        "empty_count": empty_count,
        "insufficient_data_count": insufficient_data_count,
        "backfill_touch_scan_enabled": DEFAULT_BACKFILL_SCAN_ENABLED,
        "backfill_touch_events_count": total_backfill_touch_events,
        "latest_main_index_ltp": get_latest_main_index_ltp(),
        "selected_or_instrument": get_selected_or_instrument_state(),
        "selected_or_ema_alerts_count": 0,
        "results": results,
        "backfill_touch_events": backfill_touch_events,
        "errors": errors,
    }

    output_file_path = None

    if save_data:
        try:
            output_file_path = save_opening_range_results_to_file(summary)
            summary["output_file_path"] = output_file_path

            logger.info(f"Saved opening range results to {output_file_path}")

        except Exception as ex:
            error_message = f"{type(ex).__name__}: {ex}"

            logger.error(f"Failed saving opening range results: {error_message}")

            summary["output_file_error"] = error_message

    else:
        logger.info("Opening range result file not saved because save_data=False.")

    with _opening_range_cache_lock:
        opening_range_cache["last_run_at"] = completed_at
        opening_range_cache["date"] = current_date
        opening_range_cache["status"] = overall_status
        opening_range_cache["message"] = summary.get("message")
        opening_range_cache["source"] = "intraday_api"
        opening_range_cache["interval"] = DEFAULT_OPENING_RANGE_INTERVAL
        opening_range_cache["opening_range_candle_count"] = candle_count
        opening_range_cache["market_open_time"] = summary.get("market_open_time")
        opening_range_cache["fetch_time"] = summary.get("scheduled_fetch_time")
        opening_range_cache["total_instruments"] = total_instruments
        opening_range_cache["success_count"] = success_count
        opening_range_cache["failed_count"] = failed_count
        opening_range_cache["empty_count"] = empty_count
        opening_range_cache["insufficient_data_count"] = insufficient_data_count
        opening_range_cache["output_file_path"] = output_file_path
        opening_range_cache["latest_main_index_ltp"] = get_latest_main_index_ltp()
        opening_range_cache["latest_main_index_ltp_source"] = (
            _latest_main_index_ltp_source
        )
        opening_range_cache["latest_main_index_ltp_updated_at"] = (
            _latest_main_index_ltp_updated_at
        )
        opening_range_cache["touch_events_count"] = len(_touch_events)
        opening_range_cache["pending_touch_events_count"] = len(_pending_touch_events)
        opening_range_cache["alert_sent_keys_count"] = len(_alert_sent_keys)
        opening_range_cache["data"] = results
        opening_range_cache["touch_events"] = list(_touch_events)
        opening_range_cache["errors"] = errors

    if (
        DEFAULT_BACKFILL_TOUCH_ALERT_ENABLED
        and DEFAULT_TOUCH_ALERT_ENABLED
        and DEFAULT_LEGACY_TOUCH_TELEGRAM_ENABLED
        and backfill_touch_events
    ):
        send_touch_events_telegram_alert(
            events=backfill_touch_events,
            source="intraday_backfill_scan",
            force=True,
        )

    save_touch_events_to_file_if_enabled()

    logger.info(
        f"Opening range calculation completed. "
        f"status={overall_status}, "
        f"total_instruments={total_instruments}, "
        f"success={success_count}, "
        f"empty={empty_count}, "
        f"insufficient_data={insufficient_data_count}, "
        f"failed={failed_count}, "
        f"backfill_touch_events={total_backfill_touch_events}, "
        f"output_file={output_file_path}"
    )

    logger.info(
        "================ OPENING RANGE INTRADAY FETCH COMPLETED ================"
    )

    return summary


# ============================================================
# Status Helpers
# ============================================================


def get_opening_range_status() -> dict:
    """Returns latest opening range status summary."""

    with _opening_range_cache_lock:
        return {
            "last_run_at": opening_range_cache.get("last_run_at"),
            "date": opening_range_cache.get("date"),
            "status": opening_range_cache.get("status"),
            "message": opening_range_cache.get("message"),
            "source": opening_range_cache.get("source"),
            "interval": opening_range_cache.get("interval"),
            "opening_range_candle_count": opening_range_cache.get(
                "opening_range_candle_count"
            ),
            "market_open_time": opening_range_cache.get("market_open_time"),
            "fetch_time": opening_range_cache.get("fetch_time"),
            "total_instruments": opening_range_cache.get("total_instruments"),
            "success_count": opening_range_cache.get("success_count"),
            "failed_count": opening_range_cache.get("failed_count"),
            "empty_count": opening_range_cache.get("empty_count"),
            "insufficient_data_count": opening_range_cache.get(
                "insufficient_data_count"
            ),
            "output_file_path": opening_range_cache.get("output_file_path"),
            "latest_main_index_ltp": opening_range_cache.get("latest_main_index_ltp"),
            "latest_main_index_ltp_source": opening_range_cache.get(
                "latest_main_index_ltp_source"
            ),
            "latest_main_index_ltp_updated_at": opening_range_cache.get(
                "latest_main_index_ltp_updated_at"
            ),
            "touch_events_count": opening_range_cache.get("touch_events_count"),
            "pending_touch_events_count": opening_range_cache.get(
                "pending_touch_events_count"
            ),
            "alert_sent_keys_count": opening_range_cache.get("alert_sent_keys_count"),
            "selected_or_instrument": get_selected_or_instrument_state(),
            "selected_or_ema_alerts_count": 0,
            "ema_cross_include_opening_range_levels": (
                DEFAULT_EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS
            ),
            "errors": opening_range_cache.get("errors", {}),
        }


def get_opening_range_cache() -> dict:
    """Returns full opening range cache."""

    with _opening_range_cache_lock:
        return dict(opening_range_cache)


def get_opening_range_for_instrument_from_cache(
    instrument_key: str,
) -> dict | None:
    """Returns opening range result for one instrument from cache."""

    with _opening_range_cache_lock:
        data = opening_range_cache.get("data", {})
        return data.get(instrument_key)


def get_opening_range_touch_events(limit: int = 100) -> list:
    """Returns latest opening range touch events."""

    limit = max(1, int(limit or 100))

    with _touch_lock:
        return list(_touch_events)[-limit:]


def get_opening_range_pending_touch_events() -> list:
    """Returns pending touch events."""

    with _touch_lock:
        return list(_pending_touch_events)
