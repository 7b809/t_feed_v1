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
