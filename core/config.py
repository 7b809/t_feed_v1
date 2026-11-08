# core/config.py
"""Central configuration for the Instrument Isolation Service."""

import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "UPSTOX_APP_V2")

APP_TOKENS_COLLECTION = "app_tokens"
PROJECT_INFO_COLLECTION = "project_info"
ISOLATION_EVENTS_COLLECTION = os.getenv(
    "ISOLATION_EVENTS_COLLECTION", "isolation_events"
)

TOKEN_DOC_ID = "upstox_access_token"
PIVOT_DOC_ID = "pivot_levels"
ISOLATION_DOC_ID = "instrument_isolation"

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
TELEGRAM_ENABLED = (
    os.getenv("TELEGRAM_ENABLED", "false").strip().lower() == "true"
)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Upstox
# ---------------------------------------------------------------------------
UPSTOX_API_VERSION = os.getenv("UPSTOX_API_VERSION", "2.0")
UPSTOX_SEARCH_URL = os.getenv(
    "UPSTOX_SEARCH_URL", "https://api.upstox.com/v2/instruments/search"
)

INSTRUMENT_KEYS = [
    "NSE_INDEX|Nifty 50",
    "NSE_INDEX|Nifty Bank",
    "NSE_INDEX|NIFTY MID SELECT",
    "BSE_INDEX|SENSEX",
]

INDEX_METADATA = {
    "NSE_INDEX|Nifty 50": {
        "canonical": "NIFTY", "folder": "nifty",
        "underlying": "NIFTY", "trading_symbol": "NIFTY", "priority": 1,
    },
    "NSE_INDEX|Nifty Bank": {
        "canonical": "BANKNIFTY", "folder": "banknifty",
        "underlying": "NIFTY BANK", "trading_symbol": "BANKNIFTY", "priority": 2,
    },
    "NSE_INDEX|NIFTY MID SELECT": {
        "canonical": "MIDCPNIFTY", "folder": "midcpnifty",
        "underlying": "NIFTY MID SELECT", "trading_symbol": "MIDCPNIFTY", "priority": 3,
    },
    "BSE_INDEX|SENSEX": {
        "canonical": "SENSEX", "folder": "sensex",
        "underlying": "SENSEX", "trading_symbol": "SENSEX", "priority": 4,
    },
}

# ---------------------------------------------------------------------------
# Isolation strategy
# ---------------------------------------------------------------------------
STRATEGY_INDEX_PRIORITY = [
    k.strip()
    for k in os.getenv(
        "STRATEGY_INDEX_PRIORITY",
        ",".join([
            "NSE_INDEX|NIFTY MID SELECT",
            "NSE_INDEX|Nifty Bank",
            "NSE_INDEX|Nifty 50",
            "BSE_INDEX|SENSEX",
        ]),
    ).split(",")
    if k.strip()
]

STRATEGY_RELATIONAL_R3_BIAS = (
    os.getenv("STRATEGY_RELATIONAL_R3_BIAS", "CE").strip().upper()
)
STRATEGY_RELATIONAL_S3_BIAS = (
    os.getenv("STRATEGY_RELATIONAL_S3_BIAS", "PE").strip().upper()
)

ISOLATION_BIAS_SOURCE = (
    os.getenv("ISOLATION_BIAS_SOURCE", "nifty_trend").strip().lower()
)
if ISOLATION_BIAS_SOURCE not in ("nifty_trend", "trigger_side"):
    ISOLATION_BIAS_SOURCE = "nifty_trend"

ISOLATION_MODE_CUTOFF_HOUR = int(os.getenv("ISOLATION_MODE_CUTOFF_HOUR", "10"))
ISOLATION_MODE_CUTOFF_MINUTE = int(os.getenv("ISOLATION_MODE_CUTOFF_MINUTE", "30"))
ISOLATION_MODE_PRICE_HIGH = float(os.getenv("ISOLATION_MODE_PRICE_HIGH", "250"))
ISOLATION_MODE_PRICE_LOW = float(os.getenv("ISOLATION_MODE_PRICE_LOW", "100"))

ISOLATION_SCAN_INTERVAL_SECONDS = int(
    os.getenv("ISOLATION_SCAN_INTERVAL_SECONDS", "30")
)
ISOLATION_OPTION_LTP_WAIT_SECONDS = int(
    os.getenv("ISOLATION_OPTION_LTP_WAIT_SECONDS", "5")
)

ISOLATION_SCAN_START_HOUR = int(os.getenv("MARKET_OPEN_HOUR", "9"))
ISOLATION_SCAN_START_MINUTE = int(os.getenv("MARKET_OPEN_MINUTE", "15"))
ISOLATION_SCAN_END_HOUR = int(os.getenv("ISOLATION_SCAN_END_HOUR", "15"))
ISOLATION_SCAN_END_MINUTE = int(os.getenv("ISOLATION_SCAN_END_MINUTE", "40"))

# ---------------------------------------------------------------------------
# NIFTY option search
# ---------------------------------------------------------------------------
NIFTY_STRIKE_STEP = int(os.getenv("NIFTY_STRIKE_STEP", "50"))
NIFTY_SEARCH_EXCHANGE = os.getenv("NIFTY_SEARCH_EXCHANGE", "NSE")
NIFTY_SEARCH_SEGMENT = os.getenv("NIFTY_SEARCH_SEGMENT", "FO")
NIFTY_SEARCH_EXPIRY = os.getenv("NIFTY_SEARCH_EXPIRY", "current_week")
NIFTY_SEARCH_USE_ATM_OFFSET = (
    os.getenv("NIFTY_SEARCH_USE_ATM_OFFSET", "false").strip().lower() == "true"
)

# ---------------------------------------------------------------------------
# NIFTY trend (EMA based)
# ---------------------------------------------------------------------------
NIFTY_TREND_MA_PERIOD = int(os.getenv("NIFTY_TREND_MA_PERIOD", "20"))
NIFTY_TREND_SLOPE_LOOKBACK = int(
    os.getenv("NIFTY_TREND_SLOPE_LOOKBACK", "5")
)
NIFTY_TREND_NEUTRAL_FALLBACK = (
    os.getenv("NIFTY_TREND_NEUTRAL_FALLBACK", "CE").strip().upper()
)
if NIFTY_TREND_NEUTRAL_FALLBACK not in ("CE", "PE"):
    NIFTY_TREND_NEUTRAL_FALLBACK = "CE"

# ---------------------------------------------------------------------------
# OR levels (Opening Range)
# ---------------------------------------------------------------------------
OR_FETCH_UNIT = os.getenv("OR_FETCH_UNIT", "minutes")
OR_FETCH_INTERVAL = os.getenv("OR_FETCH_INTERVAL", "1")
OR_AUTO_COMPUTE_ON_STARTUP = (
    os.getenv("OR_AUTO_COMPUTE_ON_STARTUP", "true").strip().lower() == "true"
)

OR_LEVELS_LOCAL_PATH = os.getenv(
    "OR_LEVELS_LOCAL_PATH", "data/or_levels.json"
)

# ---------------------------------------------------------------------------
# Startup backfill
# ---------------------------------------------------------------------------
BACKFILL_ENABLED = (
    os.getenv("BACKFILL_ENABLED", "true").strip().lower() == "true"
)
BACKFILL_WINDOW_START_HOUR = int(
    os.getenv("BACKFILL_WINDOW_START_HOUR", "9")
)
BACKFILL_WINDOW_START_MINUTE = int(
    os.getenv("BACKFILL_WINDOW_START_MINUTE", "15")
)
BACKFILL_WINDOW_END_HOUR = int(os.getenv("BACKFILL_WINDOW_END_HOUR", "23"))
BACKFILL_WINDOW_END_MINUTE = int(
    os.getenv("BACKFILL_WINDOW_END_MINUTE", "59")
)

# ---------------------------------------------------------------------------
# Deferred startup gate
# ---------------------------------------------------------------------------
# When TEST_FLAG=false and process starts before the cutoff, go fully
# idle until the cutoff, then run the startup sequence (daily refresh →
# backfill → scheduler already running).
#
# When TEST_FLAG=true, run the startup sequence immediately regardless
# of the current time.
TEST_FLAG = (
    os.getenv("TEST_FLAG", "false").strip().lower() == "true"
)

DEFERRED_STARTUP_HOUR = int(os.getenv("DEFERRED_STARTUP_HOUR", "9"))
DEFERRED_STARTUP_MINUTE = int(os.getenv("DEFERRED_STARTUP_MINUTE", "0"))

# ---------------------------------------------------------------------------
# Market feed
# ---------------------------------------------------------------------------
FEED_MODE = os.getenv("FEED_MODE", "full").strip().lower()
VALID_FEED_MODES = ("ltpc", "full", "full_d30", "option_greeks")
LOG_RAW_FEED = (
    os.getenv("LOG_RAW_FEED", "false").strip().lower() == "true"
)
FEED_HEALTH_INTERVAL_SECONDS = int(
    os.getenv("FEED_HEALTH_INTERVAL_SECONDS", "300")
)
FEED_STALE_AFTER_SECONDS = int(
    os.getenv("FEED_STALE_AFTER_SECONDS", "120")
)

# ---------------------------------------------------------------------------
# Market session + scheduling (IST)
# ---------------------------------------------------------------------------
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")

MARKET_OPEN_HOUR = int(os.getenv("MARKET_OPEN_HOUR", "9"))
MARKET_OPEN_MINUTE = int(os.getenv("MARKET_OPEN_MINUTE", "15"))
MARKET_CLOSE_HOUR = int(os.getenv("MARKET_CLOSE_HOUR", "15"))
MARKET_CLOSE_MINUTE = int(os.getenv("MARKET_CLOSE_MINUTE", "40"))

DAILY_REFRESH_HOUR = int(os.getenv("DAILY_REFRESH_HOUR", "9"))
DAILY_REFRESH_MINUTE = int(os.getenv("DAILY_REFRESH_MINUTE", "0"))
PIVOT_CHECK_HOUR = int(os.getenv("PIVOT_CHECK_HOUR", "9"))
PIVOT_CHECK_MINUTE = int(os.getenv("PIVOT_CHECK_MINUTE", "18"))
DISCONNECT_HOUR = int(os.getenv("DISCONNECT_HOUR", "15"))
DISCONNECT_MINUTE = int(os.getenv("DISCONNECT_MINUTE", "45"))

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8001"))