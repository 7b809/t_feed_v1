"""
Centralized configuration constants for the Opening Range package.

This module must remain independent from the other Opening Range modules.

Allowed imports:
    from core import config

Do not import from:
    services.opening_range
    .state
    .candle_utils
    .live_touch
    .isolation
    .ema_alerts
    .status
    .service

Keeping this module independent helps prevent circular imports.
"""

from typing import Any

from core import config

# ============================================================
# Safe Configuration Conversion Helpers
# ============================================================


def _config_bool(name: str, default: bool) -> bool:
    """
    Reads a boolean configuration value safely.

    Supported string values:
        true, 1, yes, y, on
        false, 0, no, n, off
    """
    value = getattr(config, name, default)

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        normalized = value.strip().lower()

        if normalized in {"true", "1", "yes", "y", "on"}:
            return True

        if normalized in {"false", "0", "no", "n", "off", ""}:
            return False

    return bool(value)


def _config_int(name: str, default: int) -> int:
    """Reads an integer configuration value safely."""
    value = getattr(config, name, default)

    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _config_float(name: str, default: float) -> float:
    """Reads a float configuration value safely."""
    value = getattr(config, name, default)

    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)


def _config_string(name: str, default: str) -> str:
    """Reads a non-empty string configuration value safely."""
    value = getattr(config, name, default)

    if value is None:
        return default

    normalized = str(value).strip()

    return normalized if normalized else default


def _config_string_list(
    name: str,
    default: list[str],
    uppercase: bool = False,
) -> list:
    value: Any = getattr(config, name, default)

    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = list(default)

    output = []
    seen = set()

    for item in items:
        normalized = str(item or "").strip()

        if not normalized:
            continue

        if uppercase:
            normalized = normalized.upper()

        if normalized in seen:
            continue

        seen.add(normalized)
        output.append(normalized)

    if output:
        return output

    fallback = []

    for item in default:
        normalized = str(item or "").strip()

        if not normalized:
            continue

        if uppercase:
            normalized = normalized.upper()

        if normalized not in fallback:
            fallback.append(normalized)

    return fallback


# ============================================================
# Main Opening Range Configuration
# ============================================================


DEFAULT_OPENING_RANGE_ENABLED = _config_bool(
    "OPENING_RANGE_ENABLED",
    True,
)

DEFAULT_OPENING_RANGE_INTERVAL = _config_string(
    "OPENING_RANGE_INTERVAL",
    "1minute",
)

DEFAULT_OPENING_RANGE_CANDLE_COUNT = max(
    1,
    _config_int(
        "OPENING_RANGE_CANDLE_COUNT",
        1,
    ),
)


# ============================================================
# Market Time Configuration
# ============================================================


DEFAULT_MARKET_TIMEZONE = _config_string(
    "MARKET_TIMEZONE",
    "Asia/Kolkata",
)

DEFAULT_MARKET_OPEN_HOUR = min(
    23,
    max(
        0,
        _config_int(
            "OPENING_RANGE_MARKET_OPEN_HOUR",
            9,
        ),
    ),
)

DEFAULT_MARKET_OPEN_MINUTE = min(
    59,
    max(
        0,
        _config_int(
            "OPENING_RANGE_MARKET_OPEN_MINUTE",
            15,
        ),
    ),
)

DEFAULT_FETCH_HOUR = min(
    23,
    max(
        0,
        _config_int(
            "OPENING_RANGE_FETCH_HOUR",
            9,
        ),
    ),
)

DEFAULT_FETCH_MINUTE = min(
    59,
    max(
        0,
        _config_int(
            "OPENING_RANGE_FETCH_MINUTE",
            18,
        ),
    ),
)


# ============================================================
# Intraday Candle API Configuration
# ============================================================


DEFAULT_INTRADAY_UNIT = _config_string(
    "OPENING_RANGE_INTRADAY_UNIT",
    "minutes",
)

DEFAULT_INTRADAY_INTERVAL = _config_string(
    "OPENING_RANGE_INTRADAY_INTERVAL",
    "1",
)

DEFAULT_MAX_WORKERS = max(
    1,
    _config_int(
        "OPENING_RANGE_MAX_WORKERS",
        5,
    ),
)

DEFAULT_SLEEP_SECONDS = max(
    0.0,
    _config_float(
        "OPENING_RANGE_REQUEST_SLEEP_SECONDS",
        0.15,
    ),
)


# ============================================================
# Opening Range Result Storage
# ============================================================


DEFAULT_SAVE_FILE = _config_bool(
    "OPENING_RANGE_SAVE_FILE",
    True,
)

DEFAULT_OUTPUT_FILE = _config_string(
    "OPENING_RANGE_OUTPUT_FILE",
    "data/opening_range_results.json",
)


# ============================================================
# Opening Range Backfill Configuration
# ============================================================


DEFAULT_BACKFILL_SCAN_ENABLED = _config_bool(
    "OPENING_RANGE_BACKFILL_TOUCH_SCAN_ENABLED",
    True,
)


# ============================================================
# Touch Alert Configuration
# ============================================================


DEFAULT_TOUCH_ALERT_ENABLED = _config_bool(
    "OPENING_RANGE_TOUCH_ALERT_ENABLED",
    True,
)

DEFAULT_BACKFILL_TOUCH_ALERT_ENABLED = _config_bool(
    "OPENING_RANGE_BACKFILL_TOUCH_ALERT_ENABLED",
    True,
)

DEFAULT_LIVE_TOUCH_ALERT_ENABLED = _config_bool(
    "OPENING_RANGE_LIVE_TOUCH_ALERT_ENABLED",
    True,
)

DEFAULT_TOUCH_ALERT_MAX_INSTRUMENTS = max(
    1,
    _config_int(
        "OPENING_RANGE_TOUCH_ALERT_MAX_INSTRUMENTS",
        5,
    ),
)

DEFAULT_TOUCH_ALERT_BATCH_SECONDS = max(
    0,
    _config_int(
        "OPENING_RANGE_TOUCH_ALERT_BATCH_SECONDS",
        10,
    ),
)

DEFAULT_TOUCH_ALERT_ONCE_PER_LEVEL = _config_bool(
    "OPENING_RANGE_TOUCH_ALERT_ONCE_PER_LEVEL",
    True,
)

DEFAULT_TOUCH_ALERT_OPTIONS_ONLY = _config_bool(
    "OPENING_RANGE_TOUCH_ALERT_OPTIONS_ONLY",
    True,
)

DEFAULT_SORT_BY_NEAREST_INDEX = _config_bool(
    "OPENING_RANGE_TOUCH_ALERT_SORT_BY_NEAREST_INDEX",
    True,
)

DEFAULT_MAIN_INDEX_KEY = _config_string(
    "OPENING_RANGE_TOUCH_ALERT_MAIN_INDEX_KEY",
    _config_string(
        "MAIN_NIFTY_SECURITY",
        "NSE_INDEX|Nifty 50",
    ),
)

DEFAULT_TOUCH_CHECK_MODE = _config_string(
    "OPENING_RANGE_TOUCH_CHECK_MODE",
    "high_low",
).lower()

if DEFAULT_TOUCH_CHECK_MODE not in {"high_low", "ltp"}:
    DEFAULT_TOUCH_CHECK_MODE = "high_low"


# ============================================================
# Touch Event Storage
# ============================================================


DEFAULT_TOUCH_EVENTS_OUTPUT_FILE = _config_string(
    "OPENING_RANGE_TOUCH_EVENTS_OUTPUT_FILE",
    "data/opening_range_touch_events.json",
)

DEFAULT_TOUCH_EVENTS_SAVE_TEST_FILE = _config_bool(
    "OPENING_RANGE_TOUCH_EVENTS_SAVE_TEST_FILE",
    True,
)

DEFAULT_MAX_EVENTS_IN_MEMORY = max(
    1,
    _config_int(
        "OPENING_RANGE_MAX_EVENTS_IN_MEMORY",
        5000,
    ),
)


# ============================================================
# Legacy Touch Telegram Configuration
# ============================================================


DEFAULT_LEGACY_TOUCH_TELEGRAM_ENABLED = _config_bool(
    "OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED",
    False,
)


# ============================================================
# EMA Opening Range Enrichment
# ============================================================


DEFAULT_EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS = _config_bool(
    "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
    True,
)


# ============================================================
# Isolated Instrument Configuration
# ============================================================


DEFAULT_ISOLATION_ENABLED = _config_bool(
    "OPENING_RANGE_ISOLATED_INSTRUMENT_ENABLED",
    True,
)

DEFAULT_ISOLATION_WINDOW_POINTS = max(
    0.0,
    _config_float(
        "OPENING_RANGE_ISOLATION_AVERAGE_WINDOW_POINTS",
        500.0,
    ),
)

DEFAULT_ISOLATION_TOUCH_LEVELS = _config_string_list(
    "OPENING_RANGE_ISOLATION_TOUCH_LEVELS",
    ["R2", "R3", "S2", "S3"],
    uppercase=True,
)

DEFAULT_ISOLATION_PRIORITY_LEVELS = _config_string_list(
    "OPENING_RANGE_ISOLATION_PRIORITY_LEVELS",
    ["R3", "S3", "R2", "S2"],
    uppercase=True,
)

DEFAULT_ISOLATION_LOCK_FOR_DAY = _config_bool(
    "OPENING_RANGE_ISOLATION_LOCK_FOR_DAY",
    True,
)

DEFAULT_ISOLATION_ALLOW_PRIORITY_UPGRADE = _config_bool(
    "OPENING_RANGE_ISOLATION_ALLOW_PRIORITY_UPGRADE",
    True,
)

DEFAULT_ISOLATED_NOTIFY_ENABLED = _config_bool(
    "OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED",
    True,
)

DEFAULT_ISOLATION_ALLOW_BACKFILL_TOUCH = _config_bool(
    "OPENING_RANGE_ISOLATION_ALLOW_BACKFILL_TOUCH",
    True,
)

DEFAULT_ISOLATION_ALLOW_LIVE_TOUCH = _config_bool(
    "OPENING_RANGE_ISOLATION_ALLOW_LIVE_TOUCH",
    True,
)

DEFAULT_ISOLATION_OPTIONS_ONLY = _config_bool(
    "OPENING_RANGE_ISOLATION_OPTIONS_ONLY",
    True,
)


# ============================================================
# Isolated EMA Telegram Configuration
# ============================================================


DEFAULT_EMA_ISOLATED_TELEGRAM_ENABLED = _config_bool(
    "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED",
    True,
)


# ============================================================
# Live EMA Calculation Configuration
# ============================================================


DEFAULT_LIVE_EMA_CALCULATION_MODE = _config_bool(
    "LIVE_EMA_CALCULATION_MODE",
    False,
)


# ============================================================
# Strike Range Configuration
# ============================================================


DEFAULT_STRIKE_FROM = _config_float(
    "STRIKE_FROM",
    0.0,
)

DEFAULT_STRIKE_TO = _config_float(
    "STRIKE_TO",
    999999.0,
)

if DEFAULT_STRIKE_FROM > DEFAULT_STRIKE_TO:
    DEFAULT_STRIKE_FROM, DEFAULT_STRIKE_TO = (
        DEFAULT_STRIKE_TO,
        DEFAULT_STRIKE_FROM,
    )


# ============================================================
# Test Configuration
# ============================================================


DEFAULT_TEST_FLAG = _config_bool(
    "TEST_FLAG",
    False,
)


# ============================================================
# Public Constants
# ============================================================


__all__ = [
    # Opening Range
    "DEFAULT_OPENING_RANGE_ENABLED",
    "DEFAULT_OPENING_RANGE_INTERVAL",
    "DEFAULT_OPENING_RANGE_CANDLE_COUNT",
    # Market time
    "DEFAULT_MARKET_TIMEZONE",
    "DEFAULT_MARKET_OPEN_HOUR",
    "DEFAULT_MARKET_OPEN_MINUTE",
    "DEFAULT_FETCH_HOUR",
    "DEFAULT_FETCH_MINUTE",
    # Intraday API
    "DEFAULT_INTRADAY_UNIT",
    "DEFAULT_INTRADAY_INTERVAL",
    "DEFAULT_MAX_WORKERS",
    "DEFAULT_SLEEP_SECONDS",
    # Result storage
    "DEFAULT_SAVE_FILE",
    "DEFAULT_OUTPUT_FILE",
    # Backfill
    "DEFAULT_BACKFILL_SCAN_ENABLED",
    # Touch alerts
    "DEFAULT_TOUCH_ALERT_ENABLED",
    "DEFAULT_BACKFILL_TOUCH_ALERT_ENABLED",
    "DEFAULT_LIVE_TOUCH_ALERT_ENABLED",
    "DEFAULT_TOUCH_ALERT_MAX_INSTRUMENTS",
    "DEFAULT_TOUCH_ALERT_BATCH_SECONDS",
    "DEFAULT_TOUCH_ALERT_ONCE_PER_LEVEL",
    "DEFAULT_TOUCH_ALERT_OPTIONS_ONLY",
    "DEFAULT_SORT_BY_NEAREST_INDEX",
    "DEFAULT_MAIN_INDEX_KEY",
    "DEFAULT_TOUCH_CHECK_MODE",
    # Touch event storage
    "DEFAULT_TOUCH_EVENTS_OUTPUT_FILE",
    "DEFAULT_TOUCH_EVENTS_SAVE_TEST_FILE",
    "DEFAULT_MAX_EVENTS_IN_MEMORY",
    # Legacy Telegram
    "DEFAULT_LEGACY_TOUCH_TELEGRAM_ENABLED",
    # EMA enrichment
    "DEFAULT_EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
    # Isolation
    "DEFAULT_ISOLATION_ENABLED",
    "DEFAULT_ISOLATION_WINDOW_POINTS",
    "DEFAULT_ISOLATION_TOUCH_LEVELS",
    "DEFAULT_ISOLATION_PRIORITY_LEVELS",
    "DEFAULT_ISOLATION_LOCK_FOR_DAY",
    "DEFAULT_ISOLATION_ALLOW_PRIORITY_UPGRADE",
    "DEFAULT_ISOLATED_NOTIFY_ENABLED",
    "DEFAULT_ISOLATION_ALLOW_BACKFILL_TOUCH",
    "DEFAULT_ISOLATION_ALLOW_LIVE_TOUCH",
    "DEFAULT_ISOLATION_OPTIONS_ONLY",
    # Isolated EMA alerts
    "DEFAULT_EMA_ISOLATED_TELEGRAM_ENABLED",
    # Live EMA
    "DEFAULT_LIVE_EMA_CALCULATION_MODE",
    # Strike range
    "DEFAULT_STRIKE_FROM",
    "DEFAULT_STRIKE_TO",
    # Test mode
    "DEFAULT_TEST_FLAG",
]
