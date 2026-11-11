# core/config.py
"""Central configuration for the Instrument Isolation Service.

All values are read from environment variables (usually via a ``.env``
file loaded by :func:`dotenv.load_dotenv`) at **import time**. Every
module that needs a knob imports it from here; nothing re-reads the
environment after this module loads, so a live edit to ``.env``
requires a process restart to take effect.

Notes on specific knobs
-----------------------
``DEFERRED_STARTUP_MINUTE``
    Defaults to **18**. Values below 17 cause the deferred OR-levels
    compute to run before the 09:15 opening candle exists, producing a
    structurally-wrong ``pivot_levels`` document (yesterday's candles
    tagged with today's ``trade_date_ist``). 18 matches the recommended
    value from ``.env.example`` and the ``PIVOT_CHECK_MINUTE`` the
    scheduler uses for its 09:18 recheck.

``ISOLATION_SCAN_START_HOUR`` / ``ISOLATION_SCAN_START_MINUTE``
    These are configurable **independently** of ``MARKET_OPEN_HOUR`` /
    ``MARKET_OPEN_MINUTE`` via the ``ISOLATION_SCAN_START_*`` env vars.
    If those are absent, the values fall back to ``MARKET_OPEN_*`` —
    preserving the original behaviour where the scan window began at
    market open. Reading from ``ISOLATION_SCAN_START_*`` first lets a
    deployment delay the scan start (e.g. to 09:18) without having to
    also move the market-open boundary used elsewhere.

``NIFTY_SEARCH_EXPIRY``
    The **primary** expiry keyword tried by
    :class:`core.instrument_search_service.InstrumentSearchService`.
    Default ``current_week``. On expiry day, or once the weekly roll
    has occurred at the provider, this keyword can resolve to nothing
    for the current ATM strike. ``InstrumentSearchService`` therefore
    also tries concrete ``YYYY-MM-DD`` dates for the next two Thursdays
    and ``current_month`` as fallbacks — see
    ``InstrumentSearchService._expiry_candidates``. Do **not** remove
    the ``current_week`` default: it is the only value that works when
    the provider is consistent, and the fallback chain costs nothing
    when it succeeds first try.

``NIFTY_SEARCH_USE_ATM_OFFSET``
    Legacy flag from an earlier iteration. Currently unused by any
    code path in this project. Kept only for backward compatibility
    with any external scripts that may import it. Do not rely on it.

``BACKFILL_WINDOW_*``
    The window in which the startup backfill will attempt to
    reconstruct a missed isolation. Defaults to Mon–Fri 09:15–23:59
    IST — deliberately extends past market close (15:30) so a process
    that crashes mid-session and is restarted in the evening can still
    recover the missing isolation record for the day.
"""

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

# Scan-window bounds. The start reads ISOLATION_SCAN_START_* first so a
# deployment can delay the scan start (e.g. until 09:18, when OR levels
# are actually ready) without moving MARKET_OPEN_*. Falls back to
# MARKET_OPEN_* when ISOLATION_SCAN_START_* is not set, preserving the
# original behaviour. See module docstring.
ISOLATION_SCAN_START_HOUR = int(
    os.getenv(
        "ISOLATION_SCAN_START_HOUR",
        os.getenv("MARKET_OPEN_HOUR", "9"),
    )
)
ISOLATION_SCAN_START_MINUTE = int(
    os.getenv(
        "ISOLATION_SCAN_START_MINUTE",
        os.getenv("MARKET_OPEN_MINUTE", "15"),
    )
)
ISOLATION_SCAN_END_HOUR = int(os.getenv("ISOLATION_SCAN_END_HOUR", "15"))
ISOLATION_SCAN_END_MINUTE = int(os.getenv("ISOLATION_SCAN_END_MINUTE", "40"))

# ---------------------------------------------------------------------------
# NIFTY option search
# ---------------------------------------------------------------------------
NIFTY_STRIKE_STEP = int(os.getenv("NIFTY_STRIKE_STEP", "50"))
NIFTY_SEARCH_EXCHANGE = os.getenv("NIFTY_SEARCH_EXCHANGE", "NSE")
NIFTY_SEARCH_SEGMENT = os.getenv("NIFTY_SEARCH_SEGMENT", "FO")

# Primary expiry keyword. Tried first; on empty result, the search
# service falls back to concrete Thursday dates and ``current_month``.
# See module docstring ("NIFTY_SEARCH_EXPIRY") and
# ``InstrumentSearchService._expiry_candidates``.
NIFTY_SEARCH_EXPIRY = os.getenv("NIFTY_SEARCH_EXPIRY", "current_week")

# Legacy: previously toggled whether the search used server-side
# ``atm_offset`` instead of an explicit strike. No longer consulted by
# any code path in this project. Kept for backward compatibility with
# external scripts that may import it.
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
#
# Window bounds default to Mon–Fri 09:15–23:59 IST. The late end-time is
# deliberate: a process that crashes mid-session and is restarted in the
# evening can still reconstruct the missed isolation record for the day.
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
#
# When TEST_FLAG=false and the process starts before the cutoff, go
# fully idle until the cutoff, then run the startup sequence
# (daily refresh → backfill) via the scheduler.
#
# When TEST_FLAG=true, run the startup sequence immediately regardless
# of the current time.
#
# DEFERRED_STARTUP_MINUTE defaults to 18 — see module docstring
# ("DEFERRED_STARTUP_MINUTE") for why values < 17 are unsafe.
# ---------------------------------------------------------------------------
TEST_FLAG = (
    os.getenv("TEST_FLAG", "false").strip().lower() == "true"
)

DEFERRED_STARTUP_HOUR = int(os.getenv("DEFERRED_STARTUP_HOUR", "9"))
DEFERRED_STARTUP_MINUTE = int(os.getenv("DEFERRED_STARTUP_MINUTE", "18"))

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
# Isolation client notifications (fire-and-forget)
# ---------------------------------------------------------------------------
ISOLATION_NOTIFY_ENABLED = (
    os.getenv("ISOLATION_NOTIFY_ENABLED", "false").strip().lower() == "true"
)

# Comma-separated URLs, e.g. https://client-a.example/api/isolation,https://client-b.example/api/isolation
ISOLATION_NOTIFY_CLIENT_URLS = [
    url.strip()
    for url in os.getenv("ISOLATION_NOTIFY_CLIENT_URLS", "").split(",")
    if url.strip()
]

# Shared secret. Both this service (sender) and each client (receiver)
# must configure the SAME value; the sender transmits it under
# ISOLATION_NOTIFY_CLIENT_KEY_HEADER. Empty means "send without a key".
ISOLATION_NOTIFY_CLIENT_KEY = (
    os.getenv("ISOLATION_NOTIFY_CLIENT_KEY", "").strip()
)

# Header name under which the shared key is sent. Defaults to
# "X-Isolation-Key". Change to "X-API-Key" / "Authorization" / etc. if
# the client expects something else.
ISOLATION_NOTIFY_CLIENT_KEY_HEADER = (
    os.getenv("ISOLATION_NOTIFY_CLIENT_KEY_HEADER", "X-Isolation-Key").strip()
    or "X-Isolation-Key"
)

ISOLATION_NOTIFY_TIMEOUT_SECONDS = float(
    os.getenv("ISOLATION_NOTIFY_TIMEOUT_SECONDS", "5")
)
TOKEN_CACHE_TTL_SECONDS = int(os.getenv("TOKEN_CACHE_TTL_SECONDS", "1800"))
# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8001"))