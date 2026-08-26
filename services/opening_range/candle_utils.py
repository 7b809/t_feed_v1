"""
Candle, time, instrument, and basic conversion helpers for the
Opening Range package.

This module contains stateless utility functions. It must not import
from the package-level services.opening_range module because doing so
can create circular imports.

Internal modules should import the required helpers directly:

    from .candle_utils import (
        safe_float,
        get_now_market_time,
        parse_candle_timestamp,
    )
"""

from datetime import date, datetime, time as dt_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core import config
from core.logger import get_logger
from services.option_service import (
    get_contract_info_by_instrument_key,
    options_cache,
)

from .constants import (
    DEFAULT_LIVE_EMA_CALCULATION_MODE,
    DEFAULT_MARKET_OPEN_HOUR,
    DEFAULT_MARKET_OPEN_MINUTE,
    DEFAULT_MARKET_TIMEZONE,
    DEFAULT_OPENING_RANGE_CANDLE_COUNT,
    DEFAULT_OPENING_RANGE_ENABLED,
)

logger = get_logger(__file__)


# ============================================================
# Basic Helpers
# ============================================================


def is_opening_range_enabled() -> bool:
    """Returns whether Opening Range calculation is enabled."""
    return DEFAULT_OPENING_RANGE_ENABLED


def get_market_timezone() -> ZoneInfo:
    """
    Loads the configured market timezone.

    Falls back to Asia/Kolkata when the configured timezone is invalid
    or cannot be loaded.
    """
    timezone_name = str(
        getattr(
            config,
            "MARKET_TIMEZONE",
            DEFAULT_MARKET_TIMEZONE,
        )
        or DEFAULT_MARKET_TIMEZONE
    ).strip()

    if not timezone_name:
        timezone_name = "Asia/Kolkata"

    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logger.error(
            "Invalid MARKET_TIMEZONE configured: %s. " "Falling back to Asia/Kolkata.",
            timezone_name,
        )
        return ZoneInfo("Asia/Kolkata")


def get_now_market_time() -> datetime:
    """Returns the current timezone-aware market datetime."""
    return datetime.now(get_market_timezone())


def get_live_ema_calculation_mode_text() -> str:
    """
    Returns the configured live EMA calculation mode.

    LIVE_EMA_CALCULATION_MODE = False:
        Completed candle-close-based EMA calculation.

    LIVE_EMA_CALCULATION_MODE = True:
        Live tick/LTP-based EMA calculation.
    """
    if DEFAULT_LIVE_EMA_CALCULATION_MODE:
        return "tick_ltp"

    return "candle_close"


def safe_float(
    value: Any,
    default: float = 0.0,
) -> float:
    """Safely converts a value to float."""
    try:
        if value is None:
            return default

        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def safe_int(
    value: Any,
    default: int = 0,
) -> int:
    """
    Safely converts a value to int.

    Float-like strings such as "100.0" are also supported.
    """
    try:
        if value is None:
            return default

        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def response_to_dict(api_response: Any) -> dict:
    """
    Converts an Upstox SDK response object to a dictionary safely.

    Supported response types:
        Dictionary
        SDK object containing to_dict()
        Dictionary-compatible object
    """
    if api_response is None:
        return {}

    if isinstance(api_response, dict):
        return api_response

    if hasattr(api_response, "to_dict"):
        try:
            converted = api_response.to_dict()

            if isinstance(converted, dict):
                return converted
        except Exception as ex:
            logger.debug(
                "Could not convert API response using to_dict(). " "error=%s: %s",
                type(ex).__name__,
                ex,
            )

    try:
        converted = dict(api_response)

        if isinstance(converted, dict):
            return converted
    except Exception:
        pass

    return {}


def extract_candles_from_response(
    api_response: Any,
) -> list:
    """
    Extracts the candle list from an Upstox intraday API response.

    Expected response shape:

        {
            "data": {
                "candles": [...]
            }
        }
    """
    response_dict = response_to_dict(api_response)
    data = response_dict.get("data", {})

    if not isinstance(data, dict):
        return []

    candles = data.get("candles", [])

    if not isinstance(candles, list):
        return []

    return candles


# ============================================================
# Timestamp Helpers
# ============================================================


def _parse_numeric_timestamp(
    timestamp_value: int | float | str,
) -> datetime | None:
    """
    Parses a numeric Unix timestamp.

    Supported units:
        Seconds
        Milliseconds
        Microseconds
        Nanoseconds
    """
    try:
        numeric_timestamp = float(timestamp_value)
    except (TypeError, ValueError, OverflowError):
        return None

    absolute_timestamp = abs(numeric_timestamp)

    # Nanoseconds
    if absolute_timestamp >= 1_000_000_000_000_000_000:
        numeric_timestamp /= 1_000_000_000

    # Microseconds
    elif absolute_timestamp >= 1_000_000_000_000_000:
        numeric_timestamp /= 1_000_000

    # Milliseconds
    elif absolute_timestamp >= 1_000_000_000_000:
        numeric_timestamp /= 1_000

    try:
        return datetime.fromtimestamp(
            numeric_timestamp,
            tz=get_market_timezone(),
        )
    except (ValueError, OverflowError, OSError):
        return None


def parse_candle_timestamp(
    timestamp_value: Any,
) -> datetime | None:
    """
    Parses an Upstox candle timestamp into a timezone-aware datetime.

    Supported values:
        Unix timestamp in seconds
        Unix timestamp in milliseconds
        Unix timestamp in microseconds
        Unix timestamp in nanoseconds
        Numeric timestamp string
        ISO-8601 datetime string
        ISO-8601 datetime string ending with Z
    """
    if timestamp_value is None:
        return None

    if isinstance(timestamp_value, datetime):
        parsed = timestamp_value
        market_tz = get_market_timezone()

        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=market_tz)

        return parsed.astimezone(market_tz)

    if isinstance(timestamp_value, (int, float)):
        return _parse_numeric_timestamp(timestamp_value)

    text = str(timestamp_value).strip()

    if not text:
        return None

    numeric_text = text

    if numeric_text.startswith(("+", "-")):
        numeric_text = numeric_text[1:]

    if numeric_text.replace(".", "", 1).isdigit():
        return _parse_numeric_timestamp(text)

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None

    market_tz = get_market_timezone()

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=market_tz)

    return parsed.astimezone(market_tz)


# ============================================================
# Candle Normalization
# ============================================================


def normalize_candle(
    candle: list | tuple,
) -> dict | None:
    """
    Normalizes one Upstox candle.

    Expected candle shape:

        [
            timestamp,
            open,
            high,
            low,
            close,
            volume,
            oi,
        ]

    Only the first five values are mandatory.
    """
    if not isinstance(candle, (list, tuple)):
        return None

    if len(candle) < 5:
        return None

    candle_dt = parse_candle_timestamp(candle[0])

    if candle_dt is None:
        return None

    return {
        "timestamp": candle_dt.isoformat(),
        "datetime": candle_dt,
        "open": safe_float(candle[1]),
        "high": safe_float(candle[2]),
        "low": safe_float(candle[3]),
        "close": safe_float(candle[4]),
        "volume": (safe_int(candle[5]) if len(candle) > 5 else 0),
        "oi": (safe_int(candle[6]) if len(candle) > 6 else 0),
    }


def normalize_candles(
    candles: list | tuple | None,
) -> list:
    """
    Normalizes, validates, deduplicates, and sorts candle values.

    Duplicate candles are removed using their normalized timestamps.
    The first candle for each timestamp is retained.
    """
    if not isinstance(candles, (list, tuple)):
        return []

    normalized_by_timestamp: dict[str, dict] = {}

    for candle in candles:
        normalized_candle = normalize_candle(candle)

        if normalized_candle is None:
            continue

        timestamp = normalized_candle.get("timestamp")

        if not timestamp:
            continue

        if timestamp not in normalized_by_timestamp:
            normalized_by_timestamp[timestamp] = normalized_candle

    normalized = list(normalized_by_timestamp.values())

    try:
        return sorted(
            normalized,
            key=lambda item: item["datetime"],
        )
    except (KeyError, TypeError, ValueError):
        return normalized


def serialize_candle(
    candle: dict | None,
) -> dict:
    """
    Serializes a normalized candle for JSON output.

    The internal datetime object is intentionally excluded because
    JSON cannot serialize it without custom handling.
    """
    if not isinstance(candle, dict):
        return {
            "timestamp": None,
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "volume": None,
            "oi": None,
        }

    return {
        "timestamp": candle.get("timestamp"),
        "open": candle.get("open"),
        "high": candle.get("high"),
        "low": candle.get("low"),
        "close": candle.get("close"),
        "volume": candle.get("volume"),
        "oi": candle.get("oi"),
    }


# ============================================================
# Opening Range Date Helpers
# ============================================================


def _get_candle_target_date(
    candles: list | tuple | None,
) -> date:
    """
    Returns the date of the first valid candle.

    The current market date is returned when there is no valid candle.
    """
    if isinstance(candles, (list, tuple)):
        for candle in candles:
            if not isinstance(candle, dict):
                continue

            candle_dt = candle.get("datetime")

            if isinstance(candle_dt, datetime):
                return candle_dt.date()

            timestamp = candle.get("timestamp")
            parsed = parse_candle_timestamp(timestamp)

            if parsed is not None:
                return parsed.date()

    return get_now_market_time().date()


def get_market_open_datetime(
    target_date: date | datetime | None = None,
) -> datetime:
    """
    Builds the market-open datetime in the configured timezone.

    When target_date is not provided, the current market date is used.
    """
    market_tz = get_market_timezone()

    if target_date is None:
        normalized_date = get_now_market_time().date()
    elif isinstance(target_date, datetime):
        normalized_date = target_date.date()
    elif isinstance(target_date, date):
        normalized_date = target_date
    else:
        try:
            normalized_date = date.fromisoformat(str(target_date).strip()[:10])
        except (TypeError, ValueError):
            normalized_date = get_now_market_time().date()

    return datetime.combine(
        normalized_date,
        dt_time(
            hour=DEFAULT_MARKET_OPEN_HOUR,
            minute=DEFAULT_MARKET_OPEN_MINUTE,
        ),
        tzinfo=market_tz,
    )


def get_opening_range_end_datetime(
    candle_count: int = DEFAULT_OPENING_RANGE_CANDLE_COUNT,
    target_date: date | datetime | None = None,
) -> datetime:
    """Returns the Opening Range completion datetime."""
    normalized_candle_count = max(
        1,
        safe_int(
            candle_count,
            default=DEFAULT_OPENING_RANGE_CANDLE_COUNT,
        ),
    )

    market_open_dt = get_market_open_datetime(target_date=target_date)

    return market_open_dt + timedelta(minutes=normalized_candle_count)


# ============================================================
# Opening Range Candle Selection
# ============================================================


def select_opening_range_candles(
    candles: list,
    candle_count: int = DEFAULT_OPENING_RANGE_CANDLE_COUNT,
) -> list:
    """
    Selects the first N candles beginning at market-open time.

    The target date is obtained from the candle collection. This allows
    the same helper to work with live, historical, and test candles.
    """
    if not isinstance(candles, list) or not candles:
        return []

    normalized_candle_count = max(
        1,
        safe_int(
            candle_count,
            default=DEFAULT_OPENING_RANGE_CANDLE_COUNT,
        ),
    )

    target_date = _get_candle_target_date(candles)

    market_open_dt = get_market_open_datetime(target_date=target_date)

    selected = []

    for candle in candles:
        if not isinstance(candle, dict):
            continue

        candle_dt = candle.get("datetime")

        if not isinstance(candle_dt, datetime):
            candle_dt = parse_candle_timestamp(candle.get("timestamp"))

        if candle_dt is None:
            continue

        if candle_dt < market_open_dt:
            continue

        selected.append(candle)

        if len(selected) >= normalized_candle_count:
            break

    return selected


def select_post_opening_range_candles(
    candles: list,
    candle_count: int = DEFAULT_OPENING_RANGE_CANDLE_COUNT,
) -> list:
    """
    Selects candles at or after the Opening Range completion time.

    Example:

        Market open: 09:15
        Candle count: 1
        Opening Range ends: 09:16
        First post-Opening-Range candle: 09:16
    """
    if not isinstance(candles, list) or not candles:
        return []

    target_date = _get_candle_target_date(candles)

    opening_range_end_dt = get_opening_range_end_datetime(
        candle_count=candle_count,
        target_date=target_date,
    )

    post_opening_range_candles = []

    for candle in candles:
        if not isinstance(candle, dict):
            continue

        candle_dt = candle.get("datetime")

        if not isinstance(candle_dt, datetime):
            candle_dt = parse_candle_timestamp(candle.get("timestamp"))

        if candle_dt is None:
            continue

        if candle_dt >= opening_range_end_dt:
            post_opening_range_candles.append(candle)

    return post_opening_range_candles


# ============================================================
# Subscribed Instrument Helpers
# ============================================================


def get_subscribed_instrument_keys() -> list:
    """
    Returns unique instrument keys from options_cache.

    The original subscription order is preserved. Empty and invalid
    instrument keys are excluded.
    """
    subscribed_keys = options_cache.get(
        "subscribed_keys",
        [],
    )

    if not isinstance(
        subscribed_keys,
        (list, tuple, set),
    ):
        return []

    unique_keys = []
    seen_keys = set()

    for instrument_key in subscribed_keys:
        if instrument_key is None:
            continue

        normalized_key = str(instrument_key).strip()

        if not normalized_key:
            continue

        if normalized_key in seen_keys:
            continue

        seen_keys.add(normalized_key)
        unique_keys.append(normalized_key)

    return unique_keys


def get_contract_info_by_key(
    instrument_key: str,
) -> dict:
    """
    Returns contract metadata for an instrument key.

    Lookup order:
        1. Main index metadata
        2. Option service lookup
        3. options_cache data
        4. Basic instrument-key fallback
    """
    if instrument_key is None:
        return {}

    normalized_key = str(instrument_key).strip()

    if not normalized_key:
        return {}

    main_key = str(
        getattr(
            config,
            "MAIN_NIFTY_SECURITY",
            "NSE_INDEX|Nifty 50",
        )
        or "NSE_INDEX|Nifty 50"
    ).strip()

    if normalized_key == main_key:
        return {
            "instrument_key": normalized_key,
            "instrument_type": "INDEX",
            "strike_price": None,
            "expiry": None,
            "trading_symbol": "NIFTY 50",
            "underlying_type": "INDEX",
            "underlying_symbol": "NIFTY 50",
        }

    try:
        resolved = get_contract_info_by_instrument_key(normalized_key)
    except Exception as ex:
        logger.warning(
            "Contract lookup failed. " "instrument_key=%s, error=%s: %s",
            normalized_key,
            type(ex).__name__,
            ex,
        )
        resolved = None

    if isinstance(resolved, dict) and resolved:
        return resolved

    cached_contracts = options_cache.get("data", [])

    if isinstance(cached_contracts, list):
        for item in cached_contracts:
            if not isinstance(item, dict):
                continue

            item_key = item.get("instrument_key")

            if item_key == normalized_key:
                return item

    return {
        "instrument_key": normalized_key,
    }


def normalize_option_type(
    instrument_type: Any,
) -> str | None:
    """
    Normalizes an option contract type.

    Supported mappings:
        CE -> CE
        CALL -> CE
        PE -> PE
        PUT -> PE
    """
    normalized_type = str(instrument_type or "").strip().upper()

    if normalized_type in {"CE", "CALL"}:
        return "CE"

    if normalized_type in {"PE", "PUT"}:
        return "PE"

    return None


def is_option_contract(
    contract_info: dict | None,
) -> bool:
    """
    Returns True when the contract is a CE or PE option.

    Both instrument_type and option_type fields are supported.
    """
    if not isinstance(contract_info, dict):
        return False

    instrument_type = contract_info.get("instrument_type")

    if not instrument_type:
        instrument_type = contract_info.get("option_type")

    return normalize_option_type(instrument_type) is not None


# ============================================================
# Public API
# ============================================================


__all__ = [
    # Basic helpers
    "is_opening_range_enabled",
    "get_market_timezone",
    "get_now_market_time",
    "get_live_ema_calculation_mode_text",
    "safe_float",
    "safe_int",
    "response_to_dict",
    "extract_candles_from_response",
    # Timestamp helpers
    "parse_candle_timestamp",
    # Candle normalization
    "normalize_candle",
    "normalize_candles",
    "serialize_candle",
    # Opening Range time helpers
    "get_market_open_datetime",
    "get_opening_range_end_datetime",
    # Candle selection
    "select_opening_range_candles",
    "select_post_opening_range_candles",
    # Instrument helpers
    "get_subscribed_instrument_keys",
    "get_contract_info_by_key",
    "normalize_option_type",
    "is_option_contract",
]
