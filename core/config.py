# core/config.py
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
# Application port
# ---------------------------------------------------------------------------
# Used by services/readiness_service.py's smoke-test probes
# (http://127.0.0.1:<PORT>/...). MUST match the port the app is actually
# listening on.
#
#   run.bat / start.sh      -> 8010  (default)
#   Procfile                -> 8000  (override PORT=8000)
#   manual uvicorn          -> whatever --port is passed
#
# If this value is wrong the smoke test will fail with a
# ConnectionRefused and the readiness digest will report YELLOW. That
# is intentional: a health probe that cannot reach the app is itself
# a real (if minor) degradation.
PORT = _int("PORT", 8010)

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
BACKUP_METADATA_COLLECTION = _str("BACKUP_METADATA_COLLECTION", "app_tokens")
BACKUP_METADATA_DOC_ID = _str("BACKUP_METADATA_DOC_ID", "backup_candles")
BACKUP_BLOB_COLLECTION = _str("BACKUP_BLOB_COLLECTION", "backup_collection")
BACKUP_BLOB_DOC_ID = _str("BACKUP_BLOB_DOC_ID", "backup_historical_candles")

LIVE_DATA_DIR = PROJECT_ROOT / _str("LIVE_DATA_DIR", "data")
BACKUP_MAX_AGE_MINUTES = max(0, _int("BACKUP_MAX_AGE_MINUTES", 0))

# ---------------------------------------------------------------------------
# Live feed / Upstox WebSocket
# ---------------------------------------------------------------------------
UPSTOX_WS_MODE = _str("UPSTOX_WS_MODE", "full")
UPSTOX_WS_AUTO_START = _bool("UPSTOX_WS_AUTO_START", True)
UPSTOX_WS_RECONNECT_INTERVAL = max(1, _int("UPSTOX_WS_RECONNECT_INTERVAL", 10))
UPSTOX_WS_MAX_RETRIES = max(0, _int("UPSTOX_WS_MAX_RETRIES", 0))

HEALTH_FEED_LOG = _bool("HEALTH_FEED_LOG", True)
HEALTH_FEED_INSTRUMENT = _str("HEALTH_FEED_INSTRUMENT", "NSE_INDEX|Nifty 50")
HEALTH_FEED_INSTRUMENT_INTERVAL = max(1, _int("HEALTH_FEED_INSTRUMENT_INTERVAL", 5))

# ---------------------------------------------------------------------------
# Live EMA cross engine (retained for compatibility)
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

LIVE_CROSSES_FILENAME = _str("LIVE_CROSSES_FILENAME", "live_crosses.json")

# ---------------------------------------------------------------------------
# Daily refresh scheduler
# ---------------------------------------------------------------------------
DAILY_REFRESH_ENABLED = _bool("DAILY_REFRESH_ENABLED", True)
DAILY_REFRESH_HOUR = _int("DAILY_REFRESH_HOUR", 8)
DAILY_REFRESH_MINUTE = _int("DAILY_REFRESH_MINUTE", 30)
DAILY_REFRESH_DAYS = _str("DAILY_REFRESH_DAYS", "mon,tue,wed,thu,fri")

# ---------------------------------------------------------------------------
# Readiness / pre-flight
# ---------------------------------------------------------------------------
READINESS_DIGEST_ENABLED = _bool("READINESS_DIGEST_ENABLED", True)
READINESS_DIGEST_HOUR = _int("READINESS_DIGEST_HOUR", 8)
READINESS_DIGEST_MINUTE = _int("READINESS_DIGEST_MINUTE", 45)

READINESS_PREFLIGHT_ENABLED = _bool("READINESS_PREFLIGHT_ENABLED", True)
READINESS_PREFLIGHT_HOUR = _int("READINESS_PREFLIGHT_HOUR", 9)
READINESS_PREFLIGHT_MINUTE = _int("READINESS_PREFLIGHT_MINUTE", 5)

READINESS_CLOCK_DRIFT_MAX_SECONDS = _float("READINESS_CLOCK_DRIFT_MAX_SECONDS", 30.0)
READINESS_DISK_FREE_MIN_GB = _float("READINESS_DISK_FREE_MIN_GB", 1.0)
READINESS_REGISTRY_MIN_COUNT = _int("READINESS_REGISTRY_MIN_COUNT", 100)
READINESS_TOKEN_MAX_AGE_MINUTES = _float("READINESS_TOKEN_MAX_AGE_MINUTES", 120.0)
READINESS_SMOKE_TEST_ENABLED = _bool("READINESS_SMOKE_TEST_ENABLED", True)

# ---------------------------------------------------------------------------
# Pipeline logging verbosity
# ---------------------------------------------------------------------------
VERBOSE_LOG = _bool("VERBOSE_LOG", False)

# ---------------------------------------------------------------------------
# Transient folder cleanup
# ---------------------------------------------------------------------------
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
# /live/* route behaviour
# ---------------------------------------------------------------------------
LIVE_RESOLVE_ON_MISS = _bool("LIVE_RESOLVE_ON_MISS", True)

# ===========================================================================
# DAILY BACKUP — zip configured folders, upload to MEGA, track state in Mongo
# ===========================================================================
DAILY_BACKUP_ENABLED = _bool("DAILY_BACKUP_ENABLED", True)
DAILY_BACKUP_HOUR = _int("DAILY_BACKUP_HOUR", 16)
DAILY_BACKUP_MINUTE = _int("DAILY_BACKUP_MINUTE", 0)
DAILY_BACKUP_DAYS = _str("DAILY_BACKUP_DAYS", "mon,tue,wed,thu,fri,sat,sun")
DAILY_BACKUP_FOLDERS = _csv("DAILY_BACKUP_FOLDERS", "data")
DAILY_BACKUP_SOURCE_LABEL = _str(
    "DAILY_BACKUP_SOURCE_LABEL", PROJECT_ROOT.name
)

MEGA_ENABLED = _bool("MEGA_ENABLED", True)
MEGA_EMAIL = _str("MEGA_EMAIL")
MEGA_PASSWORD = _str("MEGA_PASSWORD")
MEGA_BACKUP_ROOT = _str("MEGA_BACKUP_ROOT", "market_engine_backup")

DAILY_BACKUP_MONGO_DB = _str("DAILY_BACKUP_MONGO_DB", "UPSTOX_APP_V2")
DAILY_BACKUP_STATE_COLLECTION = _str(
    "DAILY_BACKUP_STATE_COLLECTION", "daily_backup_state"
)
DAILY_BACKUP_STATE_DAYS = max(1, _int("DAILY_BACKUP_STATE_DAYS", 7))
DAILY_BACKUP_MAX_BLOB_MB = max(1, _int("DAILY_BACKUP_MAX_BLOB_MB", 15))

# ===========================================================================
# First-candle OR + EMA-cross detection (09:16:10 Mon-Fri)
# ===========================================================================
FIRST_CANDLE_ENABLED = _bool("FIRST_CANDLE_ENABLED", True)
FIRST_CANDLE_HOUR = _int("FIRST_CANDLE_HOUR", 9)
FIRST_CANDLE_MINUTE = _int("FIRST_CANDLE_MINUTE", 16)
FIRST_CANDLE_SECOND = _int("FIRST_CANDLE_SECOND", 10)
FIRST_CANDLE_WORKERS = max(1, min(32, _int("FIRST_CANDLE_WORKERS", 8)))

# ===========================================================================
# Level-instrument filter (per-index CE/PE strike list from OR levels)
# ===========================================================================
LEVEL_FILTER_ENABLED = _bool("LEVEL_FILTER_ENABLED", True)

LEVEL_FILTER_INDEXES = _csv("LEVEL_FILTER_INDEXES", "NIFTY,SENSEX")

LEVEL_FILTER_STRIKE_STEP_NIFTY = max(1, _int("LEVEL_FILTER_STRIKE_STEP_NIFTY", 50))
LEVEL_FILTER_STRIKE_STEP_SENSEX = max(1, _int("LEVEL_FILTER_STRIKE_STEP_SENSEX", 100))

LEVEL_FILTER_STRIKE_STEPS_JSON = _str("LEVEL_FILTER_STRIKE_STEPS_JSON", "")

LEVEL_FILTER_RANGE_MODE = _str("LEVEL_FILTER_RANGE_MODE", "wide").lower()

LEVEL_FILTER_INCLUDE_ATM_PLUS_MINUS = max(
    0, _int("LEVEL_FILTER_INCLUDE_ATM_PLUS_MINUS", 0)
)

# When True (default), the filter output also registers the index
# instrument itself with the live SR engine. The existing
# services/level_instrument_filter_service.py reads this flag; it was
# missing from this file in the earlier revision.
LEVEL_FILTER_INCLUDE_INDEX_SR = _bool("LEVEL_FILTER_INCLUDE_INDEX_SR", True)

# ===========================================================================
# Live SR levels engine (pivot-based, runs on live 1-min candles)
# ===========================================================================
LIVE_SR_ENABLED = _bool("LIVE_SR_ENABLED", True)
LIVE_SR_TRACK_ALL_INSTRUMENTS = _bool("LIVE_SR_TRACK_ALL_INSTRUMENTS", True)
LIVE_SR_LEFT_BARS = max(1, _int("LIVE_SR_LEFT_BARS", 15))
LIVE_SR_RIGHT_BARS = max(1, _int("LIVE_SR_RIGHT_BARS", 15))
LIVE_SR_MAX_CANDLES = max(50, _int("LIVE_SR_MAX_CANDLES", 500))
LIVE_SR_FILENAME = _str("LIVE_SR_FILENAME", "live_sr_levels.json")
LIVE_SR_PERSIST_EVERY_N_CANDLES = max(0, _int("LIVE_SR_PERSIST_EVERY_N_CANDLES", 5))

# ===========================================================================
# Mid-day startup backfill via intraday API
# ===========================================================================
SR_BACKFILL_ENABLED = _bool("SR_BACKFILL_ENABLED", True)
SR_BACKFILL_MAX_WORKERS = max(1, min(16, _int("SR_BACKFILL_MAX_WORKERS", 4)))
SR_BACKFILL_START_HOUR = _int("SR_BACKFILL_START_HOUR", 9)
SR_BACKFILL_START_MINUTE = _int("SR_BACKFILL_START_MINUTE", 15)
SR_BACKFILL_END_HOUR = _int("SR_BACKFILL_END_HOUR", 23)
SR_BACKFILL_END_MINUTE = _int("SR_BACKFILL_END_MINUTE", 59)

# ===========================================================================
# LEVEL ISOLATION — pick one filtered CE/PE instrument on R3 / S3 touch
# ===========================================================================
# After first_candle produces or_levels and level_filter writes
# level_instruments.json, the isolation engine watches NIFTY / SENSEX
# LTP for R3 / S3 touches and picks ONE filtered instrument per day
# using the following priority (one-shot per side):
#
#     NIFTY-R3 -> NIFTY-S3 -> SENSEX-R3 -> SENSEX-S3
#
# * NIFTY / SENSEX pick: nearest filtered strike to the index LTP.
# * SENSEX-only touch  : reads the NIFTY LTP *at that moment* and picks
#                        the nearest NIFTY filtered instrument.
# * Stale NIFTY tick   : if older than LEVEL_ISOLATION_NIFTY_TICK_STALE_SECONDS
#                        the engine falls back to SENSEX's own nearest.
#
# Persisted to data/<LEVEL_ISOLATION_FILENAME>; a full event log for
# the day is preserved in the same file under "events".
# ===========================================================================
LEVEL_ISOLATION_ENABLED = _bool("LEVEL_ISOLATION_ENABLED", True)

# Output filename under data/ (resolved against LIVE_DATA_DIR).
LEVEL_ISOLATION_FILENAME = _str(
    "LEVEL_ISOLATION_FILENAME", "level_isolated_instrument.json"
)

# Throttle for the on-tick touch check (per NIFTY / SENSEX tick event).
# The check runs at most once every N seconds regardless of tick rate.
LEVEL_ISOLATION_CHECK_INTERVAL_SECONDS = max(
    0.5, _float("LEVEL_ISOLATION_CHECK_INTERVAL_SECONDS", 2.0)
)

# Max age (seconds) of the last NIFTY tick before it is treated as
# stale for the "SENSEX-touch -> check current NIFTY LTP" branch. On a
# stale tick, the engine falls back to the SENSEX-side nearest pick.
LEVEL_ISOLATION_NIFTY_TICK_STALE_SECONDS = max(
    1, _int("LEVEL_ISOLATION_NIFTY_TICK_STALE_SECONDS", 30)
)

# Startup backfill window (intraday-candle replay). Defaults mirror the
# SR backfill window: 09:16 -> 23:59 IST. Any startup between these
# times on a trading day reconstructs the "would-have-isolated"
# instrument by replaying today's 1-min intraday candles.
LEVEL_ISOLATION_BACKFILL_WINDOW_START_HOUR = _int(
    "LEVEL_ISOLATION_BACKFILL_WINDOW_START_HOUR", 9
)
LEVEL_ISOLATION_BACKFILL_WINDOW_START_MINUTE = _int(
    "LEVEL_ISOLATION_BACKFILL_WINDOW_START_MINUTE", 16
)
LEVEL_ISOLATION_BACKFILL_WINDOW_END_HOUR = _int(
    "LEVEL_ISOLATION_BACKFILL_WINDOW_END_HOUR", 23
)
LEVEL_ISOLATION_BACKFILL_WINDOW_END_MINUTE = _int(
    "LEVEL_ISOLATION_BACKFILL_WINDOW_END_MINUTE", 59
)

LEVEL_ISOLATION_TELEGRAM_ENABLED = _bool("LEVEL_ISOLATION_TELEGRAM_ENABLED", True)