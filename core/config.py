import os
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# MongoDB Configuration
# ============================================================

MONGO_URI = os.getenv("MONGO_URL")
MONGO_DB = os.getenv("MONGO_DB")
TOKENS_COLLECTION = os.getenv("TOKENS_COLLECTION")

# Token refresh interval in minutes
REFRESH_INTERVAL_MINUTES = 60


# ============================================================
# Telegram Notification Configuration
# ============================================================

TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "10"))


# ============================================================
# Market Timezone Configuration
# ============================================================

MARKET_TIMEZONE = os.getenv("MARKET_TIMEZONE", "Asia/Kolkata")
MARKET_TIME_FORMAT = "%Y-%m-%d %H:%M:%S %Z"

# Market open boundary.
# Today will be excluded from historical candle fetch before 09:15 AM IST.
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15


# ============================================================
# Instrument and Subscription Configuration
# ============================================================

MAIN_NIFTY_SECURITY = "NSE_INDEX|Nifty 50"

STRIKE_FROM = 23000.0
STRIKE_TO = 25000.0

# Upstox WebSocket feed mode: "ltpc" or "full"
WEBSOCKET_FEED_MODE = "full"


# ============================================================
# Historical Candle Configuration
# ============================================================

HISTORICAL_CANDLE_ENABLED = True

# Number of calendar days to fetch for historical EMA base.
HISTORICAL_CANDLE_DAYS = 10

# Upstox historical candle interval.
# Common value: "1minute"
HISTORICAL_CANDLE_INTERVAL = "1minute"

# Upstox historical API version.
HISTORICAL_CANDLE_API_VERSION = "2.0"

# Upstox historical API max date window per request.
HISTORICAL_CANDLE_MAX_DAYS_PER_REQUEST = 7

# Kept for compatibility, but raw candles should not be saved now.
HISTORICAL_CANDLE_OUTPUT_DIR = "data/historical_candles"

# Parallel fetch worker count.
# Recommended: 5 to 8.
HISTORICAL_CANDLE_MAX_WORKERS = 8

# Small pause inside each instrument's batch calls.
HISTORICAL_CANDLE_REQUEST_SLEEP_SECONDS = 0.15


# ============================================================
# Opening Range Configuration
# ============================================================

# Enables scheduled opening range calculation using Upstox intraday candle API.
OPENING_RANGE_ENABLED = True

# Opening range candle interval.
# Current requirement: use 1-minute candles.
OPENING_RANGE_INTERVAL = "1minute"

# Number of candles to use from market open.
# 1 means only 09:15 to 09:16 candle.
# 3 means 09:15, 09:16, 09:17 candles.
OPENING_RANGE_CANDLE_COUNT = 1

# Market open time for opening range calculation.
OPENING_RANGE_MARKET_OPEN_HOUR = 9
OPENING_RANGE_MARKET_OPEN_MINUTE = 15

# Scheduled fetch time.
# At 09:18 AM, intraday API should have completed 09:15 candle data.
OPENING_RANGE_FETCH_HOUR = 9
OPENING_RANGE_FETCH_MINUTE = 18

# Upstox intraday candle API unit and interval.
# Used by HistoryV3Api().get_intra_day_candle_data()
OPENING_RANGE_INTRADAY_UNIT = "minutes"
OPENING_RANGE_INTRADAY_INTERVAL = "1"

# Parallel worker count for opening range intraday fetch.
# Keep moderate to avoid API overload.
OPENING_RANGE_MAX_WORKERS = 8

# Small pause inside each instrument request.
OPENING_RANGE_REQUEST_SLEEP_SECONDS = 0.15

# Save opening range results to JSON file.
OPENING_RANGE_SAVE_FILE = True

# Opening range result output file.
OPENING_RANGE_OUTPUT_FILE = "data/opening_range_results.json"

# Maximum number of opening range events/results to keep in memory if needed.
OPENING_RANGE_MAX_EVENTS_IN_MEMORY = 5000


# ============================================================
# Opening Range Backfill Touch Scan Configuration
# ============================================================

# Enables checking already completed intraday candles after the opening range window.
# This covers the edge case where R3/S3 was already touched before 09:18 AM.
OPENING_RANGE_BACKFILL_TOUCH_SCAN_ENABLED = True

# Backfill scan source:
# At 09:18, after OR levels are calculated, scan candles from OR completion time.
# Example:
#   OR candle count = 1
#   OR candle = 09:15
#   OR completes at 09:16
#   Scan candles from 09:16 onwards.
OPENING_RANGE_BACKFILL_TOUCH_SCAN_SOURCE = "intraday_api"


# ============================================================
# Opening Range Live Touch Alert Configuration
# ============================================================

# Enables live R3/S3 touch monitoring after opening range levels are available.
OPENING_RANGE_TOUCH_ALERT_ENABLED = True

# Maximum instruments to include in one Telegram alert.
# Old/legacy behavior only. New selected OR flow disables legacy batch Telegram by default.
OPENING_RANGE_TOUCH_ALERT_MAX_INSTRUMENTS = 5

# Batch window in seconds.
# Old/legacy behavior only.
OPENING_RANGE_TOUCH_ALERT_BATCH_SECONDS = 10

# Avoid duplicate touch event tracking for the same instrument and same level.
# Example:
#   NSE_FO|41012 R3 tracked once.
#   NSE_FO|41012 S3 can still be tracked later once.
OPENING_RANGE_TOUCH_ALERT_ONCE_PER_LEVEL = True

# Include only option instruments in OR touch selection and alerts.
# NIFTY index OR levels can still be stored, but not selected as option alert instrument.
OPENING_RANGE_TOUCH_ALERT_OPTIONS_ONLY = True

# Use latest main index LTP to rank touched instruments by nearest strike.
# Old/legacy behavior only.
OPENING_RANGE_TOUCH_ALERT_SORT_BY_NEAREST_INDEX = True

# Main index key used for nearest strike calculation and NIFTY LTP tracking.
OPENING_RANGE_TOUCH_ALERT_MAIN_INDEX_KEY = MAIN_NIFTY_SECURITY

# If true, legacy Telegram alert can be sent for backfill-detected touches at 09:18.
# In the new selected OR + EMA flow, actual sending is also controlled by
# OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED.
OPENING_RANGE_BACKFILL_TOUCH_ALERT_ENABLED = True

# If true, process live R3/S3 touches after OR levels are generated.
OPENING_RANGE_LIVE_TOUCH_ALERT_ENABLED = True

# Touch check mode.
# "high_low" means:
#   R3 touched if high >= r3
#   S3 touched if low <= s3
# "ltp" means:
#   R3 touched if ltp >= r3
#   S3 touched if ltp <= s3
# Recommended: high_low
OPENING_RANGE_TOUCH_CHECK_MODE = "high_low"

# Keep this true to store R3/S3 touch state inside opening range cache.
OPENING_RANGE_STORE_TOUCH_STATUS = True

# Optional output file for touch events if test/debug file saving is needed.
OPENING_RANGE_TOUCH_EVENTS_OUTPUT_FILE = "data/opening_range_touch_events.json"

# Save touch events file only when TEST_FLAG=True.
OPENING_RANGE_TOUCH_EVENTS_SAVE_TEST_FILE = True


# ============================================================
# New Opening Range Selected Instrument + EMA Alert Configuration
# ============================================================

# Enables new flow:
# 1. First live option instrument that touches/crosses R3 or S3 is selected permanently.
# 2. All other instruments are ignored after selection.
# 3. EMA crossover Telegram alerts are sent only for the selected instrument.
OPENING_RANGE_FIRST_TOUCH_SELECTION_ENABLED = True

# Source allowed to permanently select the first R3/S3 instrument.
#
# Recommended: "live_tick"
# This means only real live tick touches can lock the selected instrument.
#
# Other possible values:
#   "intraday_backfill_scan"  -> backfill touch can select instrument
#   "all" or "any"           -> either backfill or live tick can select instrument
OPENING_RANGE_FIRST_TOUCH_SELECTION_SOURCE = "live_tick"

# Sends one Telegram message when the first R3/S3 touched instrument is selected.
OPENING_RANGE_SELECTED_OR_TOUCH_NOTIFY_ENABLED = True

# Enables Telegram message when EMA crossover happens for the selected OR instrument.
OPENING_RANGE_SELECTED_OR_EMA_ALERT_ENABLED = True

# Legacy R3/S3 touch Telegram batch alert switch.
#
# False means:
#   Do not send old "Opening Range R3/S3 Touch Alert" batch messages.
#   Only selected instrument notification and selected instrument EMA cross alerts are sent.
#
# True means:
#   Old touch batch Telegram alerts are also sent.
#
# Recommended for new requirement: False
OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED = False

# If True, duplicate EMA alert protection uses:
#   selected instrument + EMA timestamp + cross type
#
# This prevents the same completed EMA candle crossover from sending twice.
OPENING_RANGE_SELECTED_OR_EMA_ALERT_ONCE_PER_CROSS = True


# ============================================================
# Test / Debug Configuration
# ============================================================

# If True, EMA cross result JSON files are saved under data/.
# If False, results are only kept in memory.
TEST_FLAG = False


# ============================================================
# Historical EMA Crossover Configuration
# ============================================================

EMA_FAST_PERIOD = 9
EMA_SLOW_PERIOD = 21

# Historical EMA crossover result output file.
# Raw candles are not saved here, only EMA/crossover summary.
EMA_CROSS_OUTPUT_FILE = "data/ema_cross_results.json"


# ============================================================
# Live EMA Crossover Configuration
# ============================================================

# Enables live EMA continuation from historical EMA state.
LIVE_EMA_ENABLED = True

# Live EMA interval in minutes.
# Phase 1 recommended value: 1
# This uses Upstox full-feed marketOHLC interval "I1".
LIVE_EMA_INTERVAL_MINUTES = 1

# Live EMA periods.
# Usually same as historical EMA periods.
LIVE_EMA_FAST_PERIOD = 9
LIVE_EMA_SLOW_PERIOD = 21

# Save live EMA cross events to file only if TEST_FLAG=True
# and LIVE_EMA_SAVE_TEST_FILE=True.
LIVE_EMA_SAVE_TEST_FILE = True

# Live EMA crossover event output file.
LIVE_EMA_OUTPUT_FILE = "data/live_ema_cross_results.json"

# Maximum number of live EMA cross events to keep in memory.
LIVE_EMA_MAX_EVENTS_IN_MEMORY = 5000
