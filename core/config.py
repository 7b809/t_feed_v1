"""Configuration for the foundational market-data application."""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()

def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

def _csv(name: str, default: str = "") -> list[str]:
    raw = _str(name, default)
    return [part.strip() for part in raw.split(",") if part.strip()]

# ---------------------------------------------------------------------------
# Token storage / lifecycle
# ---------------------------------------------------------------------------
MONGO_URI = _str("MONGO_URL") or _str("MONGO_URI")
MONGO_DB = _str("MONGO_DB")
TOKENS_COLLECTION = _str("TOKENS_COLLECTION")
UPSTOX_TOKEN_DOC_ID = _str("UPSTOX_TOKEN_DOC_ID", "upstox_access_token")
UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES = max(1, _int("UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES", 30))

MARKET_TIMEZONE = _str("MARKET_TIMEZONE", "Asia/Kolkata")
MARKET_TIME_FORMAT = _str("MARKET_TIME_FORMAT", "%Y-%m-%d %H:%M:%S %Z")

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
TELEGRAM_ENABLED = _bool("TELEGRAM_ENABLED", False)
TELEGRAM_BOT_TOKEN = _str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _str("TELEGRAM_CHAT_ID")
TELEGRAM_TIMEOUT_SECONDS = max(1, _int("TELEGRAM_TIMEOUT_SECONDS", 10))
TELEGRAM_TOKEN_BOT_ENABLED = _bool("TELEGRAM_TOKEN_BOT_ENABLED", True)
TELEGRAM_TOKEN_BOT_POLL_SECONDS = max(1, _int("TELEGRAM_TOKEN_BOT_POLL_SECONDS", 3))
TELEGRAM_TOKEN_BOT_LONG_POLL_TIMEOUT = max(1, _int("TELEGRAM_TOKEN_BOT_LONG_POLL_TIMEOUT", 20))
TELEGRAM_TOKEN_BOT_RESTRICT_TO_CHAT = _bool("TELEGRAM_TOKEN_BOT_RESTRICT_TO_CHAT", True)

# ---------------------------------------------------------------------------
# Chart history
# ---------------------------------------------------------------------------
CHART_MAX_BATCH_WORKERS = max(1, min(32, _int("CHART_MAX_BATCH_WORKERS", 8)))
CHART_LIVE_POLL_SECONDS = max(15, _int("CHART_LIVE_POLL_SECONDS", 30))

# ---------------------------------------------------------------------------
# Backup extraction (from candle-backup Mongo docs)
# ---------------------------------------------------------------------------
# Collections / doc ids written by the candle-backup project.
BACKUP_METADATA_COLLECTION = _str("BACKUP_METADATA_COLLECTION", "app_tokens")
BACKUP_METADATA_DOC_ID = _str("BACKUP_METADATA_DOC_ID", "backup_candles")
BACKUP_BLOB_COLLECTION = _str("BACKUP_BLOB_COLLECTION", "backup_collection")
BACKUP_BLOB_DOC_ID = _str("BACKUP_BLOB_DOC_ID", "backup_historical_candles")

# Where the extracted backup tree lives locally.
LIVE_DATA_DIR = PROJECT_ROOT / _str("LIVE_DATA_DIR", "data")
# Freshness window for re-extracting on startup (minutes). 0 = always.
BACKUP_MAX_AGE_MINUTES = max(0, _int("BACKUP_MAX_AGE_MINUTES", 0))

# ---------------------------------------------------------------------------
# Live feed / Upstox WebSocket
# ---------------------------------------------------------------------------
UPSTOX_WS_MODE = _str("UPSTOX_WS_MODE", "full")           # full | ltpc | full_d30 | option_greeks
UPSTOX_WS_AUTO_START = _bool("UPSTOX_WS_AUTO_START", True)
UPSTOX_WS_RECONNECT_INTERVAL = max(1, _int("UPSTOX_WS_RECONNECT_INTERVAL", 10))
UPSTOX_WS_MAX_RETRIES = max(0, _int("UPSTOX_WS_MAX_RETRIES", 0))  # 0 = infinite

# Health feed logging
HEALTH_FEED_LOG = _bool("HEALTH_FEED_LOG", True)
HEALTH_FEED_INSTRUMENT = _str("HEALTH_FEED_INSTRUMENT", "NSE_INDEX|Nifty 50")
# Interval in minutes (e.g. 5 = log every 5 minutes)
HEALTH_FEED_INSTRUMENT_INTERVAL = max(1, _int("HEALTH_FEED_INSTRUMENT_INTERVAL", 5))

# ---------------------------------------------------------------------------
# Live EMA cross engine
# ---------------------------------------------------------------------------
EMA_FAST_PERIOD = max(1, _int("EMA_FAST_PERIOD", 9))
EMA_SLOW_PERIOD = max(2, _int("EMA_SLOW_PERIOD", 21))
EMA_ATR_PERIOD = max(1, _int("EMA_ATR_PERIOD", 14))
EMA_VOLUME_LOOKBACK = max(2, _int("EMA_VOLUME_LOOKBACK", 20))
EMA_SLOPE_LOOKBACK = max(1, _int("EMA_SLOPE_LOOKBACK", 3))
EMA_SEP_ATR_MIN = _float("EMA_SEP_ATR_MIN", 0.15)
EMA_VOLUME_MULT = _float("EMA_VOLUME_MULT", 1.0)
EMA_DISTANCE_ATR_MAX = _float("EMA_DISTANCE_ATR_MAX", 1.5)
EMA_MIN_VALIDATION_SCORE = _int("EMA_MIN_VALIDATION_SCORE", 3)
EMA_ENABLE_VALIDATION = _bool("EMA_ENABLE_VALIDATION", True)

# Where live crosses for the current day are written.
LIVE_CROSSES_FILENAME = _str("LIVE_CROSSES_FILENAME", "live_crosses.json")

# ---------------------------------------------------------------------------
# Daily refresh scheduler
# ---------------------------------------------------------------------------
DAILY_REFRESH_ENABLED = _bool("DAILY_REFRESH_ENABLED", True)
DAILY_REFRESH_HOUR = _int("DAILY_REFRESH_HOUR", 9)
DAILY_REFRESH_MINUTE = _int("DAILY_REFRESH_MINUTE", 0)
# Mon..Fri by default (APScheduler day_of_week uses mon,tue,...)
DAILY_REFRESH_DAYS = _str("DAILY_REFRESH_DAYS", "mon,tue,wed,thu,fri")

# ---------------------------------------------------------------------------
# Transient folder cleanup (project start / daily refresh / hard refresh)
# ---------------------------------------------------------------------------
# Comma-separated list of folders (relative to the project root) that
# the cleanup service will remove. Reserved folders take priority — if
# a name appears in both lists, the reserved list wins and the folder
# is preserved.
#
# Default remove list is the transient state only. `logs` and
# `logs_text` are deliberately NOT in it: the logger holds open file
# handles into those directories at import time, and removing them
# mid-process breaks log rotation on Linux (file unlinked but still
# written) and fails outright on Windows. Add them to the remove list
# (via env) only if you understand that trade-off.
#
# The reserved list protects directories that would corrupt the app
# if removed: the meta backups, the code tree, the virtualenv, and
# the git metadata.
CLEANUP_REMOVE_FOLDERS = _csv(
    "CLEANUP_REMOVE_FOLDERS",
    "data,temp,downloads,output,__pycache__,.pytest_cache,htmlcov,test_logs",
)

CLEANUP_RESERVED_FOLDERS = _csv(
    "CLEANUP_RESERVED_FOLDERS",
    "meta_data,logs,logs_text,.git,.venv,venv,myenv,templates,routes,"
    "services,core,token_tasks,static,tests",
)

# ---------------------------------------------------------------------------
# Opening Range (OR) levels
# ---------------------------------------------------------------------------
# File written into each instrument's folder for the current day's OR levels.
OPENING_RANGE_FILENAME = _str("OPENING_RANGE_FILENAME", "opening_range.json")

# The first 1-minute candle of the day starts at this time (HH:MM in the
# market timezone). The candle finalizes one minute later.
OPENING_RANGE_FIRST_MINUTE = _str("OPENING_RANGE_FIRST_MINUTE", "09:15")

# Trigger time for the scheduled OR calculation (HH:MM:SS in the market
# timezone). Defaults to 09:16:10 — a 10-second buffer past the close of
# the first 1-minute candle.
OPENING_RANGE_HOUR = _int("OPENING_RANGE_HOUR", 9)
OPENING_RANGE_MINUTE = _int("OPENING_RANGE_MINUTE", 16)
OPENING_RANGE_SECOND = _int("OPENING_RANGE_SECOND", 10)

# ---------------------------------------------------------------------------
# Strategy selection (OR-based strike selection per index)
# ---------------------------------------------------------------------------
# When enabled, the unified refresh (startup / daily / hard) and the
# scheduled task at 09:16:10 IST build a per-index strike list from the
# Opening Range midpoint and materialise it under:
#
#   data/strategy/<ticker>/<index_name>/<strike>_<CE|PE>/
#       strategy_meta.json
#       opening_range.json
#       candles.json
#       ema_crosses.json
#       live_crosses.json
#
STRATEGY_ENABLED = _bool("STRATEGY_ENABLED", True)

# Root directory for generated strategy folders (relative to project root).
STRATEGY_BASE_DIR = PROJECT_ROOT / _str("STRATEGY_BASE_DIR", "data/strategy")

# Buffer mode controls how far the strike window extends around the
# Opening Range midpoint (the ATM reference).
#
#   "typical"                -> ATM ± typical_buffer_points
#   "typical_plus_minus_avg" -> ATM ± (typical_buffer_points ± avg_buffer)
#                               where avg_buffer is the mean of all
#                               typical_buffer_points values.
#
# "typical" is the safe default and matches the example descriptions.
STRATEGY_BUFFER_MODE = _str("STRATEGY_BUFFER_MODE", "typical")

# Default index list. Override via STRATEGY_INDEXES_JSON (a JSON array).
# Each entry must have at least:
#     index_name, ticker, instrument_key, strike_step, typical_buffer_points
# Optional:
#     denominator_points, description
_DEFAULT_STRATEGY_INDEXES: list[dict] = [
    {
        "index_name": "NIFTY 50",
        "ticker": "NIFTY",
        "instrument_key": "NSE_INDEX|Nifty 50",
        "strike_step": 50,
        "typical_buffer_points": 300,
        "denominator_points": 300,
        "description": (
            "Standard range buffer (~300 to 500 points based on normal "
            "daily volatility)"
        ),
    },
    {
        "index_name": "BSE SENSEX",
        "ticker": "SENSEX",
        "instrument_key": "BSE_INDEX|SENSEX",
        "strike_step": 100,
        "typical_buffer_points": 900,
        "denominator_points": 1000,
        "description": (
            "Higher absolute index scale compared to Nifty 50, requiring "
            "a proportional 800-1200 point buffer"
        ),
    },
]

def _load_strategy_indexes() -> list[dict]:
    raw = _str("STRATEGY_INDEXES_JSON")
    if raw:
        try:
            import json as _json
            data = _json.loads(raw)
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass
    return list(_DEFAULT_STRATEGY_INDEXES)

STRATEGY_INDEXES: list[dict] = _load_strategy_indexes()

# Best-effort: try to resolve each selected strike to a real Upstox
# instrument key via the Upstox search API, and fetch historical
# candles for it. Set to False to skip network calls entirely and
# only materialise the folder structure.
STRATEGY_FETCH_HISTORY = _bool("STRATEGY_FETCH_HISTORY", True)

# How many days of historical 1-min candles to fetch per resolved strike.
STRATEGY_HISTORY_DAYS = max(1, _int("STRATEGY_HISTORY_DAYS", 30))

# Max parallel workers used while resolving / fetching candles.

STRATEGY_MAX_WORKERS = max(1, min(16, _int("STRATEGY_MAX_WORKERS", 6)))

# Allow /live/* routes to make a best-effort Upstox search when a
# triple lookup misses the registry. Set to false to force strict
# registry-only lookups.
LIVE_RESOLVE_ON_MISS = _bool("LIVE_RESOLVE_ON_MISS", True)

# ---------------------------------------------------------------------------
# Strategy — stage 1 (buffer filter) + stage 2 (EMA isolation)
# ---------------------------------------------------------------------------
STRATEGY_ENABLED = _bool("STRATEGY_ENABLED", True)

# Root for the full strategy tree. Two subfolders are created:
#   <STRATEGY_BASE_DIR>/stage_1_initial    buffer-filtered strikes
#   <STRATEGY_BASE_DIR>/stage_2_isolation  EMA-cross-isolated strikes
STRATEGY_BASE_DIR = PROJECT_ROOT / _str("STRATEGY_BASE_DIR", "data/strategy")
STRATEGY_STAGE_1_NAME = _str("STRATEGY_STAGE_1_NAME", "stage_1_initial")
STRATEGY_STAGE_2_NAME = _str("STRATEGY_STAGE_2_NAME", "stage_2_isolation")

# Buffer mode for the strike window (unchanged).
STRATEGY_BUFFER_MODE = _str("STRATEGY_BUFFER_MODE", "typical")

# Default index list — same shape as before.
STRATEGY_INDEXES: list[dict] = _load_strategy_indexes()   # keep the loader from before

# Best-effort Upstox enrichment (history fetch).
STRATEGY_FETCH_HISTORY = _bool("STRATEGY_FETCH_HISTORY", True)
STRATEGY_HISTORY_DAYS = max(1, _int("STRATEGY_HISTORY_DAYS", 30))
STRATEGY_MAX_WORKERS = max(1, min(16, _int("STRATEGY_MAX_WORKERS", 6)))

# --- Stage 2 isolation knobs ---------------------------------------------
# When enabled, the isolation service reads each stage-1 strike folder,
# computes last-day-last-cross and today's-first-candle cross, applies
# the bullish-priority rules, and copies the selected strikes into
# stage_2_isolation.
STRATEGY_ISOLATION_ENABLED = _bool("STRATEGY_ISOLATION_ENABLED", True)

# Lookback window (in minutes) at the tail of the last trading day.
# Crosses that occurred inside this window are candidates for "last
# day's last cross". Outside the window is ignored.
STRATEGY_ISOLATION_LOOKBACK_MINUTES = max(
    1, _int("STRATEGY_ISOLATION_LOOKBACK_MINUTES", 30)
)

# If a strike's Upstox key could not be resolved in stage 1, stage 2
# cannot fetch today's intraday. When True, those strikes are skipped
# with a clear status instead of being treated as a failure.
STRATEGY_ISOLATION_SKIP_UNRESOLVED = _bool(
    "STRATEGY_ISOLATION_SKIP_UNRESOLVED", True
)