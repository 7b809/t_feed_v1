import os
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# MongoDB Configuration
# ============================================================

MONGO_URI = os.getenv("MONGO_URL")
MONGO_DB = os.getenv("MONGO_DB")
TOKENS_COLLECTION = os.getenv("TOKENS_COLLECTION")

PLACE_ORDER_ENABLED = True
SL_THRESHOLD=5.0



# Token refresh interval in minutes.
# Existing token refresh workflow.
REFRESH_INTERVAL_MINUTES = 60


# ============================================================
# Telegram Notification Configuration
# ============================================================
# Telegram is used for normal main flow notifications:
# startup, token refresh, scheduler, instrument load, refresh,
# subscription, Opening Range job, shutdown, and errors.
#
# Isolated instrument requirement:
# - Send Telegram notification when one Opening Range instrument is isolated.
# - Send Telegram EMA crossover alerts only for the isolated instrument.
# - Other instruments can continue EMA calculation and WebSocket broadcast,
#   but should not send Telegram EMA alerts.
# - EMA alert order side is dynamic:
#     bullish_cross -> same side as isolated instrument
#     bearish_cross -> opposite side of isolated instrument
#
# New isolated instrument role message requirement:
# - If isolated instrument touches S2 or S3, treat isolated instrument as SUPPORT.
# - If isolated instrument touches R2 or R3, treat isolated instrument as RESISTANCE.
# - Opposite CE/PE instrument at same strike should show opposite role.
# - If isolated instrument is SUPPORT, opposite instrument is RESISTANCE-LIKE.
# - If isolated instrument is RESISTANCE, opposite instrument is SUPPORT-LIKE.
#
# Token bot requirement:
# - Telegram bot can receive /save-token, /token-status, /profile, /funds.
# - /save-token expects the next Telegram message to contain the raw Upstox token.
# - Raw token message should be deleted after processing.
# - Token commands should be restricted to TELEGRAM_CHAT_ID.

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
    os.getenv("UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES", "30")
)

TELEGRAM_TOKEN_BOT_ENABLED = (
    os.getenv("TELEGRAM_TOKEN_BOT_ENABLED", "true").lower() == "true"
)

TELEGRAM_TOKEN_BOT_POLL_SECONDS = int(
    os.getenv("TELEGRAM_TOKEN_BOT_POLL_SECONDS", "3")
)

TELEGRAM_TOKEN_BOT_LONG_POLL_TIMEOUT = int(
    os.getenv("TELEGRAM_TOKEN_BOT_LONG_POLL_TIMEOUT", "20")
)

TELEGRAM_TOKEN_BOT_RESTRICT_TO_CHAT = (
    os.getenv("TELEGRAM_TOKEN_BOT_RESTRICT_TO_CHAT", "true").lower() == "true"
)


# ============================================================
# Market Timezone Configuration
# ============================================================

MARKET_TIMEZONE = os.getenv("MARKET_TIMEZONE", "Asia/Kolkata")
MARKET_TIME_FORMAT = "%Y-%m-%d %H:%M:%S %Z"

MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15


# ============================================================
# Instrument and Subscription Configuration
# ============================================================

MAIN_NIFTY_SECURITY = "NSE_INDEX|Nifty 50"

STRIKE_FROM = 23000.0
STRIKE_TO = 25000.0

WEBSOCKET_FEED_MODE = "full"


# ============================================================
# Historical Candle Configuration
# ============================================================

HISTORICAL_CANDLE_ENABLED = True
HISTORICAL_CANDLE_DAYS = 10
HISTORICAL_CANDLE_INTERVAL = "1minute"
HISTORICAL_CANDLE_API_VERSION = "2.0"
HISTORICAL_CANDLE_MAX_DAYS_PER_REQUEST = 7
HISTORICAL_CANDLE_OUTPUT_DIR = "data/historical_candles"
HISTORICAL_CANDLE_MAX_WORKERS = 8
HISTORICAL_CANDLE_REQUEST_SLEEP_SECONDS = 0.15


# ============================================================
# Opening Range Configuration
# ============================================================
# Opening Range is calculated for every subscribed instrument.
# Opening Range levels are cached per instrument.
# EMA crossover WebSocket events can include the matching instrument's
# Opening Range range and levels when available.

OPENING_RANGE_ENABLED = True

OPENING_RANGE_INTERVAL = "1minute"

OPENING_RANGE_CANDLE_COUNT = 1

OPENING_RANGE_MARKET_OPEN_HOUR = 9
OPENING_RANGE_MARKET_OPEN_MINUTE = 15

OPENING_RANGE_FETCH_HOUR = 9
OPENING_RANGE_FETCH_MINUTE = 18

OPENING_RANGE_INTRADAY_UNIT = "minutes"
OPENING_RANGE_INTRADAY_INTERVAL = "1"

OPENING_RANGE_MAX_WORKERS = 8
OPENING_RANGE_REQUEST_SLEEP_SECONDS = 0.15

OPENING_RANGE_SAVE_FILE = True
OPENING_RANGE_OUTPUT_FILE = "data/opening_range_results.json"

OPENING_RANGE_MAX_EVENTS_IN_MEMORY = 5000


# ============================================================
# Opening Range Backfill Touch Scan Configuration
# ============================================================

OPENING_RANGE_BACKFILL_TOUCH_SCAN_ENABLED = True
OPENING_RANGE_BACKFILL_TOUCH_SCAN_SOURCE = "intraday_api"


# ============================================================
# Opening Range Live Touch Event Configuration
# ============================================================
# Live touch monitoring continues after Opening Range levels are ready.
# Touch events are used for diagnostics, WebSocket visibility,
# and isolated instrument selection.

OPENING_RANGE_TOUCH_ALERT_ENABLED = True

OPENING_RANGE_TOUCH_ALERT_MAX_INSTRUMENTS = 5
OPENING_RANGE_TOUCH_ALERT_BATCH_SECONDS = 10

OPENING_RANGE_TOUCH_ALERT_ONCE_PER_LEVEL = True

OPENING_RANGE_TOUCH_ALERT_OPTIONS_ONLY = True

OPENING_RANGE_TOUCH_ALERT_SORT_BY_NEAREST_INDEX = True
OPENING_RANGE_TOUCH_ALERT_MAIN_INDEX_KEY = MAIN_NIFTY_SECURITY

OPENING_RANGE_BACKFILL_TOUCH_ALERT_ENABLED = True
OPENING_RANGE_LIVE_TOUCH_ALERT_ENABLED = True

# Touch check mode.
# "high_low" means:
#   Resistance touched if high >= level
#   Support touched if low <= level
# "ltp" means:
#   Resistance touched if ltp >= level
#   Support touched if ltp <= level
OPENING_RANGE_TOUCH_CHECK_MODE = "high_low"

OPENING_RANGE_STORE_TOUCH_STATUS = True

OPENING_RANGE_TOUCH_EVENTS_OUTPUT_FILE = "data/opening_range_touch_events.json"
OPENING_RANGE_TOUCH_EVENTS_SAVE_TEST_FILE = True


# ============================================================
# Opening Range Isolated Instrument Configuration
# ============================================================
# Requirement:
# - After Opening Range is ready, take Opening Range average.
# - Build average +/- 500 points strike window.
# - Clamp the window inside STRIKE_FROM and STRIKE_TO.
# - Monitor only eligible option instruments inside this window.
# - If S3/R3 is touched, isolate nearest S3/R3 instrument to average.
# - If no S3/R3 touch exists, use S2/R2 and isolate nearest instrument to average.
# - Priority order is S3 first, then R3, then S2, then R2.
# - Once isolated, lock that instrument for the trading day.
# - Live EMA still runs for all instruments.
# - Telegram EMA alerts are sent only for the isolated instrument.
# - EMA alert order side is dynamic:
#     bullish_cross -> same side as isolated instrument
#     bearish_cross -> opposite side of isolated instrument

OPENING_RANGE_ISOLATED_INSTRUMENT_ENABLED = True

OPENING_RANGE_ISOLATION_AVERAGE_WINDOW_POINTS = 500.0

# Only these Opening Range levels are eligible for isolation.
OPENING_RANGE_ISOLATION_TOUCH_LEVELS = ["S3", "R3", "S2", "R2"]

# Level priority for choosing isolated instrument.
# New priority:
#   S3 first
#   R3 second
#   S2 third
#   R2 fourth
# If multiple instruments touch at same time, S3 has highest priority.
# If multiple instruments exist in same priority group, nearest strike to OR average wins.
OPENING_RANGE_ISOLATION_PRIORITY_LEVELS = ["S3", "R3", "S2", "R2"]

OPENING_RANGE_ISOLATION_LOCK_FOR_DAY = True

OPENING_RANGE_ISOLATION_ALLOW_PRIORITY_UPGRADE = True

OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED = True

OPENING_RANGE_ISOLATION_ALLOW_BACKFILL_TOUCH = True
OPENING_RANGE_ISOLATION_ALLOW_LIVE_TOUCH = True

OPENING_RANGE_ISOLATION_OPTIONS_ONLY = True

OPENING_RANGE_ISOLATED_INSTRUMENT_OUTPUT_FILE = (
    "data/isolated_opening_range_instrument.json"
)

OPENING_RANGE_ISOLATION_RESET_DAILY = True


# ============================================================
# Opening Range Isolated Instrument Role Configuration
# ============================================================
# Telegram message role logic:
#
# If selected_level is S2 or S3:
#   Isolated Instrument Role = SUPPORT
#   Opposite Instrument Role = RESISTANCE-LIKE
#
# If selected_level is R2 or R3:
#   Isolated Instrument Role = RESISTANCE
#   Opposite Instrument Role = SUPPORT-LIKE

OPENING_RANGE_ISOLATED_SUPPORT_LEVELS = ["S2", "S3"]
OPENING_RANGE_ISOLATED_RESISTANCE_LEVELS = ["R2", "R3"]

OPENING_RANGE_ISOLATED_SUPPORT_ROLE_TEXT = "SUPPORT"
OPENING_RANGE_ISOLATED_RESISTANCE_ROLE_TEXT = "RESISTANCE"

OPENING_RANGE_OPPOSITE_WHEN_ISOLATED_SUPPORT_ROLE_TEXT = "RESISTANCE-LIKE"
OPENING_RANGE_OPPOSITE_WHEN_ISOLATED_RESISTANCE_ROLE_TEXT = "SUPPORT-LIKE"

OPENING_RANGE_INCLUDE_ISOLATED_ROLE_IN_TELEGRAM = True
OPENING_RANGE_INCLUDE_OPPOSITE_INSTRUMENT_IN_TELEGRAM = True


# ============================================================
# Backward-Compatible Selected Instrument Configuration
# ============================================================

OPENING_RANGE_FIRST_TOUCH_SELECTION_ENABLED = True

OPENING_RANGE_FIRST_TOUCH_SELECTION_SOURCE = "average_window_level_priority"

OPENING_RANGE_SELECTED_OR_TOUCH_NOTIFY_ENABLED = True

OPENING_RANGE_SELECTED_OR_EMA_ALERT_ENABLED = True

OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED = False

OPENING_RANGE_SELECTED_OR_EMA_ALERT_ONCE_PER_CROSS = False


# ============================================================
# EMA + Opening Range WebSocket Enrichment Configuration
# ============================================================

EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS = True

EMA_CROSS_BROADCAST_WITHOUT_OPENING_RANGE = True


# ============================================================
# Isolated Instrument EMA Telegram Alert Configuration
# ============================================================
# Live EMA calculation continues for all subscribed instruments.
# Telegram EMA alert is sent only when:
#   1. An isolated instrument is selected for the day.
#   2. EMA crossover belongs to that isolated instrument.
#   3. The isolated instrument has touched/crossed eligible OR level.
#
# Dynamic order side rule:
#   bullish_cross:
#       Select current NIFTY spot based instruments using the same
#       option side as the isolated instrument.
#
#   bearish_cross:
#       Select current NIFTY spot based instruments using the opposite
#       option side of the isolated instrument.
#
# Examples:
#   Isolated CE + bullish_cross -> suggest CE instruments near NIFTY spot.
#   Isolated CE + bearish_cross -> suggest PE instruments near NIFTY spot.
#   Isolated PE + bullish_cross -> suggest PE instruments near NIFTY spot.
#   Isolated PE + bearish_cross -> suggest CE instruments near NIFTY spot.

EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED = True

EMA_ISOLATED_ALERT_EVERY_CROSS = True

EMA_ISOLATED_ALERT_INCLUDE_LEVEL_NAME = True
EMA_ISOLATED_ALERT_INCLUDE_NIFTY_LTP = True
EMA_ISOLATED_ALERT_INCLUDE_EMA_DETAILS = True
EMA_ISOLATED_ALERT_INCLUDE_NEAREST_ORDER_INSTRUMENTS = True

# Include role details in isolated EMA cross Telegram alerts.
# Example:
#   Isolated Instrument Role: SUPPORT
#   Opposite Instrument Type: PE
#   Opposite Instrument Role: RESISTANCE-LIKE
EMA_ISOLATED_ALERT_INCLUDE_ROLE_DETAILS = True
EMA_ISOLATED_ALERT_INCLUDE_OPPOSITE_INSTRUMENT = True

# Backward-compatible fallback only.
# These are used only when isolated instrument type is unavailable.
EMA_ALERT_BULLISH_OPTION_TYPE = "CE"
EMA_ALERT_BEARISH_OPTION_TYPE = "PE"

EMA_ALERT_STRIKE_STEP = 50

EMA_ALERT_NEAREST_STRIKE_COUNT = 3

EMA_ALERT_NEAREST_STRIKE_OFFSETS = [-50, 0, 50]

EMA_ALERT_ORDER_STRIKES_CLAMP_TO_FILTER_RANGE = True

EMA_ALERT_INCLUDE_ORDER_INSTRUMENT_LTP = True

EMA_ALERT_SHOW_ORDER_INSTRUMENT_WHEN_LTP_MISSING = True

EMA_ALERT_MAX_ORDER_INSTRUMENTS = 3


# ============================================================
# EMA Budget Range Order Instrument Configuration
# ============================================================
# Requirement:
# - In Telegram EMA alert, along with nearest order instruments,
#   show budget range order instruments.
# - Example budget range: 20 rs to 30 rs.
# - Filter instruments by suggested order side.
# - bullish_cross uses same side as isolated instrument.
# - bearish_cross uses opposite side of isolated instrument.
# - If multiple budget instruments are present, sort by nearest strike
#   to current NIFTY LTP and show top configured count.

EMA_ALERT_BUDGET_ORDER_ENABLED = True

EMA_ALERT_BUDGET_MIN_PRICE = float(
    os.getenv("EMA_ALERT_BUDGET_MIN_PRICE", "20.0")
)

EMA_ALERT_BUDGET_MAX_PRICE = float(
    os.getenv("EMA_ALERT_BUDGET_MAX_PRICE", "30.0")
)

EMA_ALERT_BUDGET_MAX_INSTRUMENTS = int(
    os.getenv("EMA_ALERT_BUDGET_MAX_INSTRUMENTS", "3")
)

EMA_ALERT_BUDGET_SORT_BY_NEAREST_NIFTY = (
    os.getenv("EMA_ALERT_BUDGET_SORT_BY_NEAREST_NIFTY", "true").lower() == "true"
)

EMA_ALERT_BUDGET_CLAMP_TO_FILTER_RANGE = True

EMA_ALERT_BUDGET_INCLUDE_ONLY_SUGGESTED_ORDER_SIDE = True

EMA_ALERT_BUDGET_SHOW_WHEN_EMPTY = True

EMA_ALERT_BUDGET_EMPTY_MESSAGE = (
    "No budget range instruments found for configured price range."
)


# ============================================================
# Test / Debug Configuration
# ============================================================

TEST_FLAG = False


# ============================================================
# Historical EMA Crossover Configuration
# ============================================================

EMA_FAST_PERIOD = 9
EMA_SLOW_PERIOD = 21

EMA_CROSS_OUTPUT_FILE = "data/ema_cross_results.json"


# ============================================================
# Live EMA Crossover Configuration
# ============================================================

LIVE_EMA_ENABLED = True

# False = candle based EMA calculation.
# True  = tick based EMA calculation.
LIVE_EMA_CALCULATION_MODE = False

LIVE_EMA_INTERVAL_MINUTES = 1

LIVE_EMA_FAST_PERIOD = 9
LIVE_EMA_SLOW_PERIOD = 21

LIVE_EMA_TICK_ALERT_ONCE_PER_DIRECTION = True

LIVE_EMA_TICK_MIN_PRICE_CHANGE = 0.0

LIVE_EMA_SAVE_TEST_FILE = True

LIVE_EMA_OUTPUT_FILE = "data/live_ema_cross_results.json"

LIVE_EMA_MAX_EVENTS_IN_MEMORY = 5000