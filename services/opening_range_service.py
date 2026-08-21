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
from services.option_service import (
    options_cache,
    get_contract_info_by_instrument_key,
    get_nearest_order_instruments_for_ema_cross,
    get_budget_order_instruments_for_ema_cross,
    update_contract_live_ltp,
)

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
    "isolated_instrument": None,
    "isolated_instrument_selected": False,
    "isolated_instrument_selected_at": None,
    "isolated_instrument_selection_reason": None,
    "isolated_ema_alerts_count": 0,
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

# Latest live LTP by instrument is used for Telegram nearest order details.
_latest_ltp_by_instrument = {}
_latest_ltp_updated_at_by_instrument = {}


# ============================================================
# Isolated Instrument State
# ============================================================

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
    "selection_priority": None,
    "selection_reason": None,
    "reference_average": None,
    "average_window": None,
    "contract_info": None,
    "range": None,
    "levels": None,
    "latest_live_data": None,
    "latest_main_index_ltp": None,
    "ema_alerts_count": 0,
    "last_ema_alert": None,
    "disabled": False,
    "message": "No isolated Opening Range instrument selected yet.",
}

_selected_or_ema_alerts = deque(
    maxlen=int(getattr(config, "OPENING_RANGE_MAX_EVENTS_IN_MEMORY", 5000))
)
_selected_or_ema_alert_minute_keys = set()
_selected_or_ema_alert_minute_date = None


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

DEFAULT_INTRADAY_UNIT = getattr(config, "OPENING_RANGE_INTRADAY_UNIT", "minutes")
DEFAULT_INTRADAY_INTERVAL = getattr(config, "OPENING_RANGE_INTRADAY_INTERVAL", "1")

DEFAULT_MAX_WORKERS = int(getattr(config, "OPENING_RANGE_MAX_WORKERS", 5))
DEFAULT_SLEEP_SECONDS = float(
    getattr(config, "OPENING_RANGE_REQUEST_SLEEP_SECONDS", 0.15)
)

DEFAULT_SAVE_FILE = bool(getattr(config, "OPENING_RANGE_SAVE_FILE", True))
DEFAULT_OUTPUT_FILE = getattr(
    config, "OPENING_RANGE_OUTPUT_FILE", "data/opening_range_results.json"
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

DEFAULT_TOUCH_CHECK_MODE = getattr(config, "OPENING_RANGE_TOUCH_CHECK_MODE", "high_low")

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

DEFAULT_ISOLATION_ENABLED = bool(
    getattr(config, "OPENING_RANGE_ISOLATED_INSTRUMENT_ENABLED", True)
)

DEFAULT_ISOLATION_WINDOW_POINTS = float(
    getattr(config, "OPENING_RANGE_ISOLATION_AVERAGE_WINDOW_POINTS", 500.0)
)

DEFAULT_ISOLATION_TOUCH_LEVELS = [
    str(item).upper()
    for item in getattr(
        config,
        "OPENING_RANGE_ISOLATION_TOUCH_LEVELS",
        ["S3", "R3", "S2", "R2"],
    )
]

DEFAULT_ISOLATION_PRIORITY_LEVELS = [
    str(item).upper()
    for item in getattr(
        config,
        "OPENING_RANGE_ISOLATION_PRIORITY_LEVELS",
        ["S3", "R3", "S2", "R2"],
    )
]

DEFAULT_ISOLATION_LOCK_FOR_DAY = bool(
    getattr(config, "OPENING_RANGE_ISOLATION_LOCK_FOR_DAY", True)
)

DEFAULT_ISOLATION_ALLOW_PRIORITY_UPGRADE = bool(
    getattr(config, "OPENING_RANGE_ISOLATION_ALLOW_PRIORITY_UPGRADE", True)
)

DEFAULT_ISOLATED_NOTIFY_ENABLED = bool(
    getattr(config, "OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED", True)
)

DEFAULT_ISOLATION_ALLOW_BACKFILL_TOUCH = bool(
    getattr(config, "OPENING_RANGE_ISOLATION_ALLOW_BACKFILL_TOUCH", True)
)

DEFAULT_ISOLATION_ALLOW_LIVE_TOUCH = bool(
    getattr(config, "OPENING_RANGE_ISOLATION_ALLOW_LIVE_TOUCH", True)
)

DEFAULT_ISOLATION_OPTIONS_ONLY = bool(
    getattr(config, "OPENING_RANGE_ISOLATION_OPTIONS_ONLY", True)
)

DEFAULT_EMA_ISOLATED_TELEGRAM_ENABLED = bool(
    getattr(config, "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED", True)
)

DEFAULT_LIVE_EMA_CALCULATION_MODE = bool(
    getattr(config, "LIVE_EMA_CALCULATION_MODE", False)
)


# ============================================================
# Basic Helpers
# ============================================================


def is_opening_range_enabled() -> bool:
    """Returns whether opening range calculation is enabled."""
    return bool(getattr(config, "OPENING_RANGE_ENABLED", True))


def get_market_timezone():
    """Loads market timezone from config."""
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


def get_live_ema_calculation_mode_text() -> str:
    """
    Returns configured live EMA calculation mode.

    LIVE_EMA_CALCULATION_MODE = False
        completed candle close based EMA calculation.

    LIVE_EMA_CALCULATION_MODE = True
        live tick/LTP based EMA calculation.
    """
    return "tick_ltp" if DEFAULT_LIVE_EMA_CALCULATION_MODE else "candle_close"


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
            return datetime.fromtimestamp(int(timestamp_value) / 1000, tz=market_tz)

        text = str(timestamp_value).strip()

        if not text:
            return None

        if text.isdigit():
            return datetime.fromtimestamp(int(text) / 1000, tz=market_tz)

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

    resolved = get_contract_info_by_instrument_key(instrument_key)

    if resolved:
        return resolved

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


def normalize_option_type(value) -> str | None:
    """Normalizes option type to CE or PE."""
    text = str(value or "").strip().upper()

    if text in ["CE", "CALL"]:
        return "CE"

    if text in ["PE", "PUT"]:
        return "PE"

    return None


def get_isolated_role_from_level(level: str) -> str:
    """
    Returns isolated instrument role based on selected level.

    S2/S3 -> SUPPORT
    R2/R3 -> RESISTANCE
    """
    level_upper = str(level or "").strip().upper()

    support_levels = [
        str(item).upper()
        for item in getattr(
            config,
            "OPENING_RANGE_ISOLATED_SUPPORT_LEVELS",
            ["S2", "S3"],
        )
    ]

    resistance_levels = [
        str(item).upper()
        for item in getattr(
            config,
            "OPENING_RANGE_ISOLATED_RESISTANCE_LEVELS",
            ["R2", "R3"],
        )
    ]

    if level_upper in support_levels:
        return getattr(config, "OPENING_RANGE_ISOLATED_SUPPORT_ROLE_TEXT", "SUPPORT")

    if level_upper in resistance_levels:
        return getattr(
            config,
            "OPENING_RANGE_ISOLATED_RESISTANCE_ROLE_TEXT",
            "RESISTANCE",
        )

    return "not_available"


def update_latest_ltp_for_instrument(
    instrument_key: str,
    ltp,
    updated_at: str | None = None,
):
    """
    Stores latest live LTP by instrument for Telegram suggested instruments.

    Also updates option_service.options_cache so budget range instrument lookup can
    find instruments by live_ltp.
    """
    if not instrument_key:
        return

    value = safe_float(ltp)

    if value <= 0:
        return

    updated_at = updated_at or get_now_market_time().isoformat()

    with _touch_lock:
        _latest_ltp_by_instrument[instrument_key] = value
        _latest_ltp_updated_at_by_instrument[instrument_key] = updated_at

    try:
        update_contract_live_ltp(instrument_key, value)
    except Exception as ex:
        logger.warning(
            f"Failed updating option_service live LTP cache. "
            f"instrument_key={instrument_key}, error={type(ex).__name__}: {ex}"
        )


def get_latest_ltp_for_instrument(instrument_key: str) -> float | None:
    """Returns latest cached live LTP for an instrument."""
    if not instrument_key:
        return None

    with _touch_lock:
        return _latest_ltp_by_instrument.get(instrument_key)


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


def get_default_touch_status() -> dict:
    """Returns default touch status."""
    return {
        "r2_touched": False,
        "s2_touched": False,
        "r3_touched": False,
        "s3_touched": False,
        "r2_touch_time": None,
        "s2_touch_time": None,
        "r3_touch_time": None,
        "s3_touch_time": None,
        "r2_alert_sent": False,
        "s2_alert_sent": False,
        "r3_alert_sent": False,
        "s3_alert_sent": False,
        "first_touch_level": None,
        "first_touch_source": None,
        "first_touch_time": None,
        "events": [],
    }


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
    """Creates normalized Opening Range R2/R3/S2/S3 touch event."""
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

    level_upper = str(level).upper()

    if level_upper not in DEFAULT_ISOLATION_TOUCH_LEVELS:
        return True

    alert_key = build_alert_key(instrument_key, level_upper)

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


def build_touch_status_from_events(events: list) -> dict:
    """Builds touch status from detected events."""
    status = get_default_touch_status()

    for event in events:
        level = str(event.get("level", "")).upper()
        lower_level = level.lower()

        if level in ["R2", "S2", "R3", "S3"]:
            status[f"{lower_level}_touched"] = True
            status[f"{lower_level}_touch_time"] = event.get("touch_time")
            status[f"{lower_level}_alert_sent"] = True

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
    lower_level = level.lower()

    with _opening_range_cache_lock:
        data = opening_range_cache.get("data", {})
        item = data.get(instrument_key)

        if not item:
            return

        touch_status = item.setdefault(
            "touch_status",
            get_default_touch_status(),
        )

        if level in ["R2", "S2", "R3", "S3"]:
            touch_status[f"{lower_level}_touched"] = True
            touch_status[f"{lower_level}_touch_time"] = event.get("touch_time")
            touch_status[f"{lower_level}_alert_sent"] = True

        if not touch_status.get("first_touch_level"):
            touch_status["first_touch_level"] = level
            touch_status["first_touch_source"] = event.get("source")
            touch_status["first_touch_time"] = event.get("touch_time")

        touch_status.setdefault("events", []).append(event)

        item["touch_status"] = touch_status
        data[instrument_key] = item
        opening_range_cache["data"] = data


# ============================================================
# Isolated Instrument Selection Helpers
# ============================================================


def get_level_priority(level: str) -> int:
    """
    Returns priority rank for touched level.

    Lower value means higher priority.
    Example:
        S3 first, then R3, then S2, then R2.
    """
    level_upper = str(level or "").upper()

    try:
        return DEFAULT_ISOLATION_PRIORITY_LEVELS.index(level_upper)
    except ValueError:
        return 999


def get_reference_opening_range_average() -> float | None:
    """
    Returns the reference Opening Range average used for isolation.

    Preference:
        1. Main index Opening Range average.
        2. Latest main index LTP.
    """
    with _opening_range_cache_lock:
        main_item = opening_range_cache.get("data", {}).get(DEFAULT_MAIN_INDEX_KEY)

        if main_item:
            range_payload = main_item.get("range") or {}
            avg = range_payload.get("average")

            if avg is not None:
                return safe_float(avg)

        latest_ltp = opening_range_cache.get("latest_main_index_ltp")

    if latest_ltp is not None:
        return safe_float(latest_ltp)

    return get_latest_main_index_ltp()


def build_average_window(reference_average: float) -> dict:
    """Builds average +/- window clamped by configured strike filter range."""
    strike_from = safe_float(getattr(config, "STRIKE_FROM", 0.0))
    strike_to = safe_float(getattr(config, "STRIKE_TO", 999999.0))

    raw_lower = reference_average - DEFAULT_ISOLATION_WINDOW_POINTS
    raw_upper = reference_average + DEFAULT_ISOLATION_WINDOW_POINTS

    final_lower = max(strike_from, raw_lower)
    final_upper = min(strike_to, raw_upper)

    return {
        "reference_average": reference_average,
        "window_points": DEFAULT_ISOLATION_WINDOW_POINTS,
        "raw_lower": raw_lower,
        "raw_upper": raw_upper,
        "eligible_lower": raw_lower,
        "eligible_upper": raw_upper,
        "configured_from": strike_from,
        "configured_to": strike_to,
        "final_lower": final_lower,
        "final_upper": final_upper,
    }


def is_event_eligible_for_isolation(event: dict) -> tuple[bool, str]:
    """Checks whether a touch event can be used for isolated instrument selection."""
    if not DEFAULT_ISOLATION_ENABLED:
        return False, "isolation_disabled"

    if not isinstance(event, dict):
        return False, "invalid_event"

    level = str(event.get("level", "")).upper()

    if level not in DEFAULT_ISOLATION_TOUCH_LEVELS:
        return False, "level_not_eligible"

    source = str(event.get("source", ""))

    if (
        source == "intraday_backfill_scan"
        and not DEFAULT_ISOLATION_ALLOW_BACKFILL_TOUCH
    ):
        return False, "backfill_selection_disabled"

    if source == "live_tick" and not DEFAULT_ISOLATION_ALLOW_LIVE_TOUCH:
        return False, "live_selection_disabled"

    contract_info = event.get("contract_info") or {}

    if DEFAULT_ISOLATION_OPTIONS_ONLY and not is_option_contract(contract_info):
        return False, "not_option_contract"

    strike = contract_info.get("strike_price")

    if strike is None:
        return False, "missing_strike"

    reference_average = get_reference_opening_range_average()

    if reference_average is None or reference_average <= 0:
        return False, "reference_average_not_available"

    window = build_average_window(reference_average)
    strike_value = safe_float(strike)

    if not (window["final_lower"] <= strike_value <= window["final_upper"]):
        return False, "strike_outside_average_window"

    return True, "eligible"


def choose_best_isolation_event(events: list) -> dict | None:
    """
    Chooses the best isolation event using:
        1. Level priority: S3, R3, S2, R2.
        2. Nearest strike to Opening Range average.
    """
    if not events:
        return None

    reference_average = get_reference_opening_range_average()

    if reference_average is None or reference_average <= 0:
        return None

    eligible_events = []

    for event in events:
        eligible, _reason = is_event_eligible_for_isolation(event)

        if not eligible:
            continue

        info = event.get("contract_info") or {}
        strike = safe_float(info.get("strike_price"))
        level = str(event.get("level", "")).upper()

        eligible_events.append(
            {
                "event": event,
                "priority": get_level_priority(level),
                "distance_to_average": abs(strike - reference_average),
                "strike": strike,
            }
        )

    if not eligible_events:
        return None

    selected = min(
        eligible_events,
        key=lambda item: (
            item["priority"],
            item["distance_to_average"],
            item["strike"],
        ),
    )

    return selected["event"]


def should_replace_isolated_instrument(new_event: dict) -> bool:
    """
    Determines whether new event can replace currently isolated instrument.

    Default behavior:
        - If nothing selected, select.
        - If already selected and lock-for-day is enabled, do not replace.
        - If priority upgrade is enabled, higher-priority level can replace lower-priority level.
    """
    with _selected_or_lock:
        already_selected = bool(_selected_or_instrument_state.get("selected"))
        current_priority = _selected_or_instrument_state.get("selection_priority")

    if not already_selected:
        return True

    if not DEFAULT_ISOLATION_LOCK_FOR_DAY:
        return True

    if not DEFAULT_ISOLATION_ALLOW_PRIORITY_UPGRADE:
        return False

    new_priority = get_level_priority(new_event.get("level"))

    if current_priority is None:
        return False

    return new_priority < int(current_priority)


def format_isolated_instrument_title(state: dict) -> str:
    """Builds isolated instrument notification title."""
    info = state.get("contract_info") or {}
    strike = info.get("strike_price", "N/A")
    option_type = str(info.get("instrument_type", "N/A")).upper()
    level = state.get("selected_level")

    return f"{strike} {option_type} isolated after {level} touch"


def send_isolated_instrument_notification(state: dict) -> bool:
    """
    Sends Telegram notification when instrument is isolated.

    Uses telegram_service.send_isolated_instrument_message so the new detailed
    role based message format is applied.
    """
    if not DEFAULT_ISOLATED_NOTIFY_ENABLED:
        return False

    return telegram_service.send_isolated_instrument_message(state)


def isolate_instrument_from_event(event: dict) -> bool:
    """
    Isolates one instrument from a touch event.

    Selection rule:
        - S3, R3, S2, R2 priority.
        - Nearest to Opening Range average.
        - Lock selected instrument for the day unless priority upgrade is allowed.
    """
    if not event:
        return False

    eligible, reason = is_event_eligible_for_isolation(event)

    if not eligible:
        logger.info(
            f"Isolation skipped. reason={reason}, "
            f"instrument_key={event.get('instrument_key')}, "
            f"level={event.get('level')}"
        )
        return False

    if not should_replace_isolated_instrument(event):
        return False

    instrument_key = event.get("instrument_key")
    contract_info = event.get("contract_info") or get_contract_info_by_key(
        instrument_key
    )

    reference_average = get_reference_opening_range_average()
    average_window = (
        build_average_window(reference_average) if reference_average else None
    )

    with _opening_range_cache_lock:
        item = opening_range_cache.get("data", {}).get(instrument_key, {})

    selected_level = str(event.get("level", "")).upper()

    state = {
        "selected": True,
        "instrument_key": instrument_key,
        "selected_level": selected_level,
        "level_value": event.get("level_value"),
        "trigger_price": event.get("trigger_price"),
        "trigger_field": event.get("trigger_field"),
        "touch_time": event.get("touch_time"),
        "touch_source": event.get("source"),
        "selected_at": get_now_market_time().isoformat(),
        "selection_priority": get_level_priority(event.get("level")),
        "selection_reason": "level_priority_nearest_to_opening_range_average",
        "reference_average": reference_average,
        "average_window": average_window,
        "contract_info": contract_info,
        "range": item.get("range"),
        "levels": item.get("levels"),
        "isolated_instrument_role": get_isolated_role_from_level(selected_level),
        "latest_live_data": {
            "ltp": get_latest_ltp_for_instrument(instrument_key),
        },
        "latest_main_index_ltp": get_latest_main_index_ltp(),
        "live_ema_calculation_mode_flag": DEFAULT_LIVE_EMA_CALCULATION_MODE,
        "live_ema_calculation_mode": get_live_ema_calculation_mode_text(),
        "ema_alerts_count": 0,
        "last_ema_alert": None,
        "disabled": False,
        "message": "Opening Range instrument isolated for EMA Telegram alerts.",
    }

    with _selected_or_lock:
        previous_alert_count = _selected_or_instrument_state.get("ema_alerts_count", 0)
        state["ema_alerts_count"] = previous_alert_count
        _selected_or_instrument_state.clear()
        _selected_or_instrument_state.update(state)

    with _opening_range_cache_lock:
        opening_range_cache["isolated_instrument"] = dict(state)
        opening_range_cache["isolated_instrument_selected"] = True
        opening_range_cache["isolated_instrument_selected_at"] = state.get(
            "selected_at"
        )
        opening_range_cache["isolated_instrument_selection_reason"] = state.get(
            "selection_reason"
        )

    logger.info(
        f"Opening Range isolated instrument selected. "
        f"instrument_key={instrument_key}, "
        f"level={state.get('selected_level')}, "
        f"strike={contract_info.get('strike_price')}, "
        f"type={contract_info.get('instrument_type')}, "
        f"isolated_role={state.get('isolated_instrument_role')}, "
        f"reference_average={reference_average}"
    )

    send_isolated_instrument_notification(state)

    return True


def try_isolate_from_touch_events(events: list) -> bool:
    """Chooses best event from a list and isolates instrument if applicable."""
    best_event = choose_best_isolation_event(events)

    if not best_event:
        return False

    return isolate_instrument_from_event(best_event)


# ============================================================
# Touch Detection
# ============================================================


def detect_touch_from_candle(
    instrument_key: str,
    candle: dict,
    levels: dict,
    contract_info: dict,
    source: str,
) -> list:
    """Detects R2/R3/S2/S3 touch from intraday candle high/low."""
    if not candle or not levels:
        return []

    events = []

    level_map = {
        "R2": {
            "value": safe_float(levels.get("r2")),
            "condition_field": "high",
            "trigger": safe_float(candle.get("high")),
            "fallback": safe_float(candle.get("close")),
            "direction": "above",
        },
        "R3": {
            "value": safe_float(levels.get("r3")),
            "condition_field": "high",
            "trigger": safe_float(candle.get("high")),
            "fallback": safe_float(candle.get("close")),
            "direction": "above",
        },
        "S2": {
            "value": safe_float(levels.get("s2")),
            "condition_field": "low",
            "trigger": safe_float(candle.get("low")),
            "fallback": safe_float(candle.get("close")),
            "direction": "below",
        },
        "S3": {
            "value": safe_float(levels.get("s3")),
            "condition_field": "low",
            "trigger": safe_float(candle.get("low")),
            "fallback": safe_float(candle.get("close")),
            "direction": "below",
        },
    }

    candle_time = candle.get("timestamp") or get_now_market_time().isoformat()

    for level_name, item in level_map.items():
        if level_name not in DEFAULT_ISOLATION_TOUCH_LEVELS:
            continue

        level_value = item["value"]

        if level_value <= 0:
            continue

        trigger_price = item["trigger"]
        trigger_field = item["condition_field"]

        if trigger_price <= 0:
            trigger_price = item["fallback"]
            trigger_field = "close"

        if trigger_price <= 0:
            continue

        if item["direction"] == "above":
            touched = trigger_price >= level_value
        else:
            touched = trigger_price <= level_value

        if not touched:
            continue

        if should_skip_touch_alert(instrument_key, level_name, contract_info):
            continue

        events.append(
            create_touch_event(
                instrument_key=instrument_key,
                level=level_name,
                level_value=level_value,
                trigger_price=trigger_price,
                trigger_field=trigger_field,
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
    """Scans post-OR intraday candles for already touched R2/R3/S2/S3."""
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
    """Extracts ltp/high/low/close from Upstox full feed."""
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
    Processes live tick for Opening Range R2/R3/S2/S3 touch detection.

    New behavior:
    - Touches are tracked for all option instruments.
    - Best eligible touch can isolate one instrument for the day.
    - EMA calculation continues for all instruments.
    - Telegram EMA alerts are sent only for isolated instrument.
    """
    if not DEFAULT_LIVE_TOUCH_ALERT_ENABLED:
        return []

    if not instrument_key or not isinstance(tick_data, dict):
        return []

    feed_values = extract_feed_values(tick_data)
    ltp = safe_float(feed_values.get("ltp"))
    updated_at = feed_values.get("timestamp")

    update_latest_ltp_for_instrument(
        instrument_key=instrument_key,
        ltp=ltp,
        updated_at=updated_at,
    )

    if instrument_key == DEFAULT_MAIN_INDEX_KEY and ltp > 0:
        update_latest_main_index_ltp(
            ltp=ltp,
            source="live_tick",
            updated_at=updated_at,
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

    pseudo_candle = {
        "timestamp": feed_values.get("timestamp") or get_now_market_time().isoformat(),
        "open": 0.0,
        "high": feed_values.get("high"),
        "low": feed_values.get("low"),
        "close": feed_values.get("close") or feed_values.get("ltp"),
        "volume": 0,
        "oi": 0,
    }

    if DEFAULT_TOUCH_CHECK_MODE == "ltp":
        ltp_value = safe_float(feed_values.get("ltp"))
        pseudo_candle["high"] = ltp_value
        pseudo_candle["low"] = ltp_value
        pseudo_candle["close"] = ltp_value

    events = detect_touch_from_candle(
        instrument_key=instrument_key,
        candle=pseudo_candle,
        levels=levels,
        contract_info=contract_info,
        source="live_tick",
    )

    for event in events:
        if DEFAULT_TOUCH_ALERT_ONCE_PER_LEVEL:
            mark_touch_alert_sent(event)

        update_touch_status_in_cache(instrument_key, event)
        queue_touch_event(event)

    if events:
        try_isolate_from_touch_events(events)

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
    """Sends legacy grouped Telegram touch alert only if enabled."""
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
        title="Opening Range Touch Alert",
        message=message,
        level="REFRESH",
    )

    if sent:
        _last_touch_alert_sent_at = now_ts

    return sent


def flush_pending_touch_alerts(force: bool = False, source: str = "live_tick") -> bool:
    """Flushes pending touch events into one Telegram message if legacy enabled."""
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
# Selected / Isolated Instrument Compatibility Helpers
# ============================================================


def is_selected_or_instrument_locked() -> bool:
    """Returns True if isolated instrument is selected."""
    with _selected_or_lock:
        return bool(_selected_or_instrument_state.get("selected"))


def get_selected_or_instrument_key() -> str | None:
    """Returns isolated instrument key."""
    with _selected_or_lock:
        return _selected_or_instrument_state.get("instrument_key")


def get_selected_or_instrument_state() -> dict:
    """Returns isolated instrument state."""
    with _selected_or_lock:
        return dict(_selected_or_instrument_state)


def get_selected_or_ema_alerts(limit: int = 100) -> list:
    """Returns isolated instrument EMA alerts."""
    limit = max(1, int(limit or 100))

    with _selected_or_lock:
        return list(_selected_or_ema_alerts)[-limit:]


# ============================================================
# Isolated EMA Telegram Alert Helpers
# ============================================================


def get_isolated_instrument_type_from_state(
    selected_state: dict,
) -> str | None:
    """
    Returns CE or PE from the isolated instrument state.
    """

    if not isinstance(selected_state, dict):
        return None

    contract_info = selected_state.get("contract_info") or {}

    instrument_type = str(contract_info.get("instrument_type", "")).strip().upper()

    if instrument_type in ["CE", "CALL"]:
        return "CE"

    if instrument_type in ["PE", "PUT"]:
        return "PE"

    return None


def get_suggested_order_instruments_for_ema(
    cross_type: str,
    isolated_instrument_type: str | None = None,
) -> list:
    """
    Returns nearest option instruments around current NIFTY LTP.

    Rule:
        bullish_cross -> same side as isolated instrument
        bearish_cross -> opposite side of isolated instrument
    """

    nifty_ltp = get_latest_main_index_ltp()

    if not nifty_ltp or nifty_ltp <= 0:
        logger.warning(
            "Suggested EMA order instruments could not be resolved because "
            "latest NIFTY LTP is not available."
        )
        return []

    instruments = get_nearest_order_instruments_for_ema_cross(
        current_nifty_ltp=nifty_ltp,
        cross_type=cross_type,
        isolated_instrument_type=isolated_instrument_type,
    )

    output = []

    for item in instruments:
        if not isinstance(item, dict):
            continue

        instrument_key = item.get("instrument_key")

        cached_live_ltp = (
            get_latest_ltp_for_instrument(instrument_key) if instrument_key else None
        )

        live_ltp = cached_live_ltp

        if live_ltp is None:
            live_ltp = item.get("live_ltp")

        if live_ltp is None:
            live_ltp = item.get("ltp")

        output.append(
            {
                **item,
                "live_ltp": live_ltp,
            }
        )

    return output


def get_budget_order_instruments_for_isolated_ema(
    cross_type: str,
    isolated_instrument_type: str | None = None,
) -> list:
    """
    Returns budget range option instruments around current NIFTY LTP.

    Rule:
        bullish_cross -> same side as isolated instrument
        bearish_cross -> opposite side of isolated instrument

    Filter:
        LTP between EMA_ALERT_BUDGET_MIN_PRICE and EMA_ALERT_BUDGET_MAX_PRICE.
        If multiple instruments exist, option_service sorts nearest to current NIFTY LTP.
    """

    nifty_ltp = get_latest_main_index_ltp()

    if not nifty_ltp or nifty_ltp <= 0:
        logger.warning(
            "Budget EMA order instruments could not be resolved because "
            "latest NIFTY LTP is not available."
        )
        return []

    return get_budget_order_instruments_for_ema_cross(
        current_nifty_ltp=nifty_ltp,
        cross_type=cross_type,
        isolated_instrument_type=isolated_instrument_type,
    )


def format_suggested_order_instruments(
    instruments: list,
) -> str:
    """Formats suggested instruments for Telegram message."""

    if not instruments:
        return "Nearest Order Instruments: not_available"

    lines = ["Nearest Order Instruments:"]

    for item in instruments:
        if not isinstance(item, dict):
            continue

        strike = item.get("strike_price", "N/A")
        option_type = str(item.get("instrument_type", "N/A")).upper()

        live_ltp = item.get("live_ltp")

        if live_ltp is None:
            price_text = "ltp_not_available"
        else:
            price_text = f"{live_ltp} rs"

        lines.append(f"- NIFTY {strike} {option_type} - {price_text}")

    if len(lines) == 1:
        return "Nearest Order Instruments: not_available"

    return "\n".join(lines)


def normalize_ema_cross_direction(ema_event: dict) -> str:
    """
    Normalizes EMA cross direction for duplicate control.

    Returns:
        bullish
        bearish
        unknown
    """

    if not isinstance(ema_event, dict):
        return "unknown"

    cross_type = str(ema_event.get("cross_type", "")).strip().lower()
    current_signal = str(ema_event.get("current_signal", "")).strip().lower()

    if "bullish" in cross_type or current_signal == "bullish":
        return "bullish"

    if "bearish" in cross_type or current_signal == "bearish":
        return "bearish"

    return "unknown"


def get_ema_alert_minute_bucket(timestamp_value=None) -> str:
    """
    Returns minute bucket for EMA Telegram duplicate control.

    Example:
        2026-08-13T09:43:22+05:30 -> 2026-08-13T09:43

    If timestamp is unavailable, current market time is used.
    """

    if timestamp_value:
        try:
            parsed = parse_candle_timestamp(timestamp_value)

            if parsed:
                return parsed.strftime("%Y-%m-%dT%H:%M")
        except Exception:
            pass

        try:
            text = str(timestamp_value)

            if "T" in text:
                return text[:16]
        except Exception:
            pass

    return get_now_market_time().strftime("%Y-%m-%dT%H:%M")


def should_skip_isolated_ema_alert_for_minute_direction(
    instrument_key: str,
    ema_event: dict,
    timestamp_value=None,
) -> tuple[bool, str, str]:
    """
    Prevents duplicate Telegram EMA alerts in tick/LTP mode.

    Rule:
        One alert per isolated instrument + minute + cross direction.

    This means:
        Same minute + same direction -> skip.
        Same minute + opposite direction -> allow.
    """

    global _selected_or_ema_alert_minute_date

    direction = normalize_ema_cross_direction(ema_event)
    minute_bucket = get_ema_alert_minute_bucket(timestamp_value)
    current_date = get_now_market_time().date().isoformat()

    alert_key = f"{instrument_key}_{minute_bucket}_{direction}"

    with _selected_or_lock:
        if _selected_or_ema_alert_minute_date != current_date:
            _selected_or_ema_alert_minute_keys.clear()
            _selected_or_ema_alert_minute_date = current_date

        if alert_key in _selected_or_ema_alert_minute_keys:
            return True, alert_key, direction

        _selected_or_ema_alert_minute_keys.add(alert_key)

    return False, alert_key, direction


def process_selected_or_ema_cross_alert(
    ema_event: dict,
) -> dict:
    """
    Sends Telegram EMA alert only for isolated instrument.

    New order-side rule:
        bullish_cross -> same side as isolated instrument
        bearish_cross -> opposite side of isolated instrument

    Budget order selection:
        - Use suggested order side only.
        - LTP must be inside configured budget range.
        - Lowest option LTP is selected first.
        - budget_order_instruments[0] is the selected order instrument.

    Return value:
        {
            "sent": bool,
            "selected_order_instrument": dict | None,
        }

    The selected order instrument is returned so the caller
    (upstox_websocket.py) can place the actual order on the
    selected budget instrument instead of blindly ordering the
    isolated EMA-crossing instrument.
    """

    if not DEFAULT_EMA_ISOLATED_TELEGRAM_ENABLED:
        return {
            "sent": False,
            "selected_order_instrument": None,
        }

    if not isinstance(ema_event, dict):
        return {
            "sent": False,
            "selected_order_instrument": None,
        }

    with _selected_or_lock:
        selected_state = dict(_selected_or_instrument_state)

    if not selected_state.get("selected"):
        return {
            "sent": False,
            "selected_order_instrument": None,
        }

    isolated_key = selected_state.get("instrument_key")
    event_key = ema_event.get("instrument_key")

    if not isolated_key or isolated_key != event_key:
        return {
            "sent": False,
            "selected_order_instrument": None,
        }

    contract_info = selected_state.get("contract_info") or {}

    isolated_instrument_type = get_isolated_instrument_type_from_state(selected_state)

    selected_level = selected_state.get(
        "selected_level",
        "N/A",
    )

    nifty_ltp = get_latest_main_index_ltp()

    cross_type = ema_event.get(
        "cross_type",
        "N/A",
    )

    current_signal = ema_event.get(
        "current_signal",
        "N/A",
    )

    event_timestamp = ema_event.get("timestamp")

    # ------------------------------------------------------------
    # Duplicate protection for tick/LTP based EMA mode
    # ------------------------------------------------------------

    if DEFAULT_LIVE_EMA_CALCULATION_MODE:
        (
            skip_alert,
            minute_alert_key,
            alert_direction,
        ) = should_skip_isolated_ema_alert_for_minute_direction(
            instrument_key=event_key,
            ema_event=ema_event,
            timestamp_value=event_timestamp,
        )

        if skip_alert:
            logger.info(
                f"Skipping duplicate tick EMA Telegram alert for same minute "
                f"and same direction. "
                f"instrument_key={event_key}, "
                f"minute_alert_key={minute_alert_key}, "
                f"direction={alert_direction}"
            )

            return {
                "sent": False,
                "selected_order_instrument": None,
            }

    else:
        minute_alert_key = None
        alert_direction = normalize_ema_cross_direction(ema_event)

    # ------------------------------------------------------------
    # EMA calculation mode
    # ------------------------------------------------------------

    ema_calculation_mode = ema_event.get(
        "ema_calculation_mode",
        get_live_ema_calculation_mode_text(),
    )

    source = ema_event.get(
        "source",
        "live_feed",
    )

    if ema_calculation_mode == "tick_ltp":
        mode_description = "Live tick/LTP based EMA cross detection"
    else:
        mode_description = "Completed candle close based EMA cross detection"

    # ------------------------------------------------------------
    # Suggested order instruments
    #
    # bullish_cross:
    #     same option side as isolated instrument
    #
    # bearish_cross:
    #     opposite option side
    # ------------------------------------------------------------

    suggested_instruments = get_suggested_order_instruments_for_ema(
        cross_type=cross_type,
        isolated_instrument_type=isolated_instrument_type,
    )

    # ------------------------------------------------------------
    # Budget instruments
    #
    # option_service.py now sorts these by LOWEST LTP.
    #
    # Therefore:
    #
    # budget_instruments[0]
    #
    # is the selected lowest-priced instrument inside the
    # configured budget range.
    # ------------------------------------------------------------

    budget_instruments = get_budget_order_instruments_for_isolated_ema(
        cross_type=cross_type,
        isolated_instrument_type=isolated_instrument_type,
    )

    selected_order_instrument = None

    if budget_instruments:
        selected_order_instrument = dict(budget_instruments[0])

    # ------------------------------------------------------------
    # Suggested order option type
    # ------------------------------------------------------------

    suggested_order_option_type = None

    if suggested_instruments:
        suggested_order_option_type = suggested_instruments[0].get("instrument_type")

    # ------------------------------------------------------------
    # Alert record
    # ------------------------------------------------------------

    alert_record = {
        "type": "isolated_instrument_ema_alert",
        "instrument_key": event_key,
        "contract_info": contract_info,
        "selected_level": selected_level,
        "nifty_ltp": nifty_ltp,
        "isolated_instrument_type": (isolated_instrument_type),
        "isolated_instrument_role": (get_isolated_role_from_level(selected_level)),
        "suggested_order_option_type": (suggested_order_option_type),
        "order_side_rule": (
            "bullish_cross uses isolated instrument side; "
            "bearish_cross uses opposite side"
        ),
        "minute_alert_key": minute_alert_key,
        "alert_direction": alert_direction,
        "ema_calculation_mode": ema_calculation_mode,
        "ema_mode_description": mode_description,
        "ema_event": dict(ema_event),
        "suggested_order_instruments": (suggested_instruments),
        "budget_order_instruments": (budget_instruments),
        # --------------------------------------------------------
        # NEW:
        # Explicitly store the selected lowest-LTP instrument.
        # --------------------------------------------------------
        "selected_order_instrument": (selected_order_instrument),
        "debug_details": {
            "cross_type": cross_type,
            "current_signal": current_signal,
            "source": source,
            "close_price": ema_event.get("close"),
            "event_timestamp": event_timestamp,
            "ema_fast_period": ema_event.get(
                "ema_fast_period",
                getattr(
                    config,
                    "LIVE_EMA_FAST_PERIOD",
                    9,
                ),
            ),
            "ema_slow_period": ema_event.get(
                "ema_slow_period",
                getattr(
                    config,
                    "LIVE_EMA_SLOW_PERIOD",
                    21,
                ),
            ),
            "ema_fast": ema_event.get("ema_fast"),
            "ema_slow": ema_event.get("ema_slow"),
            "previous_ema_fast": ema_event.get("previous_ema_fast"),
            "previous_ema_slow": ema_event.get("previous_ema_slow"),
        },
        "created_at": (get_now_market_time().isoformat()),
    }

    # ------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------

    logger.info(
        f"Processing isolated EMA Telegram alert. "
        f"instrument_key={event_key}, "
        f"cross_type={cross_type}, "
        f"isolated_instrument_type={isolated_instrument_type}, "
        f"suggested_order_option_type={suggested_order_option_type}, "
        f"suggested_count={len(suggested_instruments)}, "
        f"budget_count={len(budget_instruments)}, "
        f"selected_order_instrument="
        f"{(
            selected_order_instrument.get('instrument_key')
            if selected_order_instrument
            else None
        )}, "
        f"selected_order_symbol="
        f"{(
            selected_order_instrument.get('trading_symbol')
            if selected_order_instrument
            else None
        )}, "
        f"selected_order_ltp="
        f"{(
            selected_order_instrument.get('live_ltp')
            if selected_order_instrument
            else None
        )}"
    )

    # ------------------------------------------------------------
    # Send Telegram alert
    # ------------------------------------------------------------

    sent = telegram_service.send_isolated_ema_cross_message(
        isolated_state=selected_state,
        ema_event=ema_event,
        nifty_ltp=nifty_ltp,
        suggested_order_instruments=suggested_instruments,
        budget_order_instruments=budget_instruments,
    )

    # ------------------------------------------------------------
    # Save alert state
    # ------------------------------------------------------------

    if sent:
        with _selected_or_lock:
            _selected_or_ema_alerts.append(alert_record)

            _selected_or_instrument_state["ema_alerts_count"] = (
                int(
                    _selected_or_instrument_state.get(
                        "ema_alerts_count",
                        0,
                    )
                )
                + 1
            )

            _selected_or_instrument_state["last_ema_alert"] = alert_record

        with _opening_range_cache_lock:
            opening_range_cache["isolated_ema_alerts_count"] = len(
                _selected_or_ema_alerts
            )

            opening_range_cache["isolated_instrument"] = dict(
                _selected_or_instrument_state
            )

    # ------------------------------------------------------------
    # IMPORTANT:
    # Return both Telegram status and the selected order instrument.
    # ------------------------------------------------------------

    return {
        "sent": sent,
        "selected_order_instrument": (selected_order_instrument),
    }


# ============================================================
# EMA WebSocket Opening Range Enrichment Helper
# ============================================================


def get_opening_range_levels_for_ema_event(instrument_key: str) -> dict:
    """
    Returns lightweight Opening Range payload for EMA crossover events.
    """
    if not DEFAULT_EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS:
        return {
            "opening_range": {},
            "touch_status": get_default_touch_status(),
            "latest_intraday_close": None,
            "latest_main_index_ltp": None,
            "processed_at": None,
            "isolated_instrument": get_selected_or_instrument_state(),
        }

    if not instrument_key:
        return {
            "opening_range": {},
            "touch_status": get_default_touch_status(),
            "latest_intraday_close": None,
            "latest_main_index_ltp": None,
            "processed_at": None,
            "isolated_instrument": get_selected_or_instrument_state(),
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
            "isolated_instrument": get_selected_or_instrument_state(),
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
        "touch_status": item.get("touch_status", get_default_touch_status()),
        "latest_intraday_close": item.get("latest_intraday_close"),
        "latest_main_index_ltp": latest_main_index_ltp,
        "processed_at": item.get("processed_at"),
        "isolated_instrument": get_selected_or_instrument_state(),
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
            "live_ema_calculation_mode_flag": DEFAULT_LIVE_EMA_CALCULATION_MODE,
            "live_ema_calculation_mode": get_live_ema_calculation_mode_text(),
            "events_count": len(_touch_events),
            "events": list(_touch_events),
            "isolated_instrument": get_selected_or_instrument_state(),
            "isolated_ema_alerts_count": len(_selected_or_ema_alerts),
            "isolated_ema_alerts": list(_selected_or_ema_alerts),
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
    Also scans post-OR candles for already touched R2/R3/S2/S3.
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

    # Update cache before selecting isolated instrument from backfill events.
    with _opening_range_cache_lock:
        opening_range_cache["last_run_at"] = completed_at
        opening_range_cache["date"] = current_date
        opening_range_cache["status"] = overall_status
        opening_range_cache["message"] = "Opening range calculation completed."
        opening_range_cache["source"] = "intraday_api"
        opening_range_cache["interval"] = DEFAULT_OPENING_RANGE_INTERVAL
        opening_range_cache["opening_range_candle_count"] = candle_count
        opening_range_cache["market_open_time"] = (
            f"{DEFAULT_MARKET_OPEN_HOUR:02d}:{DEFAULT_MARKET_OPEN_MINUTE:02d}"
        )
        opening_range_cache["fetch_time"] = (
            f"{DEFAULT_FETCH_HOUR:02d}:{DEFAULT_FETCH_MINUTE:02d}"
        )
        opening_range_cache["total_instruments"] = total_instruments
        opening_range_cache["success_count"] = success_count
        opening_range_cache["failed_count"] = failed_count
        opening_range_cache["empty_count"] = empty_count
        opening_range_cache["insufficient_data_count"] = insufficient_data_count
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

    # Important: choose isolated instrument after all backfill events are collected,
    # so S3/R3 priority can win over S2/R2.
    if backfill_touch_events:
        try_isolate_from_touch_events(backfill_touch_events)

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
        "live_ema_calculation_mode_flag": DEFAULT_LIVE_EMA_CALCULATION_MODE,
        "live_ema_calculation_mode": get_live_ema_calculation_mode_text(),
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
        "isolated_instrument": get_selected_or_instrument_state(),
        "isolated_ema_alerts_count": len(_selected_or_ema_alerts),
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
        opening_range_cache["output_file_path"] = output_file_path
        opening_range_cache["isolated_instrument"] = get_selected_or_instrument_state()
        opening_range_cache["isolated_instrument_selected"] = bool(
            get_selected_or_instrument_state().get("selected")
        )
        opening_range_cache["isolated_ema_alerts_count"] = len(_selected_or_ema_alerts)

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
        f"isolated_selected={get_selected_or_instrument_state().get('selected')}, "
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
            "isolated_instrument": get_selected_or_instrument_state(),
            "isolated_instrument_selected": bool(
                get_selected_or_instrument_state().get("selected")
            ),
            "isolated_ema_alerts_count": len(_selected_or_ema_alerts),
            "ema_cross_include_opening_range_levels": (
                DEFAULT_EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS
            ),
            "live_ema_calculation": {
                "flag": DEFAULT_LIVE_EMA_CALCULATION_MODE,
                "mode": get_live_ema_calculation_mode_text(),
                "description": (
                    "live tick/LTP based EMA calculation"
                    if DEFAULT_LIVE_EMA_CALCULATION_MODE
                    else "completed candle close based EMA calculation"
                ),
            },
            "isolation_config": {
                "enabled": DEFAULT_ISOLATION_ENABLED,
                "window_points": DEFAULT_ISOLATION_WINDOW_POINTS,
                "touch_levels": DEFAULT_ISOLATION_TOUCH_LEVELS,
                "priority_levels": DEFAULT_ISOLATION_PRIORITY_LEVELS,
                "lock_for_day": DEFAULT_ISOLATION_LOCK_FOR_DAY,
                "allow_priority_upgrade": DEFAULT_ISOLATION_ALLOW_PRIORITY_UPGRADE,
            },
            "errors": opening_range_cache.get("errors", {}),
        }


def get_opening_range_cache() -> dict:
    """Returns full opening range cache."""
    with _opening_range_cache_lock:
        cache_copy = dict(opening_range_cache)

    cache_copy["isolated_instrument"] = get_selected_or_instrument_state()
    cache_copy["isolated_ema_alerts_count"] = len(_selected_or_ema_alerts)
    cache_copy["live_ema_calculation_mode_flag"] = DEFAULT_LIVE_EMA_CALCULATION_MODE
    cache_copy["live_ema_calculation_mode"] = get_live_ema_calculation_mode_text()
    cache_copy["live_ema_calculation"] = {
        "flag": DEFAULT_LIVE_EMA_CALCULATION_MODE,
        "mode": get_live_ema_calculation_mode_text(),
        "description": (
            "live tick/LTP based EMA calculation"
            if DEFAULT_LIVE_EMA_CALCULATION_MODE
            else "completed candle close based EMA calculation"
        ),
    }

    return cache_copy


def get_opening_range_dashboard_summary(
    touch_limit: int = 100,
    alert_limit: int = 100,
) -> dict:
    """
    Returns compact dashboard data for isolated EMA dashboard.

    Includes:
    - Opening Range status
    - Isolated instrument state
    - Latest isolated EMA alerts
    - Recent Opening Range touch events
    - Live EMA calculation mode
    - Basic cache summary
    """

    touch_limit = max(1, int(touch_limit or 100))
    alert_limit = max(1, int(alert_limit or 100))

    with _opening_range_cache_lock:
        cache_snapshot = dict(opening_range_cache)
        latest_main_index_ltp = opening_range_cache.get("latest_main_index_ltp")
        latest_main_index_ltp_source = opening_range_cache.get(
            "latest_main_index_ltp_source"
        )
        latest_main_index_ltp_updated_at = opening_range_cache.get(
            "latest_main_index_ltp_updated_at"
        )

    isolated_state = get_selected_or_instrument_state()
    isolated_key = isolated_state.get("instrument_key")

    isolated_opening_range_context = {}

    if isolated_key:
        try:
            isolated_opening_range_context = get_opening_range_levels_for_ema_event(
                isolated_key
            )
        except Exception as ex:
            isolated_opening_range_context = {
                "status": "error",
                "error": f"{type(ex).__name__}: {ex}",
            }

    recent_touch_events = get_opening_range_touch_events(limit=touch_limit)
    isolated_ema_alerts = get_selected_or_ema_alerts(limit=alert_limit)

    return {
        "status": "success",
        "generated_at": get_now_market_time().isoformat(),
        "date": get_now_market_time().date().isoformat(),
        "live_ema_calculation": {
            "flag": DEFAULT_LIVE_EMA_CALCULATION_MODE,
            "mode": get_live_ema_calculation_mode_text(),
            "description": (
                "live tick/LTP based EMA calculation"
                if DEFAULT_LIVE_EMA_CALCULATION_MODE
                else "completed candle close based EMA calculation"
            ),
        },
        "opening_range_status": get_opening_range_status(),
        "isolated_instrument": isolated_state,
        "selected_or_instrument": isolated_state,
        "isolated_opening_range_context": isolated_opening_range_context,
        "isolated_ema_alerts_count": len(_selected_or_ema_alerts),
        "isolated_ema_alerts": isolated_ema_alerts,
        "recent_touch_events_count": len(recent_touch_events),
        "recent_touch_events": recent_touch_events,
        "latest_main_index_ltp": latest_main_index_ltp,
        "latest_main_index_ltp_source": latest_main_index_ltp_source,
        "latest_main_index_ltp_updated_at": latest_main_index_ltp_updated_at,
        "cache_summary": {
            "last_run_at": cache_snapshot.get("last_run_at"),
            "date": cache_snapshot.get("date"),
            "status": cache_snapshot.get("status"),
            "message": cache_snapshot.get("message"),
            "source": cache_snapshot.get("source"),
            "interval": cache_snapshot.get("interval"),
            "opening_range_candle_count": cache_snapshot.get(
                "opening_range_candle_count"
            ),
            "total_instruments": cache_snapshot.get("total_instruments"),
            "success_count": cache_snapshot.get("success_count"),
            "failed_count": cache_snapshot.get("failed_count"),
            "empty_count": cache_snapshot.get("empty_count"),
            "insufficient_data_count": cache_snapshot.get("insufficient_data_count"),
            "touch_events_count": cache_snapshot.get("touch_events_count"),
            "pending_touch_events_count": cache_snapshot.get(
                "pending_touch_events_count"
            ),
            "alert_sent_keys_count": cache_snapshot.get("alert_sent_keys_count"),
            "output_file_path": cache_snapshot.get("output_file_path"),
        },
    }


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
