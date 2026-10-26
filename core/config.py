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

# Windows start confirmation (see token_tasks/telegram_token_bot.py).
# On Windows with an interactive console, the bot asks the operator to
# confirm before beginning Telegram polling (5 s timeout, default No).
# Headless deployments (Windows Service, scheduled task, no TTY) would
# never see the prompt and would therefore never start the bot — set
# this flag to true to bypass the prompt and start immediately.
# Non-Windows (Linux / macOS): the flag is ignored; the bot always
# starts as before.
TELEGRAM_BOT_SKIP_WINDOWS_PROMPT = _bool(
    "TELEGRAM_BOT_SKIP_WINDOWS_PROMPT", False
)
TELEGRAM_BOT_WINDOWS_PROMPT_TIMEOUT_SECONDS = _float(
    "TELEGRAM_BOT_WINDOWS_PROMPT_TIMEOUT_SECONDS", 5.0
)

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
    "services,core,token_tasks,upstox_services,static,tests",
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

# ===========================================================================
# ISOLATED SR TRACKER — watch SR confirmations for the isolated instrument
# ===========================================================================
# When an instrument is isolated, `isolated_sr_tracker_service` snapshots
# every SR level currently recorded by `live_sr_levels_service` as
# "pre-isolation" and then watches for NEW pivot confirmations on the
# same instrument for the rest of the day.  Each new confirmation is
# persisted (sidecar file, separate from the isolation state file) and
# can be surfaced on Telegram and the dashboard at /isolated.
#
# Sidecar file: <LIVE_DATA_DIR>/<LEVEL_ISOLATION_SR_FILENAME>
#
# Dedup is on (level_type, price, confirmed_time), so:
#   * re-seeding the SR engine does not double-count pivots;
#   * a level already snapshotted as pre-isolation is never re-added as
#     post-isolation;
#   * a genuinely new pivot (new candle) that happens to share a price
#     with an older one is still recorded.
#
# See:
#   services/isolated_sr_tracker_service.py
#   services/level_isolated_instrument_service.py  (_isolate / get_detail)
#   services/live_sr_levels_service.py             (_append_level hook)
#   routes/isolated_instrument_routes.py           (/sr-tracking endpoint)
#   templates/isolated_instrument.html             (SR Tracking card)
# ===========================================================================
LEVEL_ISOLATION_SR_FILENAME = _str(
    "LEVEL_ISOLATION_SR_FILENAME", "level_isolated_instrument_sr.json"
)

# When True (default), every new post-isolation SR confirmation fires a
# Telegram message.  When False, confirmations are still persisted and
# shown on the dashboard; only the Telegram send is suppressed.
LEVEL_ISOLATION_SR_TELEGRAM_ENABLED = _bool(
    "LEVEL_ISOLATION_SR_TELEGRAM_ENABLED", True
)

# ===========================================================================
# LIVE SR CALC — on-demand stateless SR computation
# ===========================================================================
# Feeds the /live-sr/calc endpoints.
#
#   services/live_sr_calc_service.py
#   routes/live_sr_calc_routes.py
#
# Unlike `live_sr_levels_service` (which maintains per-instrument state
# and persists to data/<...>/live_sr_levels.json), this calculator is
# STATELESS and always fetches fresh data:
#   1. Historical candles from data/<...>/candles.json
#   2. Today's intraday 1-min candles from Upstox HistoryV3Api
#
# Then runs the same strict-pivot scan as live_sr_levels_service on the
# merged series and returns the resulting levels.
#
# Every response is deterministic for the same underlying data, so
# callers can poll the endpoint safely — no shared state, no writes.
#
# Endpoints:
#   GET  /live-sr/calc?instrument_key=...          (or triple params)
#   GET  /live-sr/calc/{underlying}/{strike}/{type}
#   POST /live-sr/calc/batch
# ===========================================================================

# Default historical lookback window (days). 1..30, default 5.
LIVE_SR_CALC_DEFAULT_LOOKBACK_DAYS = max(1, min(30, _int(
    "LIVE_SR_CALC_DEFAULT_LOOKBACK_DAYS", 5
)))

# Hard cap on the number of merged candles used for pivot detection
# (default 3000). Protects against pathological lookback_days values
# and bounds the pivot scan time.
LIVE_SR_CALC_MAX_CANDLES = max(100, min(10000, _int(
    "LIVE_SR_CALC_MAX_CANDLES", 3000
)))

# ===========================================================================
# LIVE SR — accuracy tuning
# ===========================================================================
# Two settings that directly affect the accuracy of the persisted
# `live_sr_levels.json` (the file consumed by the isolation tracker and
# shown under `live_sr` on GET /live-sr/calc).
#
# See services/live_sr_levels_service.py module docstring for the full
# "accuracy overhaul" note.
# ===========================================================================

# When True (default), sr_backfill_service merges historical candles
# (data/<...>/candles.json) with today's intraday before seeding the
# live SR engine. Without this, pivots that need context from
# yesterday's tail (e.g. the 09:26 support on NIFTY 22400 CE) are
# missed.
SR_BACKFILL_INCLUDE_HISTORICAL = _bool("SR_BACKFILL_INCLUDE_HISTORICAL", True)

# Periodic re-seed of the live SR engine from the authoritative Upstox
# intraday API. Fixes the drift between tick-aggregated option candles
# and the true OHLC (break flags, freshness of very recent pivots).
#
# Set to False to disable the scheduler job entirely — the feed-start
# backfill still runs.
LIVE_SR_REFRESH_FROM_API_ENABLED = _bool(
    "LIVE_SR_REFRESH_FROM_API_ENABLED", True
)

# Refresh interval, in minutes. Default 2. Values 1..60 are supported.
# Higher values reduce Upstox API load (each refresh issues one
# intraday call per tracked instrument) at the cost of a larger
# accuracy lag on the freshest pivot / break.
LIVE_SR_REFRESH_FROM_API_MINUTES = max(
    1, min(60, _int("LIVE_SR_REFRESH_FROM_API_MINUTES", 2))
)

# ===========================================================================
# STRATEGY ENTRY SIGNAL — R3/S3 touch → buffered support → confirmed entry
# ===========================================================================
# Trigger chain:
#   1. NIFTY / SENSEX touches R3 or S3.  The touch time (T_touch) is
#      recorded, the bias side is known (R3 -> CE, S3 -> PE), and the
#      nearest filtered strike becomes the isolated instrument (this is
#      handled by LEVEL_ISOLATION_* above — nothing to configure here).
#   2. For each candidate option (bias side, plus counter side when
#      crossover is enabled), look for a SUPPORT pivot whose
#      `pivot_time` falls inside the asymmetric window:
#
#          [T_touch, T_touch + STRATEGY_ENTRY_BUFFER_MINUTES]
#
#      The window is strictly at-or-after the touch.  Pre-touch pivots
#      are excluded by default because the only pre-touch "support" a
#      freshly opened option can carry is the OR low, and using that
#      re-opens the same losing trade this filter is meant to prevent.
#      To include pre-touch pivots explicitly set
#      STRATEGY_ENTRY_INCLUDE_PRETOUCH=true.
#   3. Block the candidate if any RESISTANCE pivot was confirmed on or
#      before the entry support's confirmed_time, whose pivot_time is
#      strictly after the entry support's pivot_time, and whose price
#      is strictly above the entry support's price.  (Those are the
#      resistances actually visible on the chart at the moment of
#      entry.)
#   4. When the entry support's confirmed_time arrives, the candidate
#      becomes ARMED and the entry is signalled.  Entry price is the
#      candle-close at confirmed_time (fallback: live LTP), stop is the
#      entry support's price, target is the next resistance above
#      entry.
#   5. When crossover is enabled and BOTH sides arm on the same day,
#      whichever side's entry support confirmed EARLIEST wins; the
#      other is discarded.
#
# The signal is exposed as an informational block on `/isolated`
# (GET /admin/level-isolation/strategy-signal).  Order placement is
# NOT wired by this feature — that is a separate step.
# ===========================================================================
STRATEGY_ENTRY_ENABLED = _bool("STRATEGY_ENTRY_ENABLED", True)

# When True (default), evaluate CE and PE at the isolated strike
# (crossover).  When False, only the bias side (PE for S3, CE for R3)
# is considered — a day where the bias side has no valid setup becomes
# a no-trade day even if the counter side does.
STRATEGY_ENTRY_ALLOW_CROSSOVER = _bool(
    "STRATEGY_ENTRY_ALLOW_CROSSOVER", True
)

# Upper bound of the pivot_time window relative to the touch.  The
# window is strictly [T_touch, T_touch + BUFFER] — never symmetric.
# Default 10 covers the canonical 09:21 touch / 09:26 pivot case with
# slack for slightly-later swings.
STRATEGY_ENTRY_BUFFER_MINUTES = max(
    0, _int("STRATEGY_ENTRY_BUFFER_MINUTES", 10)
)

# When True, allow pivots whose pivot_time is BEFORE touch_time
# (within the same buffer).  Default False — the PE 09:15 support that
# would arm on an S3 touch only exists because it is the OR low;
# including it re-opens the 84% loss the filter prevents.  Leave False
# unless you have a specific reason.
STRATEGY_ENTRY_INCLUDE_PRETOUCH = _bool(
    "STRATEGY_ENTRY_INCLUDE_PRETOUCH", False
)

# Entry price source:
#   "market_at_confirmed" (default) — LTP / candle-close at confirmed_time
#   "pivot_price"                   — the entry support price itself
# The default matches the "place the order at confirmed_time" semantics
# that were agreed; "pivot_price" is retained only as a legacy escape
# hatch and will not fill on fast-moving options.
STRATEGY_ENTRY_PRICE_MODE = _str(
    "STRATEGY_ENTRY_PRICE_MODE", "market_at_confirmed"
).lower()

# When True (default), once a candidate side has ARMED for the day
# that side is not re-armed even if a new setup appears later.
# When False, a new confirmed support later in the day can re-arm
# (produces a new entry block; the frontend may show the latest).
STRATEGY_ENTRY_ONLY_ONCE_PER_DAY = _bool(
    "STRATEGY_ENTRY_ONLY_ONCE_PER_DAY", True
)

# When True (default), a resistance confirmed on or before the entry
# support's confirmed_time, with pivot_time strictly after the entry
# support's pivot_time and price strictly above the entry support's
# price, blocks the candidate.  When False, resistance never blocks.
STRATEGY_ENTRY_BLOCKED_BY_RESISTANCE = _bool(
    "STRATEGY_ENTRY_BLOCKED_BY_RESISTANCE", True
)

# When True (default), if both CE and PE arm on the same day, the one
# whose entry support confirmed EARLIEST wins; the other is discarded.
# When False, whichever side is iterated first is chosen — you almost
# certainly want True.
STRATEGY_ENTRY_FIRST_TO_CONFIRM_WINS = _bool(
    "STRATEGY_ENTRY_FIRST_TO_CONFIRM_WINS", True
)

# Soft warning threshold.  If (entry_LTP - entry_support_price) / entry_LTP
# exceeds this fraction, the entry block is flagged with a
# "stop_too_wide" warning.  Not a hard block — the entry still shows
# as ARMED — but makes it obvious that the stop is far away.  Set to
# 0 to disable.
STRATEGY_MAX_STOP_PCT = _float("STRATEGY_MAX_STOP_PCT", 0.35)

# ===========================================================================
# STRATEGY ENTRY PROMOTION — make the armed instrument the official pick
# ===========================================================================
# When the entry-signal ARMS on the COUNTER side (crossover winner), the
# bias-side instrument that the isolation engine picked at touch time is
# no longer the instrument we're actually trading.  When this flag is
# True (default), the counter-side instrument is "promoted" to be the
# **active** isolated instrument for the rest of the day:
#
#   * the SR tracker switches to it,
#   * the hero card and snapshot card display it,
#   * the entry-signal card shows "Promoted to ...".
#
# The isolation record itself is never modified — the original trigger
# (bias-side) instrument stays in the record and in the trigger_event
# for audit.  The promotion is stored in a separate sidecar file:
#
#   data/level_isolated_instrument_promotion.json
#
# Set to False to disable promotion entirely; the official instrument
# then stays fixed at the bias side (original behaviour).
STRATEGY_ENTRY_ALLOW_PROMOTION = _bool("STRATEGY_ENTRY_ALLOW_PROMOTION", True)

# When True, a second promotion may override the first (e.g. the PE
# arms later in the day after the CE was already promoted).  Default
# False — the first ARMED side wins and the pick is stable for the
# day.  Ignored when ALLOW_PROMOTION is False.
STRATEGY_ENTRY_ALLOW_RE_PROMOTION = _bool(
    "STRATEGY_ENTRY_ALLOW_RE_PROMOTION", False
)