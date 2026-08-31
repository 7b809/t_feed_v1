import os
from dotenv import load_dotenv

load_dotenv()


# Hard Coded Configs

OPENING_RANGE_ISOLATION_ALLOW_PRIORITY_UPGRADE = True
ALGO_TELE_APP = True

# ============================================================
# MongoDB Configuration
# ============================================================

MONGO_URI = os.getenv("MONGO_URL")
MONGO_DB = os.getenv("MONGO_DB")
TOKENS_COLLECTION = os.getenv("TOKENS_COLLECTION")

REFRESH_INTERVAL_MINUTES = int(os.getenv("REFRESH_INTERVAL_MINUTES", "60"))


# ============================================================
# Telegram Notification Configuration
# ============================================================

TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TELEGRAM_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "10"))


# ============================================================
# Upstox Token Monitor Configuration
# ============================================================

UPSTOX_TOKEN_DOC_ID = os.getenv(
    "UPSTOX_TOKEN_DOC_ID",
    "upstox_access_token",
)

UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES = int(
    os.getenv(
        "UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES",
        "30",
    )
)

TELEGRAM_TOKEN_BOT_ENABLED = (
    os.getenv(
        "TELEGRAM_TOKEN_BOT_ENABLED",
        "true",
    ).lower()
    == "true"
)

TELEGRAM_TOKEN_BOT_POLL_SECONDS = int(
    os.getenv(
        "TELEGRAM_TOKEN_BOT_POLL_SECONDS",
        "3",
    )
)

TELEGRAM_TOKEN_BOT_LONG_POLL_TIMEOUT = int(
    os.getenv(
        "TELEGRAM_TOKEN_BOT_LONG_POLL_TIMEOUT",
        "20",
    )
)

TELEGRAM_TOKEN_BOT_RESTRICT_TO_CHAT = (
    os.getenv(
        "TELEGRAM_TOKEN_BOT_RESTRICT_TO_CHAT",
        "true",
    ).lower()
    == "true"
)


# ============================================================
# Algo Application API Configuration
# ============================================================

ALGO_APP_ENABLED = (
    os.getenv(
        "ALGO_APP_ENABLED",
        "false",
    ).lower()
    == "true"
)

ALGO_APP_URL = os.getenv(
    "ALGO_APP_URL",
    "",
)

ALGO_APP_AUTH_TYPE = os.getenv(
    "ALGO_APP_AUTH_TYPE",
    "none",
).lower()

ALGO_APP_AUTH_TOKEN = os.getenv(
    "ALGO_APP_AUTH_TOKEN",
    "",
)

ALGO_APP_API_KEY = os.getenv(
    "ALGO_APP_API_KEY",
    "",
)

ALGO_APP_API_KEY_HEADER = os.getenv(
    "ALGO_APP_API_KEY_HEADER",
    "X-API-Key",
)

ALGO_APP_TIMEOUT_SECONDS = float(
    os.getenv(
        "ALGO_APP_TIMEOUT_SECONDS",
        "10",
    )
)

ALGO_APP_VERIFY_SSL = (
    os.getenv(
        "ALGO_APP_VERIFY_SSL",
        "true",
    ).lower()
    == "true"
)

ALGO_APP_MAX_RETRIES = int(
    os.getenv(
        "ALGO_APP_MAX_RETRIES",
        "3",
    )
)

ALGO_APP_RETRY_DELAY_SECONDS = float(
    os.getenv(
        "ALGO_APP_RETRY_DELAY_SECONDS",
        "2",
    )
)

ALGO_APP_SEND_IN_BACKGROUND = (
    os.getenv(
        "ALGO_APP_SEND_IN_BACKGROUND",
        "true",
    ).lower()
    == "true"
)

ALGO_APP_INCLUDE_EVENT_ID = (
    os.getenv(
        "ALGO_APP_INCLUDE_EVENT_ID",
        "true",
    ).lower()
    == "true"
)

ALGO_APP_PAYLOAD_SCHEMA_VERSION = os.getenv(
    "ALGO_APP_PAYLOAD_SCHEMA_VERSION",
    "1.0",
)

ALGO_APP_SOURCE_NAME = os.getenv(
    "ALGO_APP_SOURCE_NAME",
    "option_feed_engine",
)

ALGO_APP_MAX_RESPONSE_BODY_LENGTH = int(
    os.getenv(
        "ALGO_APP_MAX_RESPONSE_BODY_LENGTH",
        "2000",
    )
)


# ============================================================
# Market Timezone Configuration
# ============================================================

MARKET_TIMEZONE = os.getenv(
    "MARKET_TIMEZONE",
    "Asia/Kolkata",
)

MARKET_TIME_FORMAT = os.getenv(
    "MARKET_TIME_FORMAT",
    "%Y-%m-%d %H:%M:%S %Z",
)

MARKET_OPEN_HOUR = int(
    os.getenv(
        "MARKET_OPEN_HOUR",
        "9",
    )
)

MARKET_OPEN_MINUTE = int(
    os.getenv(
        "MARKET_OPEN_MINUTE",
        "15",
    )
)


# ============================================================
# Instrument and Subscription Configuration
# ============================================================

MAIN_NIFTY_SECURITY = os.getenv(
    "MAIN_NIFTY_SECURITY",
    "NSE_INDEX|Nifty 50",
)

STRIKE_FROM = float(
    os.getenv(
        "STRIKE_FROM",
        "23000.0",
    )
)

STRIKE_TO = float(
    os.getenv(
        "STRIKE_TO",
        "25000.0",
    )
)

WEBSOCKET_FEED_MODE = os.getenv(
    "WEBSOCKET_FEED_MODE",
    "full",
)


# ============================================================
# Historical Candle Configuration
# ============================================================

HISTORICAL_CANDLE_ENABLED = (
    os.getenv(
        "HISTORICAL_CANDLE_ENABLED",
        "true",
    ).lower()
    == "true"
)

HISTORICAL_CANDLE_DAYS = int(
    os.getenv(
        "HISTORICAL_CANDLE_DAYS",
        "10",
    )
)

HISTORICAL_CANDLE_INTERVAL = os.getenv(
    "HISTORICAL_CANDLE_INTERVAL",
    "1minute",
)

HISTORICAL_CANDLE_API_VERSION = os.getenv(
    "HISTORICAL_CANDLE_API_VERSION",
    "2.0",
)

HISTORICAL_CANDLE_MAX_DAYS_PER_REQUEST = int(
    os.getenv(
        "HISTORICAL_CANDLE_MAX_DAYS_PER_REQUEST",
        "7",
    )
)

HISTORICAL_CANDLE_OUTPUT_DIR = os.getenv(
    "HISTORICAL_CANDLE_OUTPUT_DIR",
    "data/historical_candles",
)

HISTORICAL_CANDLE_MAX_WORKERS = int(
    os.getenv(
        "HISTORICAL_CANDLE_MAX_WORKERS",
        "8",
    )
)

HISTORICAL_CANDLE_REQUEST_SLEEP_SECONDS = float(
    os.getenv(
        "HISTORICAL_CANDLE_REQUEST_SLEEP_SECONDS",
        "0.15",
    )
)


# ============================================================
# Opening Range Configuration
# ============================================================

OPENING_RANGE_ENABLED = (
    os.getenv(
        "OPENING_RANGE_ENABLED",
        "true",
    ).lower()
    == "true"
)

OPENING_RANGE_INTERVAL = os.getenv(
    "OPENING_RANGE_INTERVAL",
    "1minute",
)

OPENING_RANGE_CANDLE_COUNT = int(
    os.getenv(
        "OPENING_RANGE_CANDLE_COUNT",
        "1",
    )
)

OPENING_RANGE_MARKET_OPEN_HOUR = int(
    os.getenv(
        "OPENING_RANGE_MARKET_OPEN_HOUR",
        "9",
    )
)

OPENING_RANGE_MARKET_OPEN_MINUTE = int(
    os.getenv(
        "OPENING_RANGE_MARKET_OPEN_MINUTE",
        "15",
    )
)

OPENING_RANGE_FETCH_HOUR = int(
    os.getenv(
        "OPENING_RANGE_FETCH_HOUR",
        "9",
    )
)

OPENING_RANGE_FETCH_MINUTE = int(
    os.getenv(
        "OPENING_RANGE_FETCH_MINUTE",
        "18",
    )
)

OPENING_RANGE_INTRADAY_UNIT = os.getenv(
    "OPENING_RANGE_INTRADAY_UNIT",
    "minutes",
)

OPENING_RANGE_INTRADAY_INTERVAL = os.getenv(
    "OPENING_RANGE_INTRADAY_INTERVAL",
    "1",
)

OPENING_RANGE_MAX_WORKERS = int(
    os.getenv(
        "OPENING_RANGE_MAX_WORKERS",
        "8",
    )
)

OPENING_RANGE_REQUEST_SLEEP_SECONDS = float(
    os.getenv(
        "OPENING_RANGE_REQUEST_SLEEP_SECONDS",
        "0.15",
    )
)

OPENING_RANGE_SAVE_FILE = (
    os.getenv(
        "OPENING_RANGE_SAVE_FILE",
        "true",
    ).lower()
    == "true"
)

OPENING_RANGE_OUTPUT_FILE = os.getenv(
    "OPENING_RANGE_OUTPUT_FILE",
    "data/opening_range_results.json",
)

OPENING_RANGE_MAX_EVENTS_IN_MEMORY = int(
    os.getenv(
        "OPENING_RANGE_MAX_EVENTS_IN_MEMORY",
        "5000",
    )
)


# ============================================================
# Opening Range Backfill Configuration
# ============================================================

OPENING_RANGE_BACKFILL_TOUCH_SCAN_ENABLED = (
    os.getenv(
        "OPENING_RANGE_BACKFILL_TOUCH_SCAN_ENABLED",
        "true",
    ).lower()
    == "true"
)

OPENING_RANGE_BACKFILL_TOUCH_SCAN_SOURCE = os.getenv(
    "OPENING_RANGE_BACKFILL_TOUCH_SCAN_SOURCE",
    "intraday_api",
)


# ============================================================
# Opening Range Touch Configuration
# ============================================================

OPENING_RANGE_TOUCH_ALERT_ENABLED = (
    os.getenv(
        "OPENING_RANGE_TOUCH_ALERT_ENABLED",
        "true",
    ).lower()
    == "true"
)

OPENING_RANGE_TOUCH_ALERT_MAX_INSTRUMENTS = int(
    os.getenv(
        "OPENING_RANGE_TOUCH_ALERT_MAX_INSTRUMENTS",
        "5",
    )
)

OPENING_RANGE_TOUCH_ALERT_BATCH_SECONDS = int(
    os.getenv(
        "OPENING_RANGE_TOUCH_ALERT_BATCH_SECONDS",
        "10",
    )
)

OPENING_RANGE_TOUCH_ALERT_ONCE_PER_LEVEL = (
    os.getenv(
        "OPENING_RANGE_TOUCH_ALERT_ONCE_PER_LEVEL",
        "true",
    ).lower()
    == "true"
)

OPENING_RANGE_TOUCH_ALERT_OPTIONS_ONLY = (
    os.getenv(
        "OPENING_RANGE_TOUCH_ALERT_OPTIONS_ONLY",
        "true",
    ).lower()
    == "true"
)

OPENING_RANGE_TOUCH_ALERT_SORT_BY_NEAREST_INDEX = (
    os.getenv(
        "OPENING_RANGE_TOUCH_ALERT_SORT_BY_NEAREST_INDEX",
        "true",
    ).lower()
    == "true"
)

OPENING_RANGE_TOUCH_ALERT_MAIN_INDEX_KEY = os.getenv(
    "OPENING_RANGE_TOUCH_ALERT_MAIN_INDEX_KEY",
    MAIN_NIFTY_SECURITY,
)

OPENING_RANGE_BACKFILL_TOUCH_ALERT_ENABLED = (
    os.getenv(
        "OPENING_RANGE_BACKFILL_TOUCH_ALERT_ENABLED",
        "true",
    ).lower()
    == "true"
)

OPENING_RANGE_LIVE_TOUCH_ALERT_ENABLED = (
    os.getenv(
        "OPENING_RANGE_LIVE_TOUCH_ALERT_ENABLED",
        "true",
    ).lower()
    == "true"
)

OPENING_RANGE_TOUCH_CHECK_MODE = os.getenv(
    "OPENING_RANGE_TOUCH_CHECK_MODE",
    "high_low",
)

OPENING_RANGE_STORE_TOUCH_STATUS = (
    os.getenv(
        "OPENING_RANGE_STORE_TOUCH_STATUS",
        "true",
    ).lower()
    == "true"
)

OPENING_RANGE_TOUCH_EVENTS_OUTPUT_FILE = os.getenv(
    "OPENING_RANGE_TOUCH_EVENTS_OUTPUT_FILE",
    "data/opening_range_touch_events.json",
)

OPENING_RANGE_TOUCH_EVENTS_SAVE_TEST_FILE = (
    os.getenv(
        "OPENING_RANGE_TOUCH_EVENTS_SAVE_TEST_FILE",
        "true",
    ).lower()
    == "true"
)


# ============================================================
# Opening Range Isolation Configuration
# ============================================================

OPENING_RANGE_ISOLATED_INSTRUMENT_ENABLED = (
    os.getenv(
        "OPENING_RANGE_ISOLATED_INSTRUMENT_ENABLED",
        "true",
    ).lower()
    == "true"
)

OPENING_RANGE_ISOLATION_AVERAGE_WINDOW_POINTS = float(
    os.getenv(
        "OPENING_RANGE_ISOLATION_AVERAGE_WINDOW_POINTS",
        "500.0",
    )
)

# Re-selection rule:
# The currently isolated instrument remains locked while its
# strike is within +/- this distance from the Opening Range average.
#
# Example:
#   OR average = 24055
#   re-selection distance = 150
#   valid isolated strike range = 23905 to 24205
#
# If the current isolated instrument moves outside this range,
# a new eligible priority-level touch can replace it.

OPENING_RANGE_ISOLATION_RESELECT_DISTANCE_POINTS = float(
    os.getenv(
        "OPENING_RANGE_ISOLATION_RESELECT_DISTANCE_POINTS",
        "200.0",
    )
)

OPENING_RANGE_ISOLATION_TOUCH_LEVELS = [
    value.strip().upper()
    for value in os.getenv(
        "OPENING_RANGE_ISOLATION_TOUCH_LEVELS",
        "S3,R3",
    ).split(",")
    if value.strip()
]

OPENING_RANGE_ISOLATION_PRIORITY_LEVELS = [
    value.strip().upper()
    for value in os.getenv(
        "OPENING_RANGE_ISOLATION_PRIORITY_LEVELS",
        "S3,R3",
    ).split(",")
    if value.strip()
]

OPENING_RANGE_ISOLATION_LOCK_FOR_DAY = (
    os.getenv(
        "OPENING_RANGE_ISOLATION_LOCK_FOR_DAY",
        "true",
    ).lower()
    == "true"
)



OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED = (
    os.getenv(
        "OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED",
        "true",
    ).lower()
    == "true"
)

OPENING_RANGE_ISOLATION_ALLOW_BACKFILL_TOUCH = (
    os.getenv(
        "OPENING_RANGE_ISOLATION_ALLOW_BACKFILL_TOUCH",
        "true",
    ).lower()
    == "true"
)

OPENING_RANGE_ISOLATION_ALLOW_LIVE_TOUCH = (
    os.getenv(
        "OPENING_RANGE_ISOLATION_ALLOW_LIVE_TOUCH",
        "true",
    ).lower()
    == "true"
)

OPENING_RANGE_ISOLATION_OPTIONS_ONLY = (
    os.getenv(
        "OPENING_RANGE_ISOLATION_OPTIONS_ONLY",
        "true",
    ).lower()
    == "true"
)

OPENING_RANGE_ISOLATED_INSTRUMENT_OUTPUT_FILE = os.getenv(
    "OPENING_RANGE_ISOLATED_INSTRUMENT_OUTPUT_FILE",
    "data/isolated_opening_range_instrument.json",
)

OPENING_RANGE_ISOLATION_RESET_DAILY = (
    os.getenv(
        "OPENING_RANGE_ISOLATION_RESET_DAILY",
        "true",
    ).lower()
    == "true"
)


# ============================================================
# Selected Instrument Compatibility Configuration
# ============================================================

OPENING_RANGE_FIRST_TOUCH_SELECTION_ENABLED = (
    os.getenv(
        "OPENING_RANGE_FIRST_TOUCH_SELECTION_ENABLED",
        "true",
    ).lower()
    == "true"
)

OPENING_RANGE_FIRST_TOUCH_SELECTION_SOURCE = os.getenv(
    "OPENING_RANGE_FIRST_TOUCH_SELECTION_SOURCE",
    "average_window_level_priority",
)

OPENING_RANGE_SELECTED_OR_TOUCH_NOTIFY_ENABLED = (
    os.getenv(
        "OPENING_RANGE_SELECTED_OR_TOUCH_NOTIFY_ENABLED",
        "true",
    ).lower()
    == "true"
)

OPENING_RANGE_SELECTED_OR_EMA_ALERT_ENABLED = (
    os.getenv(
        "OPENING_RANGE_SELECTED_OR_EMA_ALERT_ENABLED",
        "true",
    ).lower()
    == "true"
)

OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED = (
    os.getenv(
        "OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED",
        "false",
    ).lower()
    == "true"
)

OPENING_RANGE_SELECTED_OR_EMA_ALERT_ONCE_PER_CROSS = (
    os.getenv(
        "OPENING_RANGE_SELECTED_OR_EMA_ALERT_ONCE_PER_CROSS",
        "false",
    ).lower()
    == "true"
)


# ============================================================
# EMA Opening Range Enrichment Configuration
# ============================================================

EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS = (
    os.getenv(
        "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
        "true",
    ).lower()
    == "true"
)

EMA_CROSS_BROADCAST_WITHOUT_OPENING_RANGE = (
    os.getenv(
        "EMA_CROSS_BROADCAST_WITHOUT_OPENING_RANGE",
        "true",
    ).lower()
    == "true"
)


# ============================================================
# Isolated Instrument EMA Alert Configuration
# ============================================================

EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED = (
    os.getenv(
        "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED",
        "true",
    ).lower()
    == "true"
)

EMA_ISOLATED_ALERT_EVERY_CROSS = (
    os.getenv(
        "EMA_ISOLATED_ALERT_EVERY_CROSS",
        "true",
    ).lower()
    == "true"
)

EMA_ISOLATED_ALERT_INCLUDE_LEVEL_NAME = (
    os.getenv(
        "EMA_ISOLATED_ALERT_INCLUDE_LEVEL_NAME",
        "true",
    ).lower()
    == "true"
)

EMA_ISOLATED_ALERT_INCLUDE_NIFTY_LTP = (
    os.getenv(
        "EMA_ISOLATED_ALERT_INCLUDE_NIFTY_LTP",
        "true",
    ).lower()
    == "true"
)

EMA_ISOLATED_ALERT_INCLUDE_EMA_DETAILS = (
    os.getenv(
        "EMA_ISOLATED_ALERT_INCLUDE_EMA_DETAILS",
        "true",
    ).lower()
    == "true"
)

EMA_ISOLATED_ALERT_INCLUDE_NEAREST_ORDER_INSTRUMENTS = (
    os.getenv(
        "EMA_ISOLATED_ALERT_INCLUDE_NEAREST_ORDER_INSTRUMENTS",
        "true",
    ).lower()
    == "true"
)

EMA_ISOLATED_ALERT_INCLUDE_BUDGET_INSTRUMENTS = (
    os.getenv(
        "EMA_ISOLATED_ALERT_INCLUDE_BUDGET_INSTRUMENTS",
        "true",
    ).lower()
    == "true"
)

EMA_ISOLATED_ALERT_INCLUDE_CANDLE_CLOSE = (
    os.getenv(
        "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_CLOSE",
        "true",
    ).lower()
    == "true"
)

EMA_ISOLATED_ALERT_INCLUDE_CANDLE_LOW = (
    os.getenv(
        "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_LOW",
        "true",
    ).lower()
    == "true"
)

EMA_ISOLATED_ALERT_INCLUDE_CLOSE_LOW_DIFFERENCE = (
    os.getenv(
        "EMA_ISOLATED_ALERT_INCLUDE_CLOSE_LOW_DIFFERENCE",
        "true",
    ).lower()
    == "true"
)

EMA_ISOLATED_ALERT_INCLUDE_CANDLE_TIME = (
    os.getenv(
        "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_TIME",
        "true",
    ).lower()
    == "true"
)

EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES = int(
    os.getenv(
        "EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES",
        "2",
    )
)


# ============================================================
# EMA Order Instrument Configuration
# ============================================================

EMA_ALERT_BULLISH_OPTION_TYPE = os.getenv(
    "EMA_ALERT_BULLISH_OPTION_TYPE",
    "CE",
).upper()

EMA_ALERT_BEARISH_OPTION_TYPE = os.getenv(
    "EMA_ALERT_BEARISH_OPTION_TYPE",
    "PE",
).upper()

EMA_ALERT_STRIKE_STEP = int(
    os.getenv(
        "EMA_ALERT_STRIKE_STEP",
        "50",
    )
)

EMA_ALERT_NEAREST_STRIKE_COUNT = int(
    os.getenv(
        "EMA_ALERT_NEAREST_STRIKE_COUNT",
        "3",
    )
)

EMA_ALERT_NEAREST_STRIKE_OFFSETS = [
    int(value.strip())
    for value in os.getenv(
        "EMA_ALERT_NEAREST_STRIKE_OFFSETS",
        "-50,0,50",
    ).split(",")
    if value.strip()
]

EMA_ALERT_ORDER_STRIKES_CLAMP_TO_FILTER_RANGE = (
    os.getenv(
        "EMA_ALERT_ORDER_STRIKES_CLAMP_TO_FILTER_RANGE",
        "true",
    ).lower()
    == "true"
)

EMA_ALERT_INCLUDE_ORDER_INSTRUMENT_LTP = (
    os.getenv(
        "EMA_ALERT_INCLUDE_ORDER_INSTRUMENT_LTP",
        "true",
    ).lower()
    == "true"
)

EMA_ALERT_SHOW_ORDER_INSTRUMENT_WHEN_LTP_MISSING = (
    os.getenv(
        "EMA_ALERT_SHOW_ORDER_INSTRUMENT_WHEN_LTP_MISSING",
        "true",
    ).lower()
    == "true"
)

EMA_ALERT_MAX_ORDER_INSTRUMENTS = int(
    os.getenv(
        "EMA_ALERT_MAX_ORDER_INSTRUMENTS",
        "3",
    )
)


# ============================================================
# EMA Budget Range Configuration
# ============================================================

EMA_ALERT_BUDGET_RANGE_ENABLED = (
    os.getenv(
        "EMA_ALERT_BUDGET_RANGE_ENABLED",
        "true",
    ).lower()
    == "true"
)

EMA_ALERT_BUDGET_MIN_PRICE = float(
    os.getenv(
        "EMA_ALERT_BUDGET_MIN_PRICE",
        "20.0",
    )
)

EMA_ALERT_BUDGET_MAX_PRICE = float(
    os.getenv(
        "EMA_ALERT_BUDGET_MAX_PRICE",
        "30.0",
    )
)

EMA_ALERT_BUDGET_MAX_INSTRUMENTS = int(
    os.getenv(
        "EMA_ALERT_BUDGET_MAX_INSTRUMENTS",
        "2",
    )
)

EMA_ALERT_BUDGET_USE_SUGGESTED_ORDER_SIDE = (
    os.getenv(
        "EMA_ALERT_BUDGET_USE_SUGGESTED_ORDER_SIDE",
        "true",
    ).lower()
    == "true"
)

EMA_ALERT_BUDGET_SUBSCRIBED_ONLY = (
    os.getenv(
        "EMA_ALERT_BUDGET_SUBSCRIBED_ONLY",
        "true",
    ).lower()
    == "true"
)

EMA_ALERT_BUDGET_REQUIRE_LIVE_LTP = (
    os.getenv(
        "EMA_ALERT_BUDGET_REQUIRE_LIVE_LTP",
        "true",
    ).lower()
    == "true"
)

EMA_ALERT_BUDGET_SORT_MODE = os.getenv(
    "EMA_ALERT_BUDGET_SORT_MODE",
    "nearest_to_budget_midpoint",
)

EMA_ALERT_BUDGET_RANGE_INCLUSIVE = (
    os.getenv(
        "EMA_ALERT_BUDGET_RANGE_INCLUSIVE",
        "true",
    ).lower()
    == "true"
)


# ============================================================
# EMA Algo Payload Configuration
# ============================================================

EMA_ALGO_PAYLOAD_INCLUDE_OPENING_RANGE = (
    os.getenv(
        "EMA_ALGO_PAYLOAD_INCLUDE_OPENING_RANGE",
        "true",
    ).lower()
    == "true"
)

EMA_ALGO_PAYLOAD_INCLUDE_EMA_VALUES = (
    os.getenv(
        "EMA_ALGO_PAYLOAD_INCLUDE_EMA_VALUES",
        "true",
    ).lower()
    == "true"
)

EMA_ALGO_PAYLOAD_INCLUDE_CANDLE = (
    os.getenv(
        "EMA_ALGO_PAYLOAD_INCLUDE_CANDLE",
        "true",
    ).lower()
    == "true"
)

EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENTS = (
    os.getenv(
        "EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENTS",
        "true",
    ).lower()
    == "true"
)

EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENTS = (
    os.getenv(
        "EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENTS",
        "true",
    ).lower()
    == "true"
)

EMA_ALGO_PAYLOAD_INCLUDE_DELIVERY_METADATA = (
    os.getenv(
        "EMA_ALGO_PAYLOAD_INCLUDE_DELIVERY_METADATA",
        "false",
    ).lower()
    == "true"
)


# ============================================================
# Test Configuration
# ============================================================

TEST_FLAG = (
    os.getenv(
        "TEST_FLAG",
        "false",
    ).lower()
    == "true"
)


# ============================================================
# Historical EMA Configuration
# ============================================================

EMA_FAST_PERIOD = int(
    os.getenv(
        "EMA_FAST_PERIOD",
        "9",
    )
)

EMA_SLOW_PERIOD = int(
    os.getenv(
        "EMA_SLOW_PERIOD",
        "21",
    )
)

EMA_CROSS_OUTPUT_FILE = os.getenv(
    "EMA_CROSS_OUTPUT_FILE",
    "data/ema_cross_results.json",
)


# ============================================================
# Live EMA Configuration
# ============================================================

LIVE_EMA_ENABLED = (
    os.getenv(
        "LIVE_EMA_ENABLED",
        "true",
    ).lower()
    == "true"
)

LIVE_EMA_CALCULATION_MODE = (
    os.getenv(
        "LIVE_EMA_CALCULATION_MODE",
        "false",
    ).lower()
    == "true"
)

LIVE_EMA_INTERVAL_MINUTES = int(
    os.getenv(
        "LIVE_EMA_INTERVAL_MINUTES",
        "1",
    )
)

LIVE_EMA_FAST_PERIOD = int(
    os.getenv(
        "LIVE_EMA_FAST_PERIOD",
        "9",
    )
)

LIVE_EMA_SLOW_PERIOD = int(
    os.getenv(
        "LIVE_EMA_SLOW_PERIOD",
        "21",
    )
)

LIVE_EMA_TICK_ALERT_ONCE_PER_DIRECTION = (
    os.getenv(
        "LIVE_EMA_TICK_ALERT_ONCE_PER_DIRECTION",
        "true",
    ).lower()
    == "true"
)

LIVE_EMA_TICK_MIN_PRICE_CHANGE = float(
    os.getenv(
        "LIVE_EMA_TICK_MIN_PRICE_CHANGE",
        "0.0",
    )
)

LIVE_EMA_SAVE_TEST_FILE = (
    os.getenv(
        "LIVE_EMA_SAVE_TEST_FILE",
        "true",
    ).lower()
    == "true"
)

LIVE_EMA_OUTPUT_FILE = os.getenv(
    "LIVE_EMA_OUTPUT_FILE",
    "data/live_ema_cross_results.json",
)

LIVE_EMA_MAX_EVENTS_IN_MEMORY = int(
    os.getenv(
        "LIVE_EMA_MAX_EVENTS_IN_MEMORY",
        "5000",
    )
)


ALGO_APP_BACKGROUND_QUEUE_COUNTS_AS_ACCEPTED = (
    os.getenv(
        "ALGO_APP_BACKGROUND_QUEUE_COUNTS_AS_ACCEPTED",
        "true",
    ).lower()
    == "true"
)

ALGO_APP_BACKGROUND_MAX_WORKERS = int(
    os.getenv(
        "ALGO_APP_BACKGROUND_MAX_WORKERS",
        "2",
    )
)

if ALGO_APP_BACKGROUND_MAX_WORKERS < 1:
    ALGO_APP_BACKGROUND_MAX_WORKERS = 1


EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENT_CANDLES = (
    os.getenv(
        "EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENT_CANDLES",
        "true",
    ).lower()
    == "true"
)

EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENT_CANDLES = (
    os.getenv(
        "EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENT_CANDLES",
        "true",
    ).lower()
    == "true"
)


EMA_ALGO_PAYLOAD_INCLUDE_RAW_EMA_EVENT = (
    os.getenv(
        "EMA_ALGO_PAYLOAD_INCLUDE_RAW_EMA_EVENT",
        "true",
    ).lower()
    == "true"
)
