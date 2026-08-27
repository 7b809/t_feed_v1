from typing import Any

from core import config


def _config_bool(
    name: str,
    default: bool,
):
    value = getattr(config, name, default)

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        normalized = value.strip().lower()

        if normalized in {
            "true",
            "1",
            "yes",
            "y",
            "on",
        }:
            return True

        if normalized in {
            "false",
            "0",
            "no",
            "n",
            "off",
            "",
        }:
            return False

    return bool(value)


def _config_int(
    name: str,
    default: int,
):
    value = getattr(config, name, default)

    try:
        return int(value)
    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return int(default)


def _config_float(
    name: str,
    default: float,
):
    value = getattr(config, name, default)

    try:
        return float(value)
    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return float(default)


def _config_string(
    name: str,
    default: str,
):
    value = getattr(config, name, default)

    if value is None:
        return default

    normalized = str(value).strip()

    return normalized if normalized else default


def _config_string_list(
    name: str,
    default: list[str],
    uppercase: bool = False,
):
    value: Any = getattr(
        config,
        name,
        default,
    )

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


def _config_int_list(
    name: str,
    default: list[int],
):
    value: Any = getattr(
        config,
        name,
        default,
    )

    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = list(default)

    output = []

    for item in items:
        try:
            normalized = int(item)
        except (
            TypeError,
            ValueError,
            OverflowError,
        ):
            continue

        if normalized not in output:
            output.append(normalized)

    return output if output else list(default)


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

DEFAULT_SAVE_FILE = _config_bool(
    "OPENING_RANGE_SAVE_FILE",
    True,
)

DEFAULT_OUTPUT_FILE = _config_string(
    "OPENING_RANGE_OUTPUT_FILE",
    "data/opening_range_results.json",
)

DEFAULT_MAX_EVENTS_IN_MEMORY = max(
    1,
    _config_int(
        "OPENING_RANGE_MAX_EVENTS_IN_MEMORY",
        5000,
    ),
)

DEFAULT_BACKFILL_SCAN_ENABLED = _config_bool(
    "OPENING_RANGE_BACKFILL_TOUCH_SCAN_ENABLED",
    True,
)

DEFAULT_BACKFILL_SCAN_SOURCE = _config_string(
    "OPENING_RANGE_BACKFILL_TOUCH_SCAN_SOURCE",
    "intraday_api",
)

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

if DEFAULT_TOUCH_CHECK_MODE not in {
    "high_low",
    "ltp",
}:
    DEFAULT_TOUCH_CHECK_MODE = "high_low"

DEFAULT_STORE_TOUCH_STATUS = _config_bool(
    "OPENING_RANGE_STORE_TOUCH_STATUS",
    True,
)

DEFAULT_TOUCH_EVENTS_OUTPUT_FILE = _config_string(
    "OPENING_RANGE_TOUCH_EVENTS_OUTPUT_FILE",
    "data/opening_range_touch_events.json",
)

DEFAULT_TOUCH_EVENTS_SAVE_TEST_FILE = _config_bool(
    "OPENING_RANGE_TOUCH_EVENTS_SAVE_TEST_FILE",
    True,
)

DEFAULT_LEGACY_TOUCH_TELEGRAM_ENABLED = _config_bool(
    "OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED",
    False,
)

DEFAULT_EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS = _config_bool(
    "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
    True,
)

DEFAULT_EMA_CROSS_BROADCAST_WITHOUT_OPENING_RANGE = _config_bool(
    "EMA_CROSS_BROADCAST_WITHOUT_OPENING_RANGE",
    True,
)

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
    name="OPENING_RANGE_ISOLATION_TOUCH_LEVELS",
    default=[
        "R2",
        "R3",
        "S2",
        "S3",
    ],
    uppercase=True,
)

DEFAULT_ISOLATION_PRIORITY_LEVELS = _config_string_list(
    name="OPENING_RANGE_ISOLATION_PRIORITY_LEVELS",
    default=[
        "R3",
        "S3",
        "R2",
        "S2",
    ],
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

DEFAULT_ISOLATION_OUTPUT_FILE = _config_string(
    "OPENING_RANGE_ISOLATED_INSTRUMENT_OUTPUT_FILE",
    "data/isolated_opening_range_instrument.json",
)

DEFAULT_ISOLATION_RESET_DAILY = _config_bool(
    "OPENING_RANGE_ISOLATION_RESET_DAILY",
    True,
)

DEFAULT_FIRST_TOUCH_SELECTION_ENABLED = _config_bool(
    "OPENING_RANGE_FIRST_TOUCH_SELECTION_ENABLED",
    True,
)

DEFAULT_FIRST_TOUCH_SELECTION_SOURCE = _config_string(
    "OPENING_RANGE_FIRST_TOUCH_SELECTION_SOURCE",
    "average_window_level_priority",
)

DEFAULT_SELECTED_OR_TOUCH_NOTIFY_ENABLED = _config_bool(
    "OPENING_RANGE_SELECTED_OR_TOUCH_NOTIFY_ENABLED",
    True,
)

DEFAULT_SELECTED_OR_EMA_ALERT_ENABLED = _config_bool(
    "OPENING_RANGE_SELECTED_OR_EMA_ALERT_ENABLED",
    True,
)

DEFAULT_SELECTED_OR_EMA_ALERT_ONCE_PER_CROSS = _config_bool(
    "OPENING_RANGE_SELECTED_OR_EMA_ALERT_ONCE_PER_CROSS",
    False,
)

DEFAULT_EMA_ISOLATED_TELEGRAM_ENABLED = _config_bool(
    "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED",
    True,
)

DEFAULT_EMA_ISOLATED_ALERT_EVERY_CROSS = _config_bool(
    "EMA_ISOLATED_ALERT_EVERY_CROSS",
    True,
)

DEFAULT_EMA_INCLUDE_LEVEL_NAME = _config_bool(
    "EMA_ISOLATED_ALERT_INCLUDE_LEVEL_NAME",
    True,
)

DEFAULT_EMA_INCLUDE_NIFTY_LTP = _config_bool(
    "EMA_ISOLATED_ALERT_INCLUDE_NIFTY_LTP",
    True,
)

DEFAULT_EMA_INCLUDE_DETAILS = _config_bool(
    "EMA_ISOLATED_ALERT_INCLUDE_EMA_DETAILS",
    True,
)

DEFAULT_EMA_INCLUDE_NEAREST_INSTRUMENTS = _config_bool(
    "EMA_ISOLATED_ALERT_INCLUDE_NEAREST_ORDER_INSTRUMENTS",
    True,
)

DEFAULT_EMA_INCLUDE_BUDGET_INSTRUMENTS = _config_bool(
    "EMA_ISOLATED_ALERT_INCLUDE_BUDGET_INSTRUMENTS",
    True,
)

DEFAULT_EMA_INCLUDE_CANDLE_CLOSE = _config_bool(
    "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_CLOSE",
    True,
)

DEFAULT_EMA_INCLUDE_CANDLE_LOW = _config_bool(
    "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_LOW",
    True,
)

DEFAULT_EMA_INCLUDE_CLOSE_LOW_DIFFERENCE = _config_bool(
    "EMA_ISOLATED_ALERT_INCLUDE_CLOSE_LOW_DIFFERENCE",
    True,
)

DEFAULT_EMA_INCLUDE_CANDLE_TIME = _config_bool(
    "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_TIME",
    True,
)

DEFAULT_EMA_PRICE_DECIMAL_PLACES = max(
    0,
    _config_int(
        "EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES",
        2,
    ),
)

DEFAULT_EMA_BULLISH_OPTION_TYPE = _config_string(
    "EMA_ALERT_BULLISH_OPTION_TYPE",
    "CE",
).upper()

DEFAULT_EMA_BEARISH_OPTION_TYPE = _config_string(
    "EMA_ALERT_BEARISH_OPTION_TYPE",
    "PE",
).upper()

DEFAULT_EMA_STRIKE_STEP = max(
    1,
    _config_int(
        "EMA_ALERT_STRIKE_STEP",
        50,
    ),
)

DEFAULT_EMA_NEAREST_STRIKE_COUNT = max(
    1,
    _config_int(
        "EMA_ALERT_NEAREST_STRIKE_COUNT",
        3,
    ),
)

DEFAULT_EMA_NEAREST_STRIKE_OFFSETS = _config_int_list(
    name="EMA_ALERT_NEAREST_STRIKE_OFFSETS",
    default=[
        -50,
        0,
        50,
    ],
)

DEFAULT_EMA_CLAMP_ORDER_STRIKES = _config_bool(
    "EMA_ALERT_ORDER_STRIKES_CLAMP_TO_FILTER_RANGE",
    True,
)

DEFAULT_EMA_INCLUDE_ORDER_INSTRUMENT_LTP = _config_bool(
    "EMA_ALERT_INCLUDE_ORDER_INSTRUMENT_LTP",
    True,
)

DEFAULT_EMA_SHOW_INSTRUMENT_WITHOUT_LTP = _config_bool(
    "EMA_ALERT_SHOW_ORDER_INSTRUMENT_WHEN_LTP_MISSING",
    True,
)

DEFAULT_EMA_MAX_ORDER_INSTRUMENTS = max(
    1,
    _config_int(
        "EMA_ALERT_MAX_ORDER_INSTRUMENTS",
        3,
    ),
)

DEFAULT_EMA_BUDGET_RANGE_ENABLED = _config_bool(
    "EMA_ALERT_BUDGET_RANGE_ENABLED",
    True,
)

DEFAULT_EMA_BUDGET_MIN_PRICE = _config_float(
    "EMA_ALERT_BUDGET_MIN_PRICE",
    20.0,
)

DEFAULT_EMA_BUDGET_MAX_PRICE = _config_float(
    "EMA_ALERT_BUDGET_MAX_PRICE",
    30.0,
)

if DEFAULT_EMA_BUDGET_MIN_PRICE > DEFAULT_EMA_BUDGET_MAX_PRICE:
    (
        DEFAULT_EMA_BUDGET_MIN_PRICE,
        DEFAULT_EMA_BUDGET_MAX_PRICE,
    ) = (
        DEFAULT_EMA_BUDGET_MAX_PRICE,
        DEFAULT_EMA_BUDGET_MIN_PRICE,
    )

DEFAULT_EMA_BUDGET_MAX_INSTRUMENTS = max(
    1,
    _config_int(
        "EMA_ALERT_BUDGET_MAX_INSTRUMENTS",
        2,
    ),
)

DEFAULT_EMA_BUDGET_USE_SUGGESTED_SIDE = _config_bool(
    "EMA_ALERT_BUDGET_USE_SUGGESTED_ORDER_SIDE",
    True,
)

DEFAULT_EMA_BUDGET_SUBSCRIBED_ONLY = _config_bool(
    "EMA_ALERT_BUDGET_SUBSCRIBED_ONLY",
    True,
)

DEFAULT_EMA_BUDGET_REQUIRE_LIVE_LTP = _config_bool(
    "EMA_ALERT_BUDGET_REQUIRE_LIVE_LTP",
    True,
)

DEFAULT_EMA_BUDGET_SORT_MODE = _config_string(
    "EMA_ALERT_BUDGET_SORT_MODE",
    "nearest_to_budget_midpoint",
).lower()

if DEFAULT_EMA_BUDGET_SORT_MODE not in {
    "nearest_to_budget_midpoint",
    "nearest_to_nifty",
    "price_ascending",
    "price_descending",
}:
    DEFAULT_EMA_BUDGET_SORT_MODE = "nearest_to_budget_midpoint"

DEFAULT_EMA_BUDGET_RANGE_INCLUSIVE = _config_bool(
    "EMA_ALERT_BUDGET_RANGE_INCLUSIVE",
    True,
)

DEFAULT_ALGO_APP_ENABLED = _config_bool(
    "ALGO_APP_ENABLED",
    False,
)

DEFAULT_ALGO_APP_TIMEOUT_SECONDS = max(
    0.0,
    _config_float(
        "ALGO_APP_TIMEOUT_SECONDS",
        10.0,
    ),
)

DEFAULT_ALGO_APP_VERIFY_SSL = _config_bool(
    "ALGO_APP_VERIFY_SSL",
    True,
)

DEFAULT_ALGO_APP_MAX_RETRIES = max(
    0,
    _config_int(
        "ALGO_APP_MAX_RETRIES",
        3,
    ),
)

DEFAULT_ALGO_APP_RETRY_DELAY_SECONDS = max(
    0.0,
    _config_float(
        "ALGO_APP_RETRY_DELAY_SECONDS",
        2.0,
    ),
)

DEFAULT_ALGO_APP_SEND_IN_BACKGROUND = _config_bool(
    "ALGO_APP_SEND_IN_BACKGROUND",
    True,
)

DEFAULT_ALGO_APP_INCLUDE_EVENT_ID = _config_bool(
    "ALGO_APP_INCLUDE_EVENT_ID",
    True,
)

DEFAULT_ALGO_APP_PAYLOAD_SCHEMA_VERSION = _config_string(
    "ALGO_APP_PAYLOAD_SCHEMA_VERSION",
    "1.0",
)

DEFAULT_ALGO_APP_SOURCE_NAME = _config_string(
    "ALGO_APP_SOURCE_NAME",
    "option_feed_engine",
)

DEFAULT_ALGO_APP_MAX_RESPONSE_BODY_LENGTH = max(
    0,
    _config_int(
        "ALGO_APP_MAX_RESPONSE_BODY_LENGTH",
        2000,
    ),
)

DEFAULT_EMA_ALGO_INCLUDE_OPENING_RANGE = _config_bool(
    "EMA_ALGO_PAYLOAD_INCLUDE_OPENING_RANGE",
    True,
)

DEFAULT_EMA_ALGO_INCLUDE_EMA_VALUES = _config_bool(
    "EMA_ALGO_PAYLOAD_INCLUDE_EMA_VALUES",
    True,
)

DEFAULT_EMA_ALGO_INCLUDE_CANDLE = _config_bool(
    "EMA_ALGO_PAYLOAD_INCLUDE_CANDLE",
    True,
)

DEFAULT_EMA_ALGO_INCLUDE_NEAREST_INSTRUMENTS = _config_bool(
    "EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENTS",
    True,
)

DEFAULT_EMA_ALGO_INCLUDE_BUDGET_INSTRUMENTS = _config_bool(
    "EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENTS",
    True,
)

DEFAULT_EMA_ALGO_INCLUDE_DELIVERY_METADATA = _config_bool(
    "EMA_ALGO_PAYLOAD_INCLUDE_DELIVERY_METADATA",
    False,
)

DEFAULT_LIVE_EMA_ENABLED = _config_bool(
    "LIVE_EMA_ENABLED",
    True,
)

DEFAULT_LIVE_EMA_CALCULATION_MODE = _config_bool(
    "LIVE_EMA_CALCULATION_MODE",
    False,
)

DEFAULT_LIVE_EMA_INTERVAL_MINUTES = max(
    1,
    _config_int(
        "LIVE_EMA_INTERVAL_MINUTES",
        1,
    ),
)

DEFAULT_LIVE_EMA_FAST_PERIOD = max(
    1,
    _config_int(
        "LIVE_EMA_FAST_PERIOD",
        9,
    ),
)

DEFAULT_LIVE_EMA_SLOW_PERIOD = max(
    1,
    _config_int(
        "LIVE_EMA_SLOW_PERIOD",
        21,
    ),
)

DEFAULT_LIVE_EMA_TICK_ALERT_ONCE_PER_DIRECTION = _config_bool(
    "LIVE_EMA_TICK_ALERT_ONCE_PER_DIRECTION",
    True,
)

DEFAULT_LIVE_EMA_TICK_MIN_PRICE_CHANGE = max(
    0.0,
    _config_float(
        "LIVE_EMA_TICK_MIN_PRICE_CHANGE",
        0.0,
    ),
)

DEFAULT_LIVE_EMA_SAVE_TEST_FILE = _config_bool(
    "LIVE_EMA_SAVE_TEST_FILE",
    True,
)

DEFAULT_LIVE_EMA_OUTPUT_FILE = _config_string(
    "LIVE_EMA_OUTPUT_FILE",
    "data/live_ema_cross_results.json",
)

DEFAULT_LIVE_EMA_MAX_EVENTS_IN_MEMORY = max(
    1,
    _config_int(
        "LIVE_EMA_MAX_EVENTS_IN_MEMORY",
        5000,
    ),
)

DEFAULT_STRIKE_FROM = _config_float(
    "STRIKE_FROM",
    0.0,
)

DEFAULT_STRIKE_TO = _config_float(
    "STRIKE_TO",
    999999.0,
)

if DEFAULT_STRIKE_FROM > DEFAULT_STRIKE_TO:
    (
        DEFAULT_STRIKE_FROM,
        DEFAULT_STRIKE_TO,
    ) = (
        DEFAULT_STRIKE_TO,
        DEFAULT_STRIKE_FROM,
    )

DEFAULT_TEST_FLAG = _config_bool(
    "TEST_FLAG",
    False,
)


__all__ = [
    "DEFAULT_OPENING_RANGE_ENABLED",
    "DEFAULT_OPENING_RANGE_INTERVAL",
    "DEFAULT_OPENING_RANGE_CANDLE_COUNT",
    "DEFAULT_MARKET_TIMEZONE",
    "DEFAULT_MARKET_OPEN_HOUR",
    "DEFAULT_MARKET_OPEN_MINUTE",
    "DEFAULT_FETCH_HOUR",
    "DEFAULT_FETCH_MINUTE",
    "DEFAULT_INTRADAY_UNIT",
    "DEFAULT_INTRADAY_INTERVAL",
    "DEFAULT_MAX_WORKERS",
    "DEFAULT_SLEEP_SECONDS",
    "DEFAULT_SAVE_FILE",
    "DEFAULT_OUTPUT_FILE",
    "DEFAULT_MAX_EVENTS_IN_MEMORY",
    "DEFAULT_BACKFILL_SCAN_ENABLED",
    "DEFAULT_BACKFILL_SCAN_SOURCE",
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
    "DEFAULT_STORE_TOUCH_STATUS",
    "DEFAULT_TOUCH_EVENTS_OUTPUT_FILE",
    "DEFAULT_TOUCH_EVENTS_SAVE_TEST_FILE",
    "DEFAULT_LEGACY_TOUCH_TELEGRAM_ENABLED",
    "DEFAULT_EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
    "DEFAULT_EMA_CROSS_BROADCAST_WITHOUT_OPENING_RANGE",
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
    "DEFAULT_ISOLATION_OUTPUT_FILE",
    "DEFAULT_ISOLATION_RESET_DAILY",
    "DEFAULT_FIRST_TOUCH_SELECTION_ENABLED",
    "DEFAULT_FIRST_TOUCH_SELECTION_SOURCE",
    "DEFAULT_SELECTED_OR_TOUCH_NOTIFY_ENABLED",
    "DEFAULT_SELECTED_OR_EMA_ALERT_ENABLED",
    "DEFAULT_SELECTED_OR_EMA_ALERT_ONCE_PER_CROSS",
    "DEFAULT_EMA_ISOLATED_TELEGRAM_ENABLED",
    "DEFAULT_EMA_ISOLATED_ALERT_EVERY_CROSS",
    "DEFAULT_EMA_INCLUDE_LEVEL_NAME",
    "DEFAULT_EMA_INCLUDE_NIFTY_LTP",
    "DEFAULT_EMA_INCLUDE_DETAILS",
    "DEFAULT_EMA_INCLUDE_NEAREST_INSTRUMENTS",
    "DEFAULT_EMA_INCLUDE_BUDGET_INSTRUMENTS",
    "DEFAULT_EMA_INCLUDE_CANDLE_CLOSE",
    "DEFAULT_EMA_INCLUDE_CANDLE_LOW",
    "DEFAULT_EMA_INCLUDE_CLOSE_LOW_DIFFERENCE",
    "DEFAULT_EMA_INCLUDE_CANDLE_TIME",
    "DEFAULT_EMA_PRICE_DECIMAL_PLACES",
    "DEFAULT_EMA_BULLISH_OPTION_TYPE",
    "DEFAULT_EMA_BEARISH_OPTION_TYPE",
    "DEFAULT_EMA_STRIKE_STEP",
    "DEFAULT_EMA_NEAREST_STRIKE_COUNT",
    "DEFAULT_EMA_NEAREST_STRIKE_OFFSETS",
    "DEFAULT_EMA_CLAMP_ORDER_STRIKES",
    "DEFAULT_EMA_INCLUDE_ORDER_INSTRUMENT_LTP",
    "DEFAULT_EMA_SHOW_INSTRUMENT_WITHOUT_LTP",
    "DEFAULT_EMA_MAX_ORDER_INSTRUMENTS",
    "DEFAULT_EMA_BUDGET_RANGE_ENABLED",
    "DEFAULT_EMA_BUDGET_MIN_PRICE",
    "DEFAULT_EMA_BUDGET_MAX_PRICE",
    "DEFAULT_EMA_BUDGET_MAX_INSTRUMENTS",
    "DEFAULT_EMA_BUDGET_USE_SUGGESTED_SIDE",
    "DEFAULT_EMA_BUDGET_SUBSCRIBED_ONLY",
    "DEFAULT_EMA_BUDGET_REQUIRE_LIVE_LTP",
    "DEFAULT_EMA_BUDGET_SORT_MODE",
    "DEFAULT_EMA_BUDGET_RANGE_INCLUSIVE",
    "DEFAULT_ALGO_APP_ENABLED",
    "DEFAULT_ALGO_APP_TIMEOUT_SECONDS",
    "DEFAULT_ALGO_APP_VERIFY_SSL",
    "DEFAULT_ALGO_APP_MAX_RETRIES",
    "DEFAULT_ALGO_APP_RETRY_DELAY_SECONDS",
    "DEFAULT_ALGO_APP_SEND_IN_BACKGROUND",
    "DEFAULT_ALGO_APP_INCLUDE_EVENT_ID",
    "DEFAULT_ALGO_APP_PAYLOAD_SCHEMA_VERSION",
    "DEFAULT_ALGO_APP_SOURCE_NAME",
    "DEFAULT_ALGO_APP_MAX_RESPONSE_BODY_LENGTH",
    "DEFAULT_EMA_ALGO_INCLUDE_OPENING_RANGE",
    "DEFAULT_EMA_ALGO_INCLUDE_EMA_VALUES",
    "DEFAULT_EMA_ALGO_INCLUDE_CANDLE",
    "DEFAULT_EMA_ALGO_INCLUDE_NEAREST_INSTRUMENTS",
    "DEFAULT_EMA_ALGO_INCLUDE_BUDGET_INSTRUMENTS",
    "DEFAULT_EMA_ALGO_INCLUDE_DELIVERY_METADATA",
    "DEFAULT_LIVE_EMA_ENABLED",
    "DEFAULT_LIVE_EMA_CALCULATION_MODE",
    "DEFAULT_LIVE_EMA_INTERVAL_MINUTES",
    "DEFAULT_LIVE_EMA_FAST_PERIOD",
    "DEFAULT_LIVE_EMA_SLOW_PERIOD",
    "DEFAULT_LIVE_EMA_TICK_ALERT_ONCE_PER_DIRECTION",
    "DEFAULT_LIVE_EMA_TICK_MIN_PRICE_CHANGE",
    "DEFAULT_LIVE_EMA_SAVE_TEST_FILE",
    "DEFAULT_LIVE_EMA_OUTPUT_FILE",
    "DEFAULT_LIVE_EMA_MAX_EVENTS_IN_MEMORY",
    "DEFAULT_STRIKE_FROM",
    "DEFAULT_STRIKE_TO",
    "DEFAULT_TEST_FLAG",
]
