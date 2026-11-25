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
# Isolation-created announcements use a dedicated official group target.
TELEGRAM_GROUP_ENABLED = (
    os.getenv("TELEGRAM_GROUP_ENABLED", "true").strip().lower() == "true"
)
TELEGRAM_GROUP_CHAT_ID = os.getenv("TELEGRAM_GROUP_CHAT_ID", "")

# Public chart page base URL. Instrument keys are URL-encoded per notification.
CHART_BASE_URL = os.getenv("CHART_BASE_URL", "").strip().rstrip("/")

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

# Read-only Supertrend history loaded when an instrument is first isolated.
ISOLATION_SUPERTREND_BACKFILL_DIR = os.getenv(
    "ISOLATION_SUPERTREND_BACKFILL_DIR", ""
).strip()

# Price-based pre-isolation Supertrend recovery. Historical decisions are
# informational unless LIVE_GTT_EXECUTION_ENABLED is also enabled.
ISOLATION_SUPERTREND_RECOVERY_ENABLED = (
    os.getenv("ISOLATION_SUPERTREND_RECOVERY_ENABLED", "true").strip().lower()
    in {"1", "true", "yes", "on"}
)
ISOLATION_SUPERTREND_MIN_MOVE_PERCENT = float(
    os.getenv("ISOLATION_SUPERTREND_MIN_MOVE_PERCENT", "0.10")
)
ISOLATION_SUPERTREND_RECOVERY_MAX_DELAY_SECONDS = max(
    1, int(os.getenv("ISOLATION_SUPERTREND_RECOVERY_MAX_DELAY_SECONDS", "60"))
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

# ---------------------------------------------------------------------------
# AlgoCLI test API (disabled by default; live broker writes require two gates)
# ---------------------------------------------------------------------------
ALGOCLI_TEST_API_ENABLED = os.getenv("ALGOCLI_TEST_API_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
ALGOCLI_TEST_API_KEY = os.getenv("ALGOCLI_TEST_API_KEY", "").strip()
ALGOCLI_TEST_LIVE_WRITES_ENABLED = os.getenv("ALGOCLI_TEST_LIVE_WRITES_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
ALGOCLI_TEST_DATA_DIR = os.getenv("ALGOCLI_TEST_DATA_DIR", "data/algocli_tests").strip() or "data/algocli_tests"

# ---------------------------------------------------------------------------
# Daily MEGA data backup
# ---------------------------------------------------------------------------
DAILY_DATA_BACKUP_ENABLED = (
    os.getenv("DAILY_DATA_BACKUP_ENABLED", "true").strip().lower() == "true"
)
DAILY_DATA_BACKUP_DIR = os.getenv("DAILY_DATA_BACKUP_DIR", "./backups")
DAILY_DATA_BACKUP_HOUR = int(os.getenv("DAILY_DATA_BACKUP_HOUR", "16"))
DAILY_DATA_BACKUP_MINUTE = int(os.getenv("DAILY_DATA_BACKUP_MINUTE", "5"))
DAILY_DATA_BACKUP_DAYS = os.getenv("DAILY_DATA_BACKUP_DAYS", "mon-fri").strip()
DAILY_DATA_BACKUP_COLLECTION = os.getenv(
    "DAILY_DATA_BACKUP_COLLECTION", "daily_data_backup_state"
)
DAILY_DATA_BACKUP_CLEANUP_ENABLED = (
    os.getenv("DAILY_DATA_BACKUP_CLEANUP_ENABLED", "true").strip().lower() == "true"
)
MEGA_ENABLED = os.getenv("MEGA_ENABLED", "true").strip().lower() == "true"
MEGA_EMAIL = os.getenv("MEGA_EMAIL", "").strip()
MEGA_PASSWORD = os.getenv("MEGA_PASSWORD", "").strip()
MEGA_BACKUP_ROOT = os.getenv("MEGA_BACKUP_ROOT", "isolation_app").strip().strip("/")


# ---------------------------------------------------------------------------
# Live option-chain capture
# ---------------------------------------------------------------------------
OPTION_CHAIN_CAPTURE_ENABLED = (
    os.getenv("OPTION_CHAIN_CAPTURE_ENABLED", "true").strip().lower() == "true"
)

OPTION_CHAIN_INDEXES = [
    key.strip()
    for key in os.getenv("OPTION_CHAIN_INDEXES", "NSE_INDEX|Nifty 50").split(",")
    if key.strip()
]

# Pivot calculation is independent of live option-chain subscriptions.
# By default, calculate pivots for all four supported index option universes.
OPTION_PIVOT_INDEXES = [
    key.strip()
    for key in os.getenv(
        "OPTION_PIVOT_INDEXES",
        "NSE_INDEX|Nifty 50,NSE_INDEX|Nifty Bank,NSE_INDEX|NIFTY MID SELECT,BSE_INDEX|SENSEX",
    ).split(",")
    if key.strip()
]


# ---------------------------------------------------------------------------
# Live Supertrend GTT execution (disabled by default)
# ---------------------------------------------------------------------------
def _live_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}

def _live_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

def _live_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

LIVE_GTT_EXECUTION_ENABLED = _live_bool("LIVE_GTT_EXECUTION_ENABLED", "false")
LIVE_GTT_KILL_SWITCH = _live_bool("LIVE_GTT_KILL_SWITCH", "false")
# Each of the two independent GTTs is fixed to one lot for this strategy.
LIVE_GTT_LOTS_PER_ORDER = _live_int("LIVE_GTT_LOTS_PER_ORDER", 1)
DUMMY_ORDER_QUANTITY = _live_int("DUMMY_ORDER_QUANTITY", 2)  # legacy; not used by two-GTT execution
DUMMY_ORDER_STOP_LOSS_PERCENT = _live_float("DUMMY_ORDER_STOP_LOSS_PERCENT", 30.0)
DUMMY_TARGET_UPPER_THRESHOLD = _live_float("DUMMY_TARGET_UPPER_THRESHOLD", 200.0)
DUMMY_TARGET_LOWER_THRESHOLD = _live_float("DUMMY_TARGET_LOWER_THRESHOLD", 100.0)
DUMMY_TARGET_POINTS_ABOVE_UPPER = _live_float("DUMMY_TARGET_POINTS_ABOVE_UPPER", 60.0)
DUMMY_TARGET_POINTS_MIDDLE = _live_float("DUMMY_TARGET_POINTS_MIDDLE", 50.0)
DUMMY_TARGET_POINTS_BELOW_LOWER = _live_float("DUMMY_TARGET_POINTS_BELOW_LOWER", 40.0)

LIVE_GTT_ISOLATION_LOOKBACK_SECONDS = _live_int("LIVE_GTT_ISOLATION_LOOKBACK_SECONDS", 60)
LIVE_GTT_MAX_SIGNAL_AGE_SECONDS = _live_int("LIVE_GTT_MAX_SIGNAL_AGE_SECONDS", 120)
LIVE_GTT_MAX_FRAME_AGE_SECONDS = _live_int("LIVE_GTT_MAX_FRAME_AGE_SECONDS", 5)
LIVE_GTT_QUOTE_MAX_AGE_SECONDS = _live_int("LIVE_GTT_QUOTE_MAX_AGE_SECONDS", 10)

LIVE_GTT_EARLY_ENTRY_START = os.getenv("LIVE_GTT_EARLY_ENTRY_START", "09:15")
LIVE_GTT_EARLY_ENTRY_END = os.getenv("LIVE_GTT_EARLY_ENTRY_END", "09:39:59")
LIVE_GTT_EARLY_MAX_ENTRIES = _live_int("LIVE_GTT_EARLY_MAX_ENTRIES", 2)
LIVE_GTT_LATE_ENTRY_START = os.getenv("LIVE_GTT_LATE_ENTRY_START", "09:40")
LIVE_GTT_LATE_MAX_ENTRIES = _live_int("LIVE_GTT_LATE_MAX_ENTRIES", 1)
LIVE_GTT_LATE_ALLOW_REVERSAL = _live_bool("LIVE_GTT_LATE_ALLOW_REVERSAL", "false")

LIVE_GTT_MAX_NOTIONAL_PER_TRADE_INR = _live_float("LIVE_GTT_MAX_NOTIONAL_PER_TRADE_INR", 0.0)
LIVE_GTT_MAX_RISK_PER_TRADE_INR = _live_float("LIVE_GTT_MAX_RISK_PER_TRADE_INR", 0.0)
LIVE_GTT_REVERSAL_CLOSE_TIMEOUT_SECONDS = _live_int("LIVE_GTT_REVERSAL_CLOSE_TIMEOUT_SECONDS", 30)
LIVE_GTT_MONITOR_INTERVAL_SECONDS = _live_int("LIVE_GTT_MONITOR_INTERVAL_SECONDS", 3)

LIVE_GTT_AUDIT_COLLECTION = os.getenv("LIVE_GTT_AUDIT_COLLECTION", "live_gtt_execution_audit")
LIVE_GTT_STATE_COLLECTION = os.getenv("LIVE_GTT_STATE_COLLECTION", "live_gtt_execution_state")
LIVE_GTT_AUDIT_DIR = os.getenv("LIVE_GTT_AUDIT_DIR", "data/live_gtt_audit")
LIVE_GTT_STATE_DIR = os.getenv("LIVE_GTT_STATE_DIR", "data/live_gtt_state")
LIVE_GTT_AUDIT_MAX_EVENTS = _live_int("LIVE_GTT_AUDIT_MAX_EVENTS", 500)
