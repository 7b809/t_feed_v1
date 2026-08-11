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
# Telegram can still be used for startup, token refresh, scheduler,
# instrument load, refresh, shutdown, and error notifications.
#
# Requirement:
# - Do not send legacy selected Opening Range instrument Telegram alerts.
# - Do not send generic EMA crossover Telegram alerts.
# - Generic EMA crossover events should go through WebSocket only.
#
# Strategy requirement:
# - OR touch + EMA confirmation alerts can be sent separately using
#   OR_EMA_STRATEGY_ALERT_ENABLED.

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
# Required for live EMA because EMA uses I1 candles from full feed.
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
# Requirement:
# - Opening Range should be calculated for every subscribed instrument.
# - Opening Range levels should be cached per instrument.
# - EMA crossover WebSocket events should include the matching instrument's
#   Opening Range range and levels when available.
# - Legacy selected Opening Range instrument flow remains disabled.

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
# Backfill scan is useful for diagnostics and strategy edge cases.
# It covers the case where S2/S3/R2/R3 was already touched before
# the scheduled 09:18 opening range job completed.

# Enables checking already completed intraday candles after the opening range window.
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
# Opening Range Live Touch Event Configuration
# ============================================================
# Requirement:
# - Touch monitoring may continue for WebSocket/debug visibility.
# - Legacy Telegram touch batch alerts are disabled.
# - Legacy first touched selected instrument flow remains disabled.

# Enables live touch monitoring after opening range levels are available.
OPENING_RANGE_TOUCH_ALERT_ENABLED = True

# Maximum instruments to include in one Telegram alert.
# Legacy behavior only. Kept for backward compatibility.
OPENING_RANGE_TOUCH_ALERT_MAX_INSTRUMENTS = 5

# Batch window in seconds.
# Legacy behavior only. Kept for backward compatibility.
OPENING_RANGE_TOUCH_ALERT_BATCH_SECONDS = 10

# Avoid duplicate touch event tracking for the same instrument and same level.
OPENING_RANGE_TOUCH_ALERT_ONCE_PER_LEVEL = True

# Include only option instruments in OR touch event tracking.
# NIFTY index OR levels can still be stored.
OPENING_RANGE_TOUCH_ALERT_OPTIONS_ONLY = True

# Use latest main index LTP to rank touched instruments by nearest strike.
# Legacy behavior only.
OPENING_RANGE_TOUCH_ALERT_SORT_BY_NEAREST_INDEX = True

# Main index key used for nearest strike calculation and NIFTY LTP tracking.
OPENING_RANGE_TOUCH_ALERT_MAIN_INDEX_KEY = MAIN_NIFTY_SECURITY

# Backfill touch events can be detected and stored, but legacy Telegram sending
# is disabled by OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED = False.
OPENING_RANGE_BACKFILL_TOUCH_ALERT_ENABLED = True

# If true, process live touches after OR levels are generated.
OPENING_RANGE_LIVE_TOUCH_ALERT_ENABLED = True

# Touch check mode.
# "high_low" means:
#   R levels touched if high >= level
#   S levels touched if low <= level
# "ltp" means:
#   R levels touched if ltp >= level
#   S levels touched if ltp <= level
# Recommended: high_low
OPENING_RANGE_TOUCH_CHECK_MODE = "high_low"

# Keep this true to store touch state inside opening range cache.
OPENING_RANGE_STORE_TOUCH_STATUS = True

# Optional output file for touch events if test/debug file saving is needed.
OPENING_RANGE_TOUCH_EVENTS_OUTPUT_FILE = "data/opening_range_touch_events.json"

# Save touch events file only when TEST_FLAG=True.
OPENING_RANGE_TOUCH_EVENTS_SAVE_TEST_FILE = True


# ============================================================
# Opening Range Selected Instrument Configuration
# ============================================================
# Requirement:
# - Disable legacy first touch selected instrument logic.
# - Disable legacy selected OR touch Telegram notification.
# - Disable legacy selected OR EMA Telegram alert.
# - Every instrument remains eligible for EMA crossover WebSocket broadcast.
# - EMA WebSocket payload should include that instrument's Opening Range levels.

# Disabled. Legacy selected OR instrument flow should not be used.
OPENING_RANGE_FIRST_TOUCH_SELECTION_ENABLED = False

# Kept only for backward compatibility.
OPENING_RANGE_FIRST_TOUCH_SELECTION_SOURCE = "disabled"

# Disabled. Do not send Telegram when any instrument touches R3/S3 through legacy flow.
OPENING_RANGE_SELECTED_OR_TOUCH_NOTIFY_ENABLED = False

# Disabled. Do not send Telegram for legacy selected OR EMA cross.
OPENING_RANGE_SELECTED_OR_EMA_ALERT_ENABLED = False

# Disabled. Do not send old grouped Opening Range touch Telegram alerts.
OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED = False

# Disabled by behavior because selected OR EMA alerts are disabled.
# Kept only for backward compatibility with existing code references.
OPENING_RANGE_SELECTED_OR_EMA_ALERT_ONCE_PER_CROSS = False


# ============================================================
# EMA + Opening Range WebSocket Enrichment Configuration
# ============================================================
# Requirement:
# - For every live EMA crossover event, attach Opening Range details
#   of the same instrument before broadcasting through WebSocket.

EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS = True

# If Opening Range levels are not available yet, still broadcast EMA cross event
# with opening_range.available = False.
EMA_CROSS_BROADCAST_WITHOUT_OPENING_RANGE = True


# ============================================================
# Opening Range Touch + EMA Strategy Alert Configuration
# ============================================================
# Strategy requirement:
# 1. After Opening Range is calculated, read NIFTY Opening Range average.
# 2. Build eligible strike universe using:
#       NIFTY opening range average +/- OR_EMA_STRATEGY_STRIKE_WINDOW_POINTS
# 3. Clamp that range using STRIKE_FROM and STRIKE_TO.
# 4. During live ticks, check only eligible option instruments.
# 5. If eligible option instruments touch/cross configured OR levels:
#       - If only one instrument touches, select that instrument.
#       - If multiple instruments touch at same timestamp/candle,
#         select the strike nearest to current NIFTY spot.
#       - If another instrument touches later, keep it only for debug.
# 6. Only the selected touched instrument waits for live EMA confirmation.
# 7. When selected instrument gets EMA cross, send Telegram strategy alert
#    with nearest strike live data.
#
# This is separate from old selected OR 