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
    # ========================================================
    # OR + EMA Strategy Runtime State
    # ========================================================
    # ========================================================
    # OR + EMA Strategy Runtime State
    # ========================================================
    "strategy_enabled": True,
    "strategy_or_average": None,
    "strategy_strike_from": None,
    "strategy_strike_to": None,
    "strategy_eligible_count": 0,
    "strategy_eligible_instruments": {},
    # All touched candidates, including non-selected debug-only touches.
    "strategy_touched_count": 0,
    "strategy_touched_instruments": {},
    # Final selected touch used for EMA confirmation.
    "strategy_selected_touch": None,
    "strategy_selected_touch_key": None,
    "strategy_selected_instrument_key": None,
    "strategy_selected_level": None,
    "strategy_selected_reason": None,
    "strategy_selected_at": None,
    "strategy_alerts_sent_count": 0,
    "strategy_alerts_sent": {},
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
# OR + EMA Strategy Runtime Cache
# ============================================================

_or_ema_strategy_lock = Lock()

_or_ema_strategy_cache = {
    "or_average": None,
    "strike_from": None,
    "strike_to": None,
    "eligible_instruments": {},
    # All raw touch candidates for debug.
    "touched_instruments": {},
    # Final selected touch for strategy EMA confirmation.
    "selected_touch": None,
    "selected_touch_key": None,
    "selected_instrument_key": None,
    "selected_level": None,
    "selected_reason": None,
    "selected_at": None,
    "alerts_sent": {},
    "latest_ticks": {},
    "last_updated_at": None,
}


# ============================================================
# Disabled Selected OR Compatibility State
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
# OR + EMA Strategy Config Defaults
# ============================================================

DEFAULT_OR_EMA_STRATEGY_ENABLED = bool(
    getattr(config, "OR_EMA_STRATEGY_ALERT_ENABLED", True)
)

DEFAULT_OR_EMA_STRATEGY_WINDOW_POINTS = int(
    getattr(config, "OR_EMA_STRATEGY_STRIKE_WINDOW_POINTS", 500)
)

DEFAULT_OR_EMA_STRATEGY_TOUCH_LEVELS = [
    str(item).upper()
    for item in getattr(
        config,
        "OR_EMA_STRATEGY_TOUCH_LEVELS",
        ["S2", "S3", "R2", "R3"],
    )
]

DEFAULT_OR_EMA_STRATEGY_TOUCH_CHECK_MODE = getattr(
    config,
    "OR_EMA_STRATEGY_TOUCH_CHECK_MODE",
    "high_low",
)

DEFAULT_OR_EMA_STRATEGY_OPTIONS_ONLY = bool(
    getattr(config, "OR_EMA_STRATEGY_OPTIONS_ONLY", True)
)

DEFAULT_OR_EMA_STRATEGY_STORE_LIVE_TICK_CACHE = bool(
    getattr(config, "OR_EMA_STRATEGY_STORE_LIVE_TICK_CACHE", True)
)

DEFAULT_OR_EMA_STRATEGY_NEAREST_STRIKE_COUNT = int(
    getattr(config, "OR_EMA_STRATEGY_NEAREST_STRIKE_COUNT", 3)
)

DEFAULT_OR_EMA_STRATEGY_BULLISH_OPTION_TYPE = str(
    getattr(config, "OR_EMA_STRATEGY_BULLISH_OPTION_TYPE", "CE")
).upper()

DEFAULT_OR_EMA_STRATEGY_BEARISH_OPTION_TYPE = str(
    getattr(config, "OR_EMA_STRATEGY_BEARISH_OPTION_TYPE", "PE")
).upper()

DEFAULT_OR_EMA_STRATEGY_BULLISH_STRIKE_MODE = getattr(
    config,
    "OR_EMA_STRATEGY_BULLISH_STRIKE_MODE",
    "equal_or_below",
)

DEFAULT_OR_EMA_STRATEGY_BEARISH_STRIKE_MODE = getattr(
    config,
    "OR_EMA_STRATEGY_BEARISH_STRIKE_MODE",
    "equal_or_above",
)

DEFAULT_OR_EMA_STRATEGY_CONFIRM_SAME_INSTRUMENT = bool(
    getattr(config, "OR_EMA_STRATEGY_CONFIRM_SAME_INSTRUMENT", True)
)

DEFAULT_OR_EMA_STRATEGY_ALERT_ONCE_PER_TOUCH_AND_CROSS = bool(
    getattr(config, "OR_EMA_STRATEGY_ALERT_ONCE_PER_TOUCH_AND_CROSS", True)
)

DEFAULT_OR_EMA_STRATEGY_MAX_TOUCHED_INSTRUMENTS = int(
    getattr(config, "OR_EMA_STRATEGY_MAX_TOUCHED_INSTRUMENTS", 2000)
)

DEFAULT_OR_EMA_STRATEGY_MAX_ALERTS_IN_MEMORY = int(
    getattr(config, "OR_EMA_STRATEGY_MAX_ALERTS_IN_MEMORY", 2000)
)

DEFAULT_OR_EMA_STRATEGY_INCLUDE_NEAREST_LIVE_DATA = bool(
    getattr(config, "OR_EMA_STRATEGY_INCLUDE_NEAREST_LIVE_DATA", True)
)

DEFAULT_OR_EMA_STRATEGY_INCLUDE_TOUCHED_LIVE_DATA = bool(
    getattr(config, "OR_EMA_STRATEGY_INCLUDE_TOUCHED_INSTRUMENT_LIVE_DATA", True)
)

DEFAULT_OR_EMA_STRATEGY_TELEGRAM_TITLE = getattr(
    config,
    "OR_EMA_STRATEGY_TELEGRAM_TITLE",
    "OR Touch + EMA Cross Strategy Alert",
)

DEFAULT_OR_EMA_STRATEGY_TELEGRAM_LEVEL = getattr(
    config,
    "OR_EMA_STRATEGY_TELEGRAM_LEVEL",
    "OPENING_RANGE",
)

DEFAULT_OR_EMA_STRATEGY_SELECTION_MODE = getattr(
    config,
    "OR_EMA_STRATEGY_SELECTION_MODE",
    "first_touch_same_time_nearest",
)

DEFAULT_OR_EMA_STRATEGY_LOCK_AFTER_FIRST_SELECTION = bool(
    getattr(config, "OR_EMA_STRATEGY_LOCK_AFTER_FIRST_SELECTION", True)
)

DEFAULT_OR_EMA_STRATEGY_STORE_NON_SELECTED_TOUCHES = bool(
    getattr(config, "OR_EMA_STRATEGY_STORE_NON_SELECTED_TOUCHES", True)
)

DEFAULT_OR_EMA_STRATEGY_USE_NIFTY_SPOT_FOR_SELECTION = bool(
    getattr(config, "OR_EMA_STRATEGY_USE_NIFTY_SPOT_FOR_SELECTION", True)
)

DEFAULT_OR_EMA_STRATEGY_FALLBACK_TO_EVENT_DISTANCE = bool(
    getattr(config, "OR_EMA_STRATEGY_FALLBACK_TO_EVENT_DISTANCE", True)
)

DEFAULT_OR_EMA_STRATEGY_UNKNOWN_DISTANCE_VALUE = float(
    getattr(config, "OR_EMA_STRATEGY_UNKNOWN_DISTANCE_VALUE", 999999999.0)
)

DEFAULT_OR_EMA_STRATEGY_SAME_TIME_COMPARE_MODE = getattr(
    config,
    "OR_EMA_STRATEGY_SAME_TIME_COMPARE_MODE",
    "exact_timestamp",
)

DEFAULT_OR_EMA_STRATEGY_CONFIRM_SELECTED_ONLY = bool(
    getattr(config, "OR_EMA_STRATEGY_CONFIRM_SELECTED_ONLY", True)
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


def trim_dict_to_max_size(data: dict, max_size: int) -> dict:
    """Keeps dictionary size within max size by removing oldest inserted keys."""

    if not isinstance(data, dict):
        return {}

    try:
        max_size = max(1, int(max_size))
    except Exception:
        max_size = 1000

    while len(data) > max_size:
        first_key = next(iter(data))
        data.pop(first_key, None)

    return data


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
# OR + EMA Strategy Helpers
# ============================================================


def build_or_ema_strategy_universe(results: dict):
    """
    Builds eligible option universe using NIFTY Opening Range average.

    Formula:
        eligible_from = max(STRIKE_FROM, nifty_or_average - window)
        eligible_to = min(STRIKE_TO, nifty_or_average + window)
    """

    if not DEFAULT_OR_EMA_STRATEGY_ENABLED:
        return

    if not isinstance(results, dict):
        return

    nifty_result = results.get(DEFAULT_MAIN_INDEX_KEY)

    if not nifty_result:
        logger.warning(
            "OR EMA strategy universe skipped. "
            f"NIFTY result not found for key={DEFAULT_MAIN_INDEX_KEY}"
        )
        return

    if nifty_result.get("status") != "success":
        logger.warning(
            "OR EMA strategy universe skipped. "
            f"NIFTY OR status={nifty_result.get('status')}"
        )
        return

    range_data = nifty_result.get("range") or {}
    or_average = safe_float(range_data.get("average"))

    if or_average <= 0:
        logger.warning("OR EMA strategy universe skipped. Invalid NIFTY OR average.")
        return

    strike_from_config = safe_float(getattr(config, "STRIKE_FROM", 0))
    strike_to_config = safe_float(getattr(config, "STRIKE_TO", 999999))

    raw_from = or_average - DEFAULT_OR_EMA_STRATEGY_WINDOW_POINTS
    raw_to = or_average + DEFAULT_OR_EMA_STRATEGY_WINDOW_POINTS

    strike_from = max(strike_from_config, raw_from)
    strike_to = min(strike_to_config, raw_to)

    eligible = {}

    for item in options_cache.get("data", []):
        if not isinstance(item, dict):
            continue

        instrument_key = item.get("instrument_key")
        instrument_type = str(item.get("instrument_type", "")).upper()
        strike = safe_float(item.get("strike_price"))

        if not instrument_key:
            continue

        if instrument_type not in ["CE", "PE"]:
            continue

        if strike_from <= strike <= strike_to:
            eligible[instrument_key] = {
                "instrument_key": instrument_key,
                "strike_price": strike,
                "instrument_type": instrument_type,
                "trading_symbol": item.get("trading_symbol"),
                "expiry": item.get("expiry"),
                "contract_info": item,
            }

    with _or_ema_strategy_lock:
        _or_ema_strategy_cache["or_average"] = round(or_average, 4)
        _or_ema_strategy_cache["strike_from"] = round(strike_from, 4)
        _or_ema_strategy_cache["strike_to"] = round(strike_to, 4)
        _or_ema_strategy_cache["eligible_instruments"] = eligible
        _or_ema_strategy_cache["touched_instruments"] = {}

        _or_ema_strategy_cache["selected_touch"] = None
        _or_ema_strategy_cache["selected_touch_key"] = None
        _or_ema_strategy_cache["selected_instrument_key"] = None
        _or_ema_strategy_cache["selected_level"] = None
        _or_ema_strategy_cache["selected_reason"] = None
        _or_ema_strategy_cache["selected_at"] = None

        _or_ema_strategy_cache["alerts_sent"] = {}
        _or_ema_strategy_cache["last_updated_at"] = get_now_market_time().isoformat()

    with _opening_range_cache_lock:
        opening_range_cache["strategy_enabled"] = DEFAULT_OR_EMA_STRATEGY_ENABLED
        opening_range_cache["strategy_or_average"] = round(or_average, 4)
        opening_range_cache["strategy_strike_from"] = round(strike_from, 4)
        opening_range_cache["strategy_strike_to"] = round(strike_to, 4)
        opening_range_cache["strategy_eligible_count"] = len(eligible)
        opening_range_cache["strategy_eligible_instruments"] = eligible
        opening_range_cache["strategy_touched_count"] = 0
        opening_range_cache["strategy_touched_instruments"] = {}
        opening_range_cache["strategy_alerts_sent_count"] = 0
        opening_range_cache["strategy_alerts_sent"] = {}
        opening_range_cache["strategy_selected_touch"] = None
        opening_range_cache["strategy_selected_touch_key"] = None
        opening_range_cache["strategy_selected_instrument_key"] = None
        opening_range_cache["strategy_selected_level"] = None
        opening_range_cache["strategy_selected_reason"] = None
        opening_range_cache["strategy_selected_at"] = None

    logger.info(
        f"OR EMA strategy universe built. "
        f"or_average={or_average}, "
        f"strike_from={strike_from}, "
        f"strike_to={strike_to}, "
        f"eligible_count={len(eligible)}"
    )


def store_strategy_latest_tick(
    instrument_key: str,
    feed_values: dict,
    contract_info: dict | None = None,
):
    """Stores latest live tick for Telegram nearest strike details."""

    if not DEFAULT_OR_EMA_STRATEGY_ENABLED:
        return

    if not DEFAULT_OR_EMA_STRATEGY_STORE_LIVE_TICK_CACHE:
        return

    if not instrument_key:
        return

    contract_info = contract_info or get_contract_info_by_key(instrument_key)

    tick_snapshot = {
        "instrument_key": instrument_key,
        "ltp": safe_float(feed_values.get("ltp")),
        "high": safe_float(feed_values.get("high")),
        "low": safe_float(feed_values.get("low")),
        "close": safe_float(feed_values.get("close")),
        "timestamp": feed_values.get("timestamp"),
        "contract_info": contract_info or {},
        "updated_at": get_now_market_time().isoformat(),
    }

    with _or_ema_strategy_lock:
        latest_ticks = _or_ema_strategy_cache.setdefault("latest_ticks", {})
        latest_ticks[instrument_key] = tick_snapshot

        if len(latest_ticks) > 10000:
            _or_ema_strategy_cache["latest_ticks"] = trim_dict_to_max_size(
                latest_ticks,
                10000,
            )


def is_strategy_eligible_instrument(instrument_key: str, contract_info: dict) -> bool:
    """Checks whether instrument is inside OR average +/- configured range."""

    if not DEFAULT_OR_EMA_STRATEGY_ENABLED:
        return False

    if DEFAULT_OR_EMA_STRATEGY_OPTIONS_ONLY and not is_option_contract(contract_info):
        return False

    with _or_ema_strategy_lock:
        eligible = _or_ema_strategy_cache.get("eligible_instruments", {})
        return instrument_key in eligible


def build_strategy_touch_key(instrument_key: str, level: str) -> str:
    """Builds strategy touch key."""

    return f"{instrument_key}_{str(level).upper()}"


def build_strategy_alert_key(
    instrument_key: str,
    level: str,
    cross_type: str,
) -> str:
    """Builds strategy alert duplicate key."""

    return f"{instrument_key}_{str(level).upper()}_{str(cross_type).lower()}"


def get_strategy_touch_distance(event: dict) -> float:
    """
    Calculates distance between touched option strike and current NIFTY spot.

    Selection rule:
    - Lower distance is better.
    - Prefer current NIFTY spot.
    - If not available, fallback to event distance_from_index.
    - If both unavailable, return large configured value.
    """

    if not isinstance(event, dict):
        return DEFAULT_OR_EMA_STRATEGY_UNKNOWN_DISTANCE_VALUE

    contract_info = event.get("contract_info") or {}
    strike_price = safe_float(contract_info.get("strike_price"))

    nifty_spot = 0.0

    if DEFAULT_OR_EMA_STRATEGY_USE_NIFTY_SPOT_FOR_SELECTION:
        nifty_spot = safe_float(
            event.get("main_index_ltp") or get_latest_main_index_ltp()
        )

    if strike_price > 0 and nifty_spot > 0:
        return abs(strike_price - nifty_spot)

    if DEFAULT_OR_EMA_STRATEGY_FALLBACK_TO_EVENT_DISTANCE:
        event_distance = event.get("distance_from_index")

        if event_distance is not None:
            return safe_float(
                event_distance,
                default=DEFAULT_OR_EMA_STRATEGY_UNKNOWN_DISTANCE_VALUE,
            )

    return DEFAULT_OR_EMA_STRATEGY_UNKNOWN_DISTANCE_VALUE


def is_same_strategy_touch_time(existing_touch: dict, current_touch: dict) -> bool:
    """
    Returns True if two touch records should be treated as same-time touches.

    Current mode:
    - exact_timestamp
    """

    if not existing_touch or not current_touch:
        return False

    existing_time = existing_touch.get("touch_time")
    current_time = current_touch.get("touch_time")

    if DEFAULT_OR_EMA_STRATEGY_SAME_TIME_COMPARE_MODE == "exact_timestamp":
        return bool(existing_time and current_time and existing_time == current_time)

    return bool(existing_time and current_time and existing_time == current_time)


def update_strategy_selection_cache_fields():
    """Syncs selected strategy fields into opening_range_cache."""

    with _opening_range_cache_lock:
        opening_range_cache["strategy_touched_count"] = len(
            _or_ema_strategy_cache.get("touched_instruments", {})
        )
        opening_range_cache["strategy_touched_instruments"] = dict(
            _or_ema_strategy_cache.get("touched_instruments", {})
        )

        opening_range_cache["strategy_selected_touch"] = _or_ema_strategy_cache.get(
            "selected_touch"
        )
        opening_range_cache["strategy_selected_touch_key"] = _or_ema_strategy_cache.get(
            "selected_touch_key"
        )
        opening_range_cache["strategy_selected_instrument_key"] = (
            _or_ema_strategy_cache.get("selected_instrument_key")
        )
        opening_range_cache["strategy_selected_level"] = _or_ema_strategy_cache.get(
            "selected_level"
        )
        opening_range_cache["strategy_selected_reason"] = _or_ema_strategy_cache.get(
            "selected_reason"
        )
        opening_range_cache["strategy_selected_at"] = _or_ema_strategy_cache.get(
            "selected_at"
        )


def track_strategy_touch(event: dict):
    """
    Stores OR + EMA strategy touch candidate.

    New selection behavior:
    1. If no selected touch exists, select this event.
    2. If selected touch exists with same touch_time, compare distance to NIFTY spot.
       Select nearest strike.
    3. If selected touch already exists from an earlier time, keep later touches only
       for debug and do not use them for EMA confirmation.
    """

    if not DEFAULT_OR_EMA_STRATEGY_ENABLED:
        return

    if not isinstance(event, dict):
        return

    instrument_key = event.get("instrument_key")
    level = str(event.get("level", "")).upper()
    contract_info = event.get("contract_info") or {}

    if level not in DEFAULT_OR_EMA_STRATEGY_TOUCH_LEVELS:
        return

    if not instrument_key:
        return

    if not is_strategy_eligible_instrument(instrument_key, contract_info):
        return

    touch_key = build_strategy_touch_key(instrument_key, level)
    selection_distance = get_strategy_touch_distance(event)

    touch_payload = {
        "touch_key": touch_key,
        "instrument_key": instrument_key,
        "level": level,
        "level_value": event.get("level_value"),
        "trigger_price": event.get("trigger_price"),
        "trigger_field": event.get("trigger_field"),
        "touch_time": event.get("touch_time"),
        "source": event.get("source"),
        "main_index_ltp": event.get("main_index_ltp"),
        "distance_from_index": event.get("distance_from_index"),
        "selection_distance": round(selection_distance, 4),
        "contract_info": contract_info,
        "event": event,
        "created_at": get_now_market_time().isoformat(),
    }

    selected_now = False
    selected_reason = None

    with _or_ema_strategy_lock:
        touched = _or_ema_strategy_cache.setdefault("touched_instruments", {})

        if DEFAULT_OR_EMA_STRATEGY_STORE_NON_SELECTED_TOUCHES:
            if touch_key not in touched:
                touched[touch_key] = touch_payload

            _or_ema_strategy_cache["touched_instruments"] = trim_dict_to_max_size(
                touched,
                DEFAULT_OR_EMA_STRATEGY_MAX_TOUCHED_INSTRUMENTS,
            )

        selected_touch = _or_ema_strategy_cache.get("selected_touch")

        # Case 1: no selected touch yet.
        if not selected_touch:
            selected_now = True
            selected_reason = "first_touch"

            _or_ema_strategy_cache["selected_touch"] = touch_payload
            _or_ema_strategy_cache["selected_touch_key"] = touch_key
            _or_ema_strategy_cache["selected_instrument_key"] = instrument_key
            _or_ema_strategy_cache["selected_level"] = level
            _or_ema_strategy_cache["selected_reason"] = selected_reason
            _or_ema_strategy_cache["selected_at"] = get_now_market_time().isoformat()

        else:
            same_time = is_same_strategy_touch_time(
                existing_touch=selected_touch,
                current_touch=touch_payload,
            )

            existing_distance = safe_float(
                selected_touch.get("selection_distance"),
                default=DEFAULT_OR_EMA_STRATEGY_UNKNOWN_DISTANCE_VALUE,
            )

            current_distance = safe_float(
                touch_payload.get("selection_distance"),
                default=DEFAULT_OR_EMA_STRATEGY_UNKNOWN_DISTANCE_VALUE,
            )

            # Case 2: same timestamp/candle, choose nearest to NIFTY spot.
            if same_time and current_distance < existing_distance:
                selected_now = True
                selected_reason = "same_time_nearest_to_nifty"

                _or_ema_strategy_cache["selected_touch"] = touch_payload
                _or_ema_strategy_cache["selected_touch_key"] = touch_key
                _or_ema_strategy_cache["selected_instrument_key"] = instrument_key
                _or_ema_strategy_cache["selected_level"] = level
                _or_ema_strategy_cache["selected_reason"] = selected_reason
                _or_ema_strategy_cache["selected_at"] = (
                    get_now_market_time().isoformat()
                )

            # Case 3: later touches are debug-only.
            # Do nothing for selection.

    update_strategy_selection_cache_fields()

    logger.info(
        f"OR EMA strategy touch processed. "
        f"instrument_key={instrument_key}, "
        f"level={level}, "
        f"touch_key={touch_key}, "
        f"selection_distance={round(selection_distance, 4)}, "
        f"selected_now={selected_now}, "
        f"selected_reason={selected_reason}"
    )


def get_touched_strategy_records_for_instrument(instrument_key: str) -> list:
    """Returns strategy touch records matching an instrument."""

    if not instrument_key:
        return []

    with _or_ema_strategy_lock:
        touched = _or_ema_strategy_cache.get("touched_instruments", {})

        return [
            item
            for item in touched.values()
            if item.get("instrument_key") == instrument_key
        ]


def get_latest_tick_snapshot(instrument_key: str) -> dict | None:
    """Returns latest stored live tick for an instrument."""

    if not instrument_key:
        return None

    with _or_ema_strategy_lock:
        return _or_ema_strategy_cache.get("latest_ticks", {}).get(instrument_key)


def get_nearest_strategy_contracts(
    nifty_spot: float,
    option_type: str,
    count: int,
    mode: str,
) -> list:
    """Returns nearest CE/PE contracts based on current NIFTY spot."""

    nifty_spot = safe_float(nifty_spot)

    if nifty_spot <= 0:
        return []

    option_type = str(option_type or "").upper()
    count = max(1, int(count or 3))

    candidates = []

    for item in options_cache.get("data", []):
        if not isinstance(item, dict):
            continue

        instrument_type = str(item.get("instrument_type", "")).upper()

        if instrument_type != option_type:
            continue

        strike = safe_float(item.get("strike_price"))

        if strike <= 0:
            continue

        candidate = {
            "instrument_key": item.get("instrument_key"),
            "instrument_type": instrument_type,
            "strike_price": strike,
            "trading_symbol": item.get("trading_symbol"),
            "expiry": item.get("expiry"),
            "distance": abs(strike - nifty_spot),
            "contract_info": item,
        }

        candidates.append(candidate)

    if mode == "equal_or_below":
        filtered = [item for item in candidates if item["strike_price"] <= nifty_spot]
        filtered.sort(key=lambda item: item["strike_price"], reverse=True)

    elif mode == "equal_or_above":
        filtered = [item for item in candidates if item["strike_price"] >= nifty_spot]
        filtered.sort(key=lambda item: item["strike_price"])

    elif mode == "nearest_around_spot":
        filtered = candidates
        filtered.sort(key=lambda item: item["distance"])

    else:
        filtered = candidates
        filtered.sort(key=lambda item: item["distance"])

    selected = filtered[:count]

    for item in selected:
        latest_tick = get_latest_tick_snapshot(item.get("instrument_key"))
        item["latest_tick"] = latest_tick

    return selected


def format_strategy_contract_line(index: int, item: dict) -> str:
    """Formats nearest live data line for Telegram."""

    latest_tick = item.get("latest_tick") or {}

    ltp = latest_tick.get("ltp")
    high = latest_tick.get("high")
    low = latest_tick.get("low")
    close = latest_tick.get("close")
    timestamp = latest_tick.get("timestamp")

    return (
        f"{index}. {item.get('strike_price')} {item.get('instrument_type')}\n"
        f"   Symbol: {item.get('trading_symbol') or item.get('instrument_key')}\n"
        f"   LTP: {ltp if ltp is not None else 'N/A'}\n"
        f"   High: {high if high is not None else 'N/A'}\n"
        f"   Low: {low if low is not None else 'N/A'}\n"
        f"   Close: {close if close is not None else 'N/A'}\n"
        f"   Updated: {timestamp if timestamp else 'N/A'}"
    )


def send_or_ema_strategy_telegram_alert(
    touch_record: dict,
    ema_event: dict,
    nearest_contracts: list,
    nifty_spot: float | None,
) -> bool:
    """Sends Telegram alert for OR touch + EMA cross confirmation."""

    touch_info = touch_record or {}
    ema_event = ema_event or {}
    contract_info = touch_info.get("contract_info") or {}

    strike = contract_info.get("strike_price", "N/A")
    instrument_type = str(contract_info.get("instrument_type", "N/A")).upper()
    symbol = (
        contract_info.get("trading_symbol")
        or contract_info.get("instrument_key")
        or touch_info.get("instrument_key")
    )

    level = touch_info.get("level")
    cross_type = ema_event.get("cross_type")
    cross_text = str(cross_type or "").replace("_", " ").title()

    touched_live = get_latest_tick_snapshot(touch_info.get("instrument_key"))

    touched_live_text = "N/A"

    if touched_live:
        touched_live_text = (
            f"LTP: {touched_live.get('ltp')}, "
            f"High: {touched_live.get('high')}, "
            f"Low: {touched_live.get('low')}, "
            f"Close: {touched_live.get('close')}, "
            f"Updated: {touched_live.get('timestamp')}"
        )

    nearest_lines = [
        format_strategy_contract_line(index + 1, item)
        for index, item in enumerate(nearest_contracts or [])
    ]

    nearest_text = "\n\n".join(nearest_lines) if nearest_lines else "N/A"

    message = (
        f"{strike} {instrument_type} - {level} touch completed\n"
        f"{cross_text} detected\n"
        f"NIFTY Current Spot: {nifty_spot if nifty_spot is not None else 'N/A'}\n\n"
        f"Touch Details:\n"
        f"Symbol: {symbol}\n"
        f"Level: {level}\n"
        f"Level Value: {touch_info.get('level_value')}\n"
        f"Trigger Field: {touch_info.get('trigger_field')}\n"
        f"Trigger Price: {touch_info.get('trigger_price')}\n"
        f"Touch Time: {touch_info.get('touch_time')}\n"
        f"Touch Source: {touch_info.get('source')}\n"
        f"Distance From Index: {touch_info.get('distance_from_index')}\n\n"
        f"EMA Details:\n"
        f"EMA Instrument: {ema_event.get('instrument_key')}\n"
        f"Cross Type: {cross_type}\n"
        f"Current Signal: {ema_event.get('current_signal')}\n"
        f"EMA Close: {ema_event.get('close')}\n"
        f"EMA Candle Time: {ema_event.get('timestamp')}\n"
        f"Created At: {ema_event.get('created_at')}\n\n"
        f"Touched Instrument Live Data:\n"
        f"{touched_live_text}\n\n"
        f"Nearest Live Data:\n"
        f"{nearest_text}"
    )

    return telegram_service.send_message(
        title=DEFAULT_OR_EMA_STRATEGY_TELEGRAM_TITLE,
        message=message,
        level=DEFAULT_OR_EMA_STRATEGY_TELEGRAM_LEVEL,
    )


def process_or_touch_ema_strategy_confirmation(ema_event: dict) -> dict | None:
    """
    Called after a live EMA crossover is generated.

    New behavior:
    - Only selected touched instrument can trigger strategy Telegram alert.
    - Non-selected touched instruments are ignored for EMA confirmation.
    """

    if not DEFAULT_OR_EMA_STRATEGY_ENABLED:
        return None

    if not isinstance(ema_event, dict):
        return None

    instrument_key = ema_event.get("instrument_key")
    cross_type = str(ema_event.get("cross_type", "")).lower()

    if not instrument_key or not cross_type:
        return None

    with _or_ema_strategy_lock:
        selected_touch = _or_ema_strategy_cache.get("selected_touch")
        selected_instrument_key = _or_ema_strategy_cache.get("selected_instrument_key")

    if not selected_touch:
        return None

    if DEFAULT_OR_EMA_STRATEGY_CONFIRM_SELECTED_ONLY:
        if instrument_key != selected_instrument_key:
            logger.info(
                f"OR EMA strategy confirmation ignored for non-selected instrument. "
                f"ema_instrument={instrument_key}, "
                f"selected_instrument={selected_instrument_key}, "
                f"cross_type={cross_type}"
            )
            return None

    if "bullish" in cross_type:
        option_type = DEFAULT_OR_EMA_STRATEGY_BULLISH_OPTION_TYPE
        strike_mode = DEFAULT_OR_EMA_STRATEGY_BULLISH_STRIKE_MODE

    elif "bearish" in cross_type:
        option_type = DEFAULT_OR_EMA_STRATEGY_BEARISH_OPTION_TYPE
        strike_mode = DEFAULT_OR_EMA_STRATEGY_BEARISH_STRIKE_MODE

    else:
        return None

    nifty_spot = get_latest_main_index_ltp()

    nearest_contracts = []

    if DEFAULT_OR_EMA_STRATEGY_INCLUDE_NEAREST_LIVE_DATA:
        nearest_contracts = get_nearest_strategy_contracts(
            nifty_spot=nifty_spot,
            option_type=option_type,
            count=DEFAULT_OR_EMA_STRATEGY_NEAREST_STRIKE_COUNT,
            mode=strike_mode,
        )

    level = selected_touch.get("level")

    alert_key = build_strategy_alert_key(
        instrument_key=instrument_key,
        level=level,
        cross_type=cross_type,
    )

    with _or_ema_strategy_lock:
        already_sent = alert_key in _or_ema_strategy_cache.get("alerts_sent", {})

    if DEFAULT_OR_EMA_STRATEGY_ALERT_ONCE_PER_TOUCH_AND_CROSS and already_sent:
        return None

    sent = send_or_ema_strategy_telegram_alert(
        touch_record=selected_touch,
        ema_event=ema_event,
        nearest_contracts=nearest_contracts,
        nifty_spot=nifty_spot,
    )

    alert_record = {
        "alert_key": alert_key,
        "sent": sent,
        "instrument_key": instrument_key,
        "level": level,
        "cross_type": cross_type,
        "nifty_spot": nifty_spot,
        "nearest_contracts": nearest_contracts,
        "touch_record": selected_touch,
        "ema_event": ema_event,
        "selection_mode": DEFAULT_OR_EMA_STRATEGY_SELECTION_MODE,
        "selected_touch_key": _or_ema_strategy_cache.get("selected_touch_key"),
        "selected_reason": _or_ema_strategy_cache.get("selected_reason"),
        "created_at": get_now_market_time().isoformat(),
    }

    with _or_ema_strategy_lock:
        alerts_sent = _or_ema_strategy_cache.setdefault("alerts_sent", {})
        alerts_sent[alert_key] = alert_record

        _or_ema_strategy_cache["alerts_sent"] = trim_dict_to_max_size(
            alerts_sent,
            DEFAULT_OR_EMA_STRATEGY_MAX_ALERTS_IN_MEMORY,
        )

    with _opening_range_cache_lock:
        opening_range_cache["strategy_alerts_sent_count"] = len(
            _or_ema_strategy_cache.get("alerts_sent", {})
        )
        opening_range_cache["strategy_alerts_sent"] = dict(
            _or_ema_strategy_cache.get("alerts_sent", {})
        )

    logger.info(
        f"OR EMA selected strategy alert processed. "
        f"alert_key={alert_key}, sent={sent}, "
        f"selected_instrument={selected_instrument_key}"
    )

    return {
        "status": "processed",
        "selection_mode": DEFAULT_OR_EMA_STRATEGY_SELECTION_MODE,
        "instrument_key": instrument_key,
        "cross_type": cross_type,
        "alert": alert_record,
    }


def get_or_ema_strategy_status() -> dict:
    """Returns OR + EMA strategy status."""

    with _or_ema_strategy_lock:
        return {
            "enabled": DEFAULT_OR_EMA_STRATEGY_ENABLED,
            "selection_mode": DEFAULT_OR_EMA_STRATEGY_SELECTION_MODE,
            "lock_after_first_selection": DEFAULT_OR_EMA_STRATEGY_LOCK_AFTER_FIRST_SELECTION,
            "confirm_selected_only": DEFAULT_OR_EMA_STRATEGY_CONFIRM_SELECTED_ONLY,
            "store_non_selected_touches": DEFAULT_OR_EMA_STRATEGY_STORE_NON_SELECTED_TOUCHES,
            "or_average": _or_ema_strategy_cache.get("or_average"),
            "strike_from": _or_ema_strategy_cache.get("strike_from"),
            "strike_to": _or_ema_strategy_cache.get("strike_to"),
            "eligible_count": len(
                _or_ema_strategy_cache.get("eligible_instruments", {})
            ),
            "touched_count": len(_or_ema_strategy_cache.get("touched_instruments", {})),
            "selected_touch": _or_ema_strategy_cache.get("selected_touch"),
            "selected_touch_key": _or_ema_strategy_cache.get("selected_touch_key"),
            "selected_instrument_key": _or_ema_strategy_cache.get(
                "selected_instrument_key"
            ),
            "selected_level": _or_ema_strategy_cache.get("selected_level"),
            "selected_reason": _or_ema_strategy_cache.get("selected_reason"),
            "selected_at": _or_ema_strategy_cache.get("selected_at"),
            "alerts_sent_count": len(_or_ema_strategy_cache.get("alerts_sent", {})),
            "latest_ticks_count": len(_or_ema_strategy_cache.get("latest_ticks", {})),
            "last_updated_at": _or_ema_strategy_cache.get("last_updated_at"),
            "touch_levels": DEFAULT_OR_EMA_STRATEGY_TOUCH_LEVELS,
            "nearest_strike_count": DEFAULT_OR_EMA_STRATEGY_NEAREST_STRIKE_COUNT,
            "bullish_option_type": DEFAULT_OR_EMA_STRATEGY_BULLISH_OPTION_TYPE,
            "bullish_strike_mode": DEFAULT_OR_EMA_STRATEGY_BULLISH_STRIKE_MODE,
            "bearish_option_type": DEFAULT_OR_EMA_STRATEGY_BEARISH_OPTION_TYPE,
            "bearish_strike_mode": DEFAULT_OR_EMA_STRATEGY_BEARISH_STRIKE_MODE,
        }


def get_or_ema_strategy_cache() -> dict:
    """Returns full OR + EMA strategy cache."""

    with _or_ema_strategy_lock:
        return {
            "or_average": _or_ema_strategy_cache.get("or_average"),
            "strike_from": _or_ema_strategy_cache.get("strike_from"),
            "strike_to": _or_ema_strategy_cache.get("strike_to"),
            "eligible_instruments": dict(
                _or_ema_strategy_cache.get("eligible_instruments", {})
            ),
            "touched_instruments": dict(
                _or_ema_strategy_cache.get("touched_instruments", {})
            ),
            "selected_touch": _or_ema_strategy_cache.get("selected_touch"),
            "selected_touch_key": _or_ema_strategy_cache.get("selected_touch_key"),
            "selected_instrument_key": _or_ema_strategy_cache.get(
                "selected_instrument_key"
            ),
            "selected_level": _or_ema_strategy_cache.get("selected_level"),
            "selected_reason": _or_ema_strategy_cache.get("selected_reason"),
            "selected_at": _or_ema_strategy_cache.get("selected_at"),
            "alerts_sent": dict(_or_ema_strategy_cache.get("alerts_sent", {})),
            "latest_ticks": dict(_or_ema_strategy_cache.get("latest_ticks", {})),
            "last_updated_at": _or_ema_strategy_cache.get("last_updated_at"),
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
    """Creates normalized Opening Range touch event."""

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


def build_touch_status_from_events(events: list) -> dict:
    """Builds touch status from detected events."""

    status = get_default_touch_status()

    for event in events:
        level = str(event.get("level", "")).upper()
        level_lower = level.lower()

        if level in ["R2", "S2", "R3", "S3"]:
            status[f"{level_lower}_touched"] = True
            status[f"{level_lower}_touch_time"] = event.get("touch_time")
            status[f"{level_lower}_alert_sent"] = True

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
    level_lower = level.lower()

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
            touch_status[f"{level_lower}_touched"] = True
            touch_status[f"{level_lower}_touch_time"] = event.get("touch_time")
            touch_status[f"{level_lower}_alert_sent"] = True

        if not touch_status.get("first_touch_level"):
            touch_status["first_touch_level"] = level
            touch_status["first_touch_source"] = event.get("source")
            touch_status["first_touch_time"] = event.get("touch_time")

        touch_status.setdefault("events", []).append(event)

        item["touch_status"] = touch_status
        data[instrument_key] = item
        opening_range_cache["data"] = data


def evaluate_level_touch(
    level: str,
    level_value: float,
    high_value: float,
    low_value: float,
    close_value: float,
    ltp_value: float,
    mode: str,
) -> tuple[bool, float, str]:
    """Evaluates whether one OR level is touched."""

    level = str(level).upper()
    mode = str(mode or "high_low").lower()

    if level_value <= 0:
        return False, 0.0, "none"

    if mode == "ltp":
        if level.startswith("R"):
            return ltp_value >= level_value, ltp_value, "ltp"

        if level.startswith("S"):
            return ltp_value <= level_value, ltp_value, "ltp"

    if level.startswith("R"):
        trigger = high_value if high_value > 0 else close_value
        field = "high" if high_value > 0 else "close"
        return trigger >= level_value, trigger, field

    if level.startswith("S"):
        trigger = low_value if low_value > 0 else close_value
        field = "low" if low_value > 0 else "close"
        return trigger <= level_value, trigger, field

    return False, 0.0, "none"


def detect_touch_from_candle(
    instrument_key: str,
    candle: dict,
    levels: dict,
    contract_info: dict,
    source: str,
) -> list:
    """Detects S2/S3/R2/R3 touch from intraday candle high/low."""

    if not candle or not levels:
        return []

    events = []

    candle_high = safe_float(candle.get("high"))
    candle_low = safe_float(candle.get("low"))
    candle_close = safe_float(candle.get("close"))
    candle_time = candle.get("timestamp") or get_now_market_time().isoformat()

    level_mapping = {
        "R2": safe_float(levels.get("r2")),
        "R3": safe_float(levels.get("r3")),
        "S2": safe_float(levels.get("s2")),
        "S3": safe_float(levels.get("s3")),
    }

    for level_name, level_value in level_mapping.items():
        if level_name not in DEFAULT_OR_EMA_STRATEGY_TOUCH_LEVELS:
            continue

        touched, trigger_price, trigger_field = evaluate_level_touch(
            level=level_name,
            level_value=level_value,
            high_value=candle_high,
            low_value=candle_low,
            close_value=candle_close,
            ltp_value=candle_close,
            mode=DEFAULT_TOUCH_CHECK_MODE,
        )

        if touched:
            if not should_skip_touch_alert(instrument_key, level_name, contract_info):
                event = create_touch_event(
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

                events.append(event)
                track_strategy_touch(event)

    return events


def scan_backfill_touches(
    instrument_key: str,
    candles: list,
    levels: dict,
    contract_info: dict,
    candle_count: int,
) -> list:
    """Scans post-OR intraday candles for already touched levels."""

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
    Processes live tick for Opening Range touch detection.

    New behavior:
    - Checks S2/S3/R2/R3.
    - Tracks touched eligible instruments for OR + EMA strategy confirmation.
    - No selected OR instrument is locked.
    """

    if not DEFAULT_LIVE_TOUCH_ALERT_ENABLED:
        return []

    if not instrument_key or not isinstance(tick_data, dict):
        return []

    if not contract_info:
        contract_info = get_contract_info_by_key(instrument_key)

    feed_values = extract_feed_values(tick_data)
    ltp = safe_float(feed_values.get("ltp"))

    store_strategy_latest_tick(
        instrument_key=instrument_key,
        feed_values=feed_values,
        contract_info=contract_info,
    )

    if instrument_key == DEFAULT_MAIN_INDEX_KEY and ltp > 0:
        update_latest_main_index_ltp(
            ltp=ltp,
            source="live_tick",
            updated_at=feed_values.get("timestamp"),
        )
        return []

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

    high_value = safe_float(feed_values.get("high"))
    low_value = safe_float(feed_values.get("low"))
    close_value = safe_float(feed_values.get("close"), default=ltp)
    event_time = feed_values.get("timestamp") or get_now_market_time().isoformat()

    events = []

    level_mapping = {
        "R2": safe_float(levels.get("r2")),
        "R3": safe_float(levels.get("r3")),
        "S2": safe_float(levels.get("s2")),
        "S3": safe_float(levels.get("s3")),
    }

    for level_name, level_value in level_mapping.items():
        if level_name not in DEFAULT_OR_EMA_STRATEGY_TOUCH_LEVELS:
            continue

        touched, trigger_price, trigger_field = evaluate_level_touch(
            level=level_name,
            level_value=level_value,
            high_value=high_value,
            low_value=low_value,
            close_value=close_value,
            ltp_value=ltp,
            mode=DEFAULT_OR_EMA_STRATEGY_TOUCH_CHECK_MODE,
        )

        if not touched:
            continue

        if should_skip_touch_alert(instrument_key, level_name, contract_info):
            continue

        event = create_touch_event(
            instrument_key=instrument_key,
            level=level_name,
            level_value=level_value,
            trigger_price=trigger_price,
            trigger_field=trigger_field,
            touch_time=event_time,
            source="live_tick",
            contract_info=contract_info,
        )

        events.append(event)
        track_strategy_touch(event)

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
    """Sends legacy Telegram alert for touch events if enabled."""

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
    """Flushes pending live touch events into one Telegram message."""

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
    """Selected OR EMA Telegram alert is disabled."""

    return False


# ============================================================
# EMA WebSocket Opening Range Enrichment Helper
# ============================================================


def get_opening_range_levels_for_ema_event(instrument_key: str) -> dict:
    """Returns lightweight Opening Range payload for EMA crossover events."""

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
            "or_ema_strategy": get_or_ema_strategy_cache(),
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
    """Fetches intraday candles for one instrument and calculates opening range levels."""

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

    # ========================================================
    # Build strategy universe after OR results are ready.
    # ========================================================
    # Important:
    # During per-instrument backfill scan, strategy eligible universe may not
    # be ready yet. So first build the eligible universe from NIFTY OR average,
    # then re-process all backfill touches to apply:
    #
    #   1. first touch wins
    #   2. same timestamp/candle touches choose nearest strike to NIFTY spot
    #   3. later touches are stored only for debug
    # ========================================================

    build_or_ema_strategy_universe(results)

    for event in backfill_touch_events:
        try:
            track_strategy_touch(event)

        except Exception as strategy_ex:
            logger.error(
                f"Failed applying OR EMA strategy selection for backfill event: "
                f"{type(strategy_ex).__name__}: {strategy_ex}"
            )

    strategy_status_after_selection = get_or_ema_strategy_status()

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
        "market_open_time": (
            f"{DEFAULT_MARKET_OPEN_HOUR:02d}:{DEFAULT_MARKET_OPEN_MINUTE:02d}"
        ),
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
        "or_ema_strategy": strategy_status_after_selection,
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

        strategy_cache = get_or_ema_strategy_cache()

        opening_range_cache["strategy_enabled"] = DEFAULT_OR_EMA_STRATEGY_ENABLED
        opening_range_cache["strategy_or_average"] = strategy_cache.get("or_average")
        opening_range_cache["strategy_strike_from"] = strategy_cache.get("strike_from")
        opening_range_cache["strategy_strike_to"] = strategy_cache.get("strike_to")
        opening_range_cache["strategy_eligible_count"] = len(
            strategy_cache.get("eligible_instruments", {})
        )
        opening_range_cache["strategy_eligible_instruments"] = strategy_cache.get(
            "eligible_instruments",
            {},
        )

        opening_range_cache["strategy_touched_count"] = len(
            strategy_cache.get("touched_instruments", {})
        )
        opening_range_cache["strategy_touched_instruments"] = strategy_cache.get(
            "touched_instruments",
            {},
        )

        # Selected strategy touch fields.
        opening_range_cache["strategy_selected_touch"] = strategy_cache.get(
            "selected_touch"
        )
        opening_range_cache["strategy_selected_touch_key"] = strategy_cache.get(
            "selected_touch_key"
        )
        opening_range_cache["strategy_selected_instrument_key"] = strategy_cache.get(
            "selected_instrument_key"
        )
        opening_range_cache["strategy_selected_level"] = strategy_cache.get(
            "selected_level"
        )
        opening_range_cache["strategy_selected_reason"] = strategy_cache.get(
            "selected_reason"
        )
        opening_range_cache["strategy_selected_at"] = strategy_cache.get("selected_at")

        opening_range_cache["strategy_alerts_sent_count"] = len(
            strategy_cache.get("alerts_sent", {})
        )
        opening_range_cache["strategy_alerts_sent"] = strategy_cache.get(
            "alerts_sent",
            {},
        )

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
        f"strategy={get_or_ema_strategy_status()}, "
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
            "or_ema_strategy": get_or_ema_strategy_status(),
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
