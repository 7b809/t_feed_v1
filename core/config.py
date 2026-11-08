# core/config.py
"""Central configuration for the Instrument Isolation Service.

All values can be overridden with environment variables (loaded from
``.env`` via python-dotenv). Module-level constants are read **once**
at import time, so changes to ``.env`` require a process restart.
"""

import os
from dotenv import load_dotenv

load_dotenv()  # read .env if present


# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "UPSTOX_APP_V2")

# Collections
APP_TOKENS_COLLECTION = "app_tokens"
PROJECT_INFO_COLLECTION = "project_info"

# Well-known document _id values inside the collections above.
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

# Instrument keys subscribed in full mode on the live feed.
# These are the four indices monitored for R3/S3 isolation touches.
INSTRUMENT_KEYS = [
    "NSE_INDEX|Nifty 50",
    "NSE_INDEX|Nifty Bank",
    "NSE_INDEX|NIFTY MID SELECT",
    "BSE_INDEX|SENSEX",
]


# ---------------------------------------------------------------------------
# Isolation strategy
# ---------------------------------------------------------------------------
# Index priority for simultaneous-touch arbitration (highest priority first).
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

# Default bias side per trigger side.
STRATEGY_RELATIONAL_R3_BIAS = (
    os.getenv("STRATEGY_RELATIONAL_R3_BIAS", "CE").strip().upper()
)
STRATEGY_RELATIONAL_S3_BIAS = (
    os.getenv("STRATEGY_RELATIONAL_S3_BIAS", "PE").strip().upper()
)

# Execution-mode pinning thresholds (strategy §6.1).
ISOLATION_MODE_CUTOFF_HOUR = int(
    os.getenv("ISOLATION_MODE_CUTOFF_HOUR", "10")
)
ISOLATION_MODE_CUTOFF_MINUTE = int(
    os.getenv("ISOLATION_MODE_CUTOFF_MINUTE", "30")
)
ISOLATION_MODE_PRICE_HIGH = float(
    os.getenv("ISOLATION_MODE_PRICE_HIGH", "250")
)
ISOLATION_MODE_PRICE_LOW = float(
    os.getenv("ISOLATION_MODE_PRICE_LOW", "100")
)


# ---------------------------------------------------------------------------
# Market feed
# ---------------------------------------------------------------------------
# Subscription mode passed to ``MarketDataStreamerV3``. Default is
# ``full`` (which the server upgrades to ``full_d5`` on the wire — that
# upgrade is server-side and not configurable here).
#
# Valid values (per Upstox SDK docs):
#   ltpc           — last traded price + prev close only
#   full           — full feed (LTP, OHLC, 5-level depth); server-side d5
#   full_d30       — full + 30-level quotes
#   option_greeks  — option greeks only (options only)
#
# Invalid values are ignored at construction time in
# ``market_feed_service`` with a warning; the service falls back to
# ``full``.
FEED_MODE = os.getenv("FEED_MODE", "full").strip().lower()
VALID_FEED_MODES = ("ltpc", "full", "full_d30", "option_greeks")

# Restore the raw per-message DEBUG dump (troubleshooting only; floods logs).
LOG_RAW_FEED = (
    os.getenv("LOG_RAW_FEED", "false").strip().lower() == "true"
)

# One compact [HEALTH] line per instrument every N seconds.
FEED_HEALTH_INTERVAL_SECONDS = int(
    os.getenv("FEED_HEALTH_INTERVAL_SECONDS", "300")
)

# Flag an instrument STALE when its last update is older than this (seconds).
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
MARKET_CLOSE_MINUTE = int(os.getenv("MARKET_CLOSE_MINUTE", "30"))

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