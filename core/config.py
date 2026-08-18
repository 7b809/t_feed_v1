import os
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# MongoDB Configuration
# ============================================================

MONGO_URI = os.getenv("MONGO_URL")
MONGO_DB = os.getenv("MONGO_DB")
TOKENS_COLLECTION = os.getenv("TOKENS_COLLECTION")

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
# Separate background task for validating the current Upstox access token.
#
# Current token MongoDB document format:
# {
#   "_id": "upstox_access_token",
#   "access_token": "eyJ0eXAiOiJK...",
#   "created_at": "2026-08-13T07:16:44.000000+05:30",
#   "updated_at": "2026-08-13T07:16:44.000000+05:30"
# }
#
# Token validation logic:
# - Read token document from MongoDB.
# - Call Upstox get_profile API using the current token.
# - If profile API returns success, token is considered valid.
# - If profile API fails, token is considered expired, invalid, or corrupted.
# - Send Telegram alert asking user to save new token.
#
# Telegram command flow:
# - /save-token:
#     Bot asks user to paste raw token in next message.
#     Bot deletes raw token message after receiving it.
#     Bot validates token using get_profile.
#     Bot saves token to MongoDB if valid.
#
# - /token-status:
#     Bot returns masked token status and validation metadata.
#
# - /profile:
#     Bot calls Upstox profile API and returns raw response to Telegram.
#
# - /funds:
#     Bot calls Upstox funds and margin API and returns raw response to Telegram.

UPSTOX_TOKEN_DOC_ID = os.getenv(
    "UPSTOX_TOKEN_DOC_ID",
    "upstox_access_token",
)

# Background token validity check interval.
# Default: every 30 minutes.
UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES = int(
    os.getenv("UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES", "30")
)

# Enable or disable Telegram token command bot.
TELEGRAM_TOKEN_BOT_ENABLED = (
    os.getenv("TELEGRAM_TOKEN_BOT_ENABLED", "true").lower() == "true"
)

# Telegram polling interval in seconds.
# Used between polling failures or loop waits.
TELEGRAM_TOKEN_BOT_POLL_SECONDS = int(
    os.getenv("TELEGRAM_TOKEN_BOT_POLL_SECONDS", "3")
)

# Telegram long polling timeout in seconds.
TELEGRAM_TOKEN_BOT_LONG_POLL_TIMEOUT = int(
    os.getenv("TELEGRAM_TOKEN_BOT_LONG_POLL_TIMEOUT", "20")
)

# Restrict token commands only to TELEGRAM_CHAT_ID.
# Strongly recommended to keep True.
TELEGRAM_TOKEN_BOT_RESTRICT_TO_CHAT = (
    os.getenv("TELEGRAM_TOKEN_BOT_RESTRICT_TO_CHAT", "true").lower() == "true"
)


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
# Opening Range is calculated for every subscribed instrument.
# Opening Range levels are cached per instrument.
# EMA crossover WebSocket events can include the matching instrument's
# Opening Range range and levels when available.

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
# Backfill scan detects R2/R3/S2/S3 touches that may have already happened
# between Opening Range completion and the scheduled 09:18 fetch.

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
# Live touch monitoring continues after Opening Range levels are ready.
# Touch events are used for diagnostics, WebSocket visibility,
# and isolated instrument selection.

# Enables live R2/R3/S2/S3 touch monitoring after opening range levels are available.
OPENING_RANGE_TOUCH_ALERT_ENABLED = True

# Maximum instruments to include in one legacy grouped Telegram alert.
# Kept for backward compatibility only.
OPENING_RANGE_TOUCH_ALERT_MAX_INSTRUMENTS = 5

# Batch window in seconds.
# Kept for backward compatibility only.
OPENING_RANGE_TOUCH_ALERT_BATCH_SECONDS = 10

# Avoid duplicate touch event tracking for the same instrument and same level.
OPENING_RANGE_TOUCH_ALERT_ONCE_PER_LEVEL = True

# Include only option instruments in OR touch event tracking.
# NIFTY index OR levels can still be stored.
OPENING_RANGE_TOUCH_ALERT_OPTIONS_ONLY = True

# Use latest main index LTP to rank touched instruments by nearest strike.
OPENING_RANGE_TOUCH_ALERT_SORT_BY_NEAREST_INDEX = True

# Main index key used for nearest strike calculation and NIFTY LTP tracking.
OPENING_RANGE_TOUCH_ALERT_MAIN_INDEX_KEY = MAIN_NIFTY_SECURITY

# Backfill touch events can be detected and stored.
OPENING_RANGE_BACKFILL_TOUCH_ALERT_ENABLED = True

# If true, process live R2/R3/S2/S3 touches after OR levels are generated.
OPENING_RANGE_LIVE_TOUCH_ALERT_ENABLED = True

# Touch check mode.
# "high_low" means:
#   Resistance touched if high >= level
#   Support touched if low <= level
# "ltp" means:
#   Resistance touched if ltp >= level
#   Support touched if ltp <= level
# Recommended: high_low
OPENING_RANGE_TOUCH_CHECK_MODE = "high_low"

# Keep this true to store R2/R3/S2/S3 touch state inside opening range cache.
OPENING_RANGE_STORE_TOUCH_STATUS = True

# Optional output file for touch events if test/debug file saving is needed.
OPENING_RANGE_TOUCH_EVENTS_OUTPUT_FILE = "data/opening_range_touch_events.json"

# Save touch events file only when TEST_FLAG=True.
OPENING_RANGE_TOUCH_EVENTS_SAVE_TEST_FILE = True


# ============================================================
# Opening Range Isolated Instrument Configuration
# ============================================================
# Requirement:
# - After Opening Range is ready, take Opening Range average.
# - Build average +/- 500 points strike window.
# - Clamp the window inside STRIKE_FROM and STRIKE_TO.
# - Monitor only eligible option instruments inside this window.
# - If R3/S3 is touched, isolate nearest R3/S3 instrument to average.
# - If no R3/S3 touch exists, use R2/S2 and isolate nearest instrument to average.
# - Once isolated, lock that instrument for the trading day.
# - Live EMA still runs for all instruments.
# - Telegram EMA alerts are sent only for the isolated instrument.
# - EMA alert order side is decided dynamically:
#     bullish_cross -> same side as isolated instrument
#     bearish_cross -> opposite side of isolated instrument

OPENING_RANGE_ISOLATED_INSTRUMENT_ENABLED = True

# Around Opening Range average, consider strikes within +/- this value.
# Example:
#   OR average = 24570
#   window = 24570 +/- 500
#   final range is clamped inside STRIKE_FROM and STRIKE_TO.
OPENING_RANGE_ISOLATION_AVERAGE_WINDOW_POINTS = 700.0

# Only these Opening Range levels are eligible for isolation.
OPENING_RANGE_ISOLATION_TOUCH_LEVELS = ["R2", "R3", "S2", "S3"]

# Level priority for choosing isolated instrument.
# R3/S3 gets higher priority than R2/S2.
# If multiple instruments exist in same priority group, nearest strike to OR average wins.
OPENING_RANGE_ISOLATION_PRIORITY_LEVELS = ["S3", "R3", "S2", "R2"]

# Once one instrument is selected, keep it locked for the trading day.
OPENING_RANGE_ISOLATION_LOCK_FOR_DAY = True

# Allow higher-priority level to upgrade existing isolated instrument.
# Example:
#   Existing isolated instrument came from R2/S2.
#   Later R3/S3 qualifies.
#   If True, higher-priority R3/S3 can replace R2/S2.
OPENING_RANGE_ISOLATION_ALLOW_PRIORITY_UPGRADE = True

# Send Telegram notification when instrument is isolated.
OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED = True

# Include backfill touches also for isolation.
# This covers the case where R2/R3/S2/S3 was touched before 09:18.
OPENING_RANGE_ISOLATION_ALLOW_BACKFILL_TOUCH = True

# Include live tick touches also for isolation after 09:18.
OPENING_RANGE_ISOLATION_ALLOW_LIVE_TOUCH = True

# Restrict isolation only to option instruments.
OPENING_RANGE_ISOLATION_OPTIONS_ONLY = True

# Save isolated instrument state to file only when TEST_FLAG=True if implemented.
OPENING_RANGE_ISOLATED_INSTRUMENT_OUTPUT_FILE = (
    "data/isolated_opening_range_instrument.json"
)

# Reset isolated instrument daily based on market date.
OPENING_RANGE_ISOLATION_RESET_DAILY = True


# ============================================================
# Backward-Compatible Selected Instrument Configuration
# ============================================================
# Existing selected OR names are retained for compatibility.
# New implementation should map selected OR behavior to isolated instrument behavior.

# Enabled now because isolated instrument selection is required.
OPENING_RANGE_FIRST_TOUCH_SELECTION_ENABLED = True

# New source name for clarity.
OPENING_RANGE_FIRST_TOUCH_SELECTION_SOURCE = "average_window_level_priority"

# Send Telegram when isolated instrument is selected.
OPENING_RANGE_SELECTED_OR_TOUCH_NOTIFY_ENABLED = True

# Send Telegram EMA crossover alerts only for isolated instrument.
OPENING_RANGE_SELECTED_OR_EMA_ALERT_ENABLED = True

# Old grouped touch Telegram alerts are still disabled.
# We only want selected/isolated instrument notifications and isolated EMA alerts.
OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED = False

# If True, send only one alert per cross type.
# Requirement says every EMA cross of isolated instrument should alert,
# so keep this False.
OPENING_RANGE_SELECTED_OR_EMA_ALERT_ONCE_PER_CROSS = False


# ============================================================
# EMA + Opening Range WebSocket Enrichment Configuration
# ============================================================
# For every live EMA crossover event, attach Opening Range details
# of the same instrument before broadcasting through WebSocket.

EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS = True

# If Opening Range levels are not available yet, still broadcast EMA cross event
# with opening_range.available = False.
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
# New dynamic order side rule:
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

# Send every EMA crossover alert for the isolated instrument.
EMA_ISOLATED_ALERT_EVERY_CROSS = True

# Required Telegram alert format concept:
#   {strike value} CE/PE - crosses {levelname} - At {current nifty live point}
#   EMA Cross: bullish/bearish
#   EMA candle close: ...
#   EMA candle timestamp: ...
#   Isolated Instrument Type: CE/PE
#   Suggested Order Side: CE/PE
#   Nearest instrument details:
#      24350PE - 135rs
EMA_ISOLATED_ALERT_INCLUDE_LEVEL_NAME = True
EMA_ISOLATED_ALERT_INCLUDE_NIFTY_LTP = True
EMA_ISOLATED_ALERT_INCLUDE_EMA_DETAILS = True
EMA_ISOLATED_ALERT_INCLUDE_NEAREST_ORDER_INSTRUMENTS = True

# Backward-compatible fallback only.
# These are used only when isolated instrument type is unavailable.
# Normal expected flow should use:
#   bullish_cross -> isolated instrument side
#   bearish_cross -> opposite side of isolated instrument
EMA_ALERT_BULLISH_OPTION_TYPE = "CE"
EMA_ALERT_BEARISH_OPTION_TYPE = "PE"

# Strike step for NIFTY options.
EMA_ALERT_STRIKE_STEP = 50

# For order suggestion, use three nearest strikes around current NIFTY spot.
# Example:
#   NIFTY = 24333
#   nearest 50-point strikes can be 24300, 24350, 24400.
EMA_ALERT_NEAREST_STRIKE_COUNT = 3

# Offsets around nearest rounded strike.
# With STRIKE_STEP=50, this gives:
#   nearest - 50
#   nearest
#   nearest + 50
EMA_ALERT_NEAREST_STRIKE_OFFSETS = [-50, 0, 50]

# Clamp suggested order strikes inside configured STRIKE_FROM and STRIKE_TO.
EMA_ALERT_ORDER_STRIKES_CLAMP_TO_FILTER_RANGE = True

# Include live price/LTP for suggested CE/PE instruments when available.
EMA_ALERT_INCLUDE_ORDER_INSTRUMENT_LTP = True

# If suggested instrument live price is unavailable, still show the strike and type.
EMA_ALERT_SHOW_ORDER_INSTRUMENT_WHEN_LTP_MISSING = True

# Maximum number of suggested instruments to append in Telegram message.
EMA_ALERT_MAX_ORDER_INSTRUMENTS = 3


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
# Live EMA should run for all subscribed instruments.
# Every EMA crossover can still be broadcast through WebSocket.
# Telegram alerting is restricted to isolated instrument only.

# Enables live EMA continuation from historical EMA state.
LIVE_EMA_ENABLED = True

# Live EMA calculation mode flag.
#
# False = candle based EMA calculation.
#         Uses completed 1-minute I1 candle close.
#         This is the current/default stable behavior.
#
# True  = tick based EMA calculation.
#         Uses every incoming live LTP tick.
#         This gives faster signals but can be noisier.
#
# In both modes:
# - EMA runs for all subscribed instruments.
# - WebSocket EMA events can be broadcast for all instruments.
# - Telegram EMA alerts are sent only for the isolated instrument.
# - Suggested order side follows the dynamic isolated-side rule:
#     bullish_cross -> same side as isolated instrument
#     bearish_cross -> opposite side of isolated instrument
LIVE_EMA_CALCULATION_MODE = True

# Live EMA interval in minutes.
# Used only when LIVE_EMA_CALCULATION_MODE=False.
# Phase 1 recommended value: 1
# This uses Upstox full-feed marketOHLC interval "I1".
LIVE_EMA_INTERVAL_MINUTES = 1

# Live EMA periods.
# Usually same as historical EMA periods.
LIVE_EMA_FAST_PERIOD = 9
LIVE_EMA_SLOW_PERIOD = 21

# Tick-based EMA duplicate alert control.
# Used only when LIVE_EMA_CALCULATION_MODE=True.
# If True, avoids repeated same-direction tick cross alerts until direction changes again.
LIVE_EMA_TICK_ALERT_ONCE_PER_DIRECTION = False

# Tick-based EMA minimum LTP movement filter.
# Used only when LIVE_EMA_CALCULATION_MODE=True.
# 0 means process every tick LTP.
# Example: 0.05 means ignore tick EMA recalculation if LTP changed less than 0.05.
LIVE_EMA_TICK_MIN_PRICE_CHANGE = 0.0

# Save live EMA cross events to file only if TEST_FLAG=True
# and LIVE_EMA_SAVE_TEST_FILE=True.
LIVE_EMA_SAVE_TEST_FILE = True

# Live EMA crossover event output file.
LIVE_EMA_OUTPUT_FILE = "data/live_ema_cross_results.json"

# Maximum number of live EMA cross events to keep in memory.
LIVE_EMA_MAX_EVENTS_IN_MEMORY = 5000