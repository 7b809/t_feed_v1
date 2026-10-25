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
# Live EMA cross engine (retained for compatibility until
# live_ema_engine.py / live_cross_store.py are fully removed)
# ---------------------------------------------------------------------------
# These keys are no longer consumed by the live feed (the on_tick call
# was removed) and the strategy cross handler is being stripped from
# admin_routes / live_routes. They remain here so any lingering import
# of the EMA engine / cross store does not raise AttributeError.
#
# Delete this whole block once services/live_ema_engine.py and
# services/live_cross_store.py are deleted.
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
# 08:30 IST — the industry-standard "pre-open warm-up window". Chosen
# deliberately: 30 minutes before the 09:00 premarket and 45 minutes
# before the 09:15 cash open, which is enough time to catch an error,
# fix it, and still be ready for the open.
#
# The readiness pipeline is built around this timing:
#     08:30  daily refresh runs
#     08:45  readiness digest (GREEN/YELLOW/RED + per-check)
#     09:05  pre-flight recheck (alert on any regression)
#     09:15  cash open
DAILY_REFRESH_ENABLED = _bool("DAILY_REFRESH_ENABLED", True)
DAILY_REFRESH_HOUR = _int("DAILY_REFRESH_HOUR", 8)
DAILY_REFRESH_MINUTE = _int("DAILY_REFRESH_MINUTE", 30)
# Mon..Fri by default (APScheduler day_of_week uses mon,tue,...)
DAILY_REFRESH_DAYS = _str("DAILY_REFRESH_DAYS", "mon,tue,wed,thu,fri")

# ---------------------------------------------------------------------------
# Readiness / pre-flight
# ---------------------------------------------------------------------------
# The readiness pipeline produces ONE aggregate GREEN/YELLOW/RED status
# from ~10 independent subsystem checks (token, registry, Mongo, live
# feed, scheduler, clock drift, disk, order toggle, ...) and surfaces
# it on Telegram at 08:45 and re-verifies at 09:05.
#
#   READINESS_DIGEST_*    08:45 Telegram digest (short, glanceable)
#   READINESS_PREFLIGHT_* 09:05 re-check + intervention alert
#
# The config validator (services/config_validation.py) enforces that
# digest < preflight, so a misconfiguration here fails loudly at
# startup rather than silently producing no alerts.
READINESS_DIGEST_ENABLED = _bool("READINESS_DIGEST_ENABLED", True)
READINESS_DIGEST_HOUR = _int("READINESS_DIGEST_HOUR", 8)
READINESS_DIGEST_MINUTE = _int("READINESS_DIGEST_MINUTE", 45)

READINESS_PREFLIGHT_ENABLED = _bool("READINESS_PREFLIGHT_ENABLED", True)
READINESS_PREFLIGHT_HOUR = _int("READINESS_PREFLIGHT_HOUR", 9)
READINESS_PREFLIGHT_MINUTE = _int("READINESS_PREFLIGHT_MINUTE", 5)

# Thresholds for the individual checks. Any check that returns "fail"
# turns the aggregate status RED; any "warn" turns it YELLOW; all-ok
# is GREEN. These are tuned for a single-VPS deployment — bump the
# disk threshold on a large-data host.
READINESS_CLOCK_DRIFT_MAX_SECONDS = _float("READINESS_CLOCK_DRIFT_MAX_SECONDS", 30.0)
READINESS_DISK_FREE_MIN_GB = _float("READINESS_DISK_FREE_MIN_GB", 1.0)
READINESS_REGISTRY_MIN_COUNT = _int("READINESS_REGISTRY_MIN_COUNT", 100)
READINESS_TOKEN_MAX_AGE_MINUTES = _float("READINESS_TOKEN_MAX_AGE_MINUTES", 120.0)
# When False, the readiness digest skips the active HTTP probes
# (GET /live/snapshot, GET /live/instruments, GET /api/upstox/token).
# Set to False only if the app is intentionally not listening on
# 127.0.0.1:<PORT> (e.g. a container where the probe would need a
# service-name hostname instead of loopback).
READINESS_SMOKE_TEST_ENABLED = _bool("READINESS_SMOKE_TEST_ENABLED", True)

# ---------------------------------------------------------------------------
# Pipeline logging verbosity
# ---------------------------------------------------------------------------
# When False (default), pipeline modules log ONLY lifecycle lines
# (START / COMPLETED / FAILED, per-index one-liners) and never dump
# the full per-instrument summary dicts. Set to True when debugging
# to see every per-instrument decision, candle count, and window
# extreme.
#
# Consumer: routes/admin_routes.py, services/scheduler_service.py,
# and the token_tasks/*.py scheduled wrappers. All of them read this
# flag at runtime; toggling it in .env and restarting is enough — no
# code change is required.
VERBOSE_LOG = _bool("VERBOSE_LOG", False)

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
# /live/* route behaviour
# ---------------------------------------------------------------------------
# Allow /live/* routes to make a best-effort Upstox search when a
# triple lookup misses the registry. Set to false to force strict
# registry-only lookups.
LIVE_RESOLVE_ON_MISS = _bool("LIVE_RESOLVE_ON_MISS", True)

# ===========================================================================
# DAILY BACKUP — zip configured folders, upload to MEGA, track state in Mongo
# ===========================================================================
# Runs at DAILY_BACKUP_HOUR:DAILY_BACKUP_MINUTE every day via the shared
# APScheduler. For each folder in DAILY_BACKUP_FOLDERS, a zip is built,
# uploaded to MEGA under:
#
#     <MEGA_BACKUP_ROOT>/<folder>/<folder>_<YYYY-MM-DD>.zip
#
# and a progress-state document is upserted into
# <DAILY_BACKUP_MONGO_DB>.<DAILY_BACKUP_STATE_COLLECTION> keyed by the
# date, so the last N days can be reviewed from
# GET /admin/daily-backup-status.
#
# NOTE: the archive is no longer stored in MongoDB. The collection
#       BACKUP_BLOB_COLLECTION remains reserved for the candle-backup
#       extract that is re-consumed on every refresh.
DAILY_BACKUP_ENABLED = _bool("DAILY_BACKUP_ENABLED", True)

# Scheduler trigger time (market timezone).
DAILY_BACKUP_HOUR = _int("DAILY_BACKUP_HOUR", 16)
DAILY_BACKUP_MINUTE = _int("DAILY_BACKUP_MINUTE", 0)

# All days by default. Set to "mon,tue,wed,thu,fri" to skip weekends.
DAILY_BACKUP_DAYS = _str("DAILY_BACKUP_DAYS", "mon,tue,wed,thu,fri,sat,sun")

# Folders (relative to the project root) to zip on each run. Each
# folder becomes its own zip; they are uploaded to separate MEGA
# subfolders under MEGA_BACKUP_ROOT.
DAILY_BACKUP_FOLDERS = _csv("DAILY_BACKUP_FOLDERS", "data")

# Value written to the `folder` field in the state document. Defaults
# to the project directory name (e.g. "market-engine-v1").
DAILY_BACKUP_SOURCE_LABEL = _str(
    "DAILY_BACKUP_SOURCE_LABEL", PROJECT_ROOT.name
)

# ===========================================================================
# MEGA cloud upload (destination for daily zips)
# ===========================================================================
# Credentials for the MEGA account that receives the daily zips.
# ⚠️  SECRETS — keep out of Git.
MEGA_ENABLED = _bool("MEGA_ENABLED", True)
MEGA_EMAIL = _str("MEGA_EMAIL")
MEGA_PASSWORD = _str("MEGA_PASSWORD")

# Remote folder layout under the MEGA root:
#     <MEGA_BACKUP_ROOT>/<folder>/<folder>_<YYYY-MM-DD>.zip
# e.g. market_engine_backup/data/data_2026-10-20.zip
MEGA_BACKUP_ROOT = _str("MEGA_BACKUP_ROOT", "market_engine_backup")

# ===========================================================================
# MongoDB progress-state tracking for the daily backup
# ===========================================================================
# NOT the archive — the archive lives on MEGA. This collection holds
# ONE document per date, keyed by _id = "YYYY-MM-DD", tracking whether
# each folder's zip was uploaded, its size, MEGA link, and error (if
# any). /admin/daily-backup-status returns the last
# DAILY_BACKUP_STATE_DAYS date documents, newest first.
DAILY_BACKUP_MONGO_DB = _str("DAILY_BACKUP_MONGO_DB", "UPSTOX_APP_V2")
DAILY_BACKUP_STATE_COLLECTION = _str(
    "DAILY_BACKUP_STATE_COLLECTION", "daily_backup_state"
)
DAILY_BACKUP_STATE_DAYS = max(1, _int("DAILY_BACKUP_STATE_DAYS", 7))

# DEPRECATED — kept only so an existing .env that still sets
# DAILY_BACKUP_MAX_BLOB_MB does not crash the loader. Ignored by the
# MEGA-based implementation, which has no inline size cap.
DAILY_BACKUP_MAX_BLOB_MB = max(1, _int("DAILY_BACKUP_MAX_BLOB_MB", 15))


# ===========================================================================
# First-candle OR + EMA-cross detection (09:16:10 Mon-Fri)
# ===========================================================================
# Runs after the first 1-min candle of the day closes (09:15-09:16).
# Fetches the first candle for every registered instrument in parallel,
# computes Opening Range levels using the D_H/D_L formula, checks the
# last recorded EMA cross against the new close, and writes
# first_candle.json next to candles.json in each instrument folder.
FIRST_CANDLE_ENABLED = _bool("FIRST_CANDLE_ENABLED", True)
FIRST_CANDLE_HOUR = _int("FIRST_CANDLE_HOUR", 9)
FIRST_CANDLE_MINUTE = _int("FIRST_CANDLE_MINUTE", 16)
FIRST_CANDLE_SECOND = _int("FIRST_CANDLE_SECOND", 10)
FIRST_CANDLE_WORKERS = max(1, min(32, _int("FIRST_CANDLE_WORKERS", 8)))


# ===========================================================================
# Level-instrument filter (per-index CE/PE strike list from OR levels)
# ===========================================================================
# Given an index's first-candle OR levels, filter the option chain down to
# the strikes that correspond to R1/R2/R3/R3_Threshold (CE) and
# S1/S2/S3/S3_Threshold (PE), then apply the S3..R3 range filter.
#
# Output (per index):
#   data/index/<underlying_lower>/level_instruments.json
#
# This file is a UI/analysis artefact — the CE/PE strikes the index's OR
# levels map to, with reasons and level tags. It is independent of the
# live SR engine (which now tracks every subscribed instrument, not just
# these filtered strikes).
LEVEL_FILTER_ENABLED = _bool("LEVEL_FILTER_ENABLED", True)

# Which index underlyings to filter. "NIFTY" and "SENSEX" are the defaults.
LEVEL_FILTER_INDEXES = _csv("LEVEL_FILTER_INDEXES", "NIFTY,SENSEX")

# Default strike step per index (can be overridden by JSON below).
LEVEL_FILTER_STRIKE_STEP_NIFTY = max(1, _int("LEVEL_FILTER_STRIKE_STEP_NIFTY", 50))
LEVEL_FILTER_STRIKE_STEP_SENSEX = max(1, _int("LEVEL_FILTER_STRIKE_STEP_SENSEX", 100))

# Optional JSON override: {"NIFTY": 50, "SENSEX": 100, "BANKNIFTY": 100}
# Parsed as JSON; invalid JSON falls back to the two env scalars above.
LEVEL_FILTER_STRIKE_STEPS_JSON = _str("LEVEL_FILTER_STRIKE_STEPS_JSON", "")

# Range filter mode:
#   "wide"   -> S3 <= strike <= R3          (default)
#   "strict" -> S3_Threshold <= strike <= R3_Threshold
LEVEL_FILTER_RANGE_MODE = _str("LEVEL_FILTER_RANGE_MODE", "wide").lower()

# When > 0, also include ATM +/- N strikes in the filter output.
LEVEL_FILTER_INCLUDE_ATM_PLUS_MINUS = max(
    0, _int("LEVEL_FILTER_INCLUDE_ATM_PLUS_MINUS", 0)
)

# ===========================================================================
# Live SR levels engine (pivot-based, runs on live 1-min candles)
# ===========================================================================
# For every tracked instrument, the engine accumulates 1-min candles
# from live ticks and detects resistance (swing high) / support (swing
# low) pivots using the strict left/right rule:
#
#     candle[i].high > candle[j].high  for j in [i-left, i-1] and [i+1, i+right]
#     candle[i].low  < candle[j].low   for j in [i-left, i-1] and [i+1, i+right]
#
# A pivot at index i is confirmed at index i + right_bars. Confirmed pivots
# are written to data/<...>/live_sr_levels.json.
#
# Track-all mode (default)
# ------------------------
# When LIVE_SR_TRACK_ALL_INSTRUMENTS is True (default), every instrument
# subscribed by the live feed (i.e. every entry in the registry) is
# registered with the SR engine on feed start and gets a
# data/<...>/live_sr_levels.json file, even before any pivot is detected.
# This makes SR available uniformly across the whole universe, not just
# the level-filtered strikes.
#
# When False, only the instruments explicitly registered by other
# services (currently: level_instrument_filter_service for the index +
# filtered CE/PE strikes) are tracked.
LIVE_SR_ENABLED = _bool("LIVE_SR_ENABLED", True)
LIVE_SR_TRACK_ALL_INSTRUMENTS = _bool("LIVE_SR_TRACK_ALL_INSTRUMENTS", True)
LIVE_SR_LEFT_BARS = max(1, _int("LIVE_SR_LEFT_BARS", 15))
LIVE_SR_RIGHT_BARS = max(1, _int("LIVE_SR_RIGHT_BARS", 15))
LIVE_SR_MAX_CANDLES = max(50, _int("LIVE_SR_MAX_CANDLES", 500))
LIVE_SR_FILENAME = _str("LIVE_SR_FILENAME", "live_sr_levels.json")

# Persistence throttle. The file is ALWAYS written:
#   * on initial registration (empty file so readers never see a missing file)
#   * when a new pivot is confirmed
#   * when an existing level is broken
# Additionally, the file is refreshed every N finalized candles so that
# "candle_count" and "last_candle" do not drift stale. 0 disables the
# periodic refresh (write only on register / pivot / break). 1 writes on
# every candle close. Default 5 balances freshness against I/O load on
# a ~245-instrument registry.
LIVE_SR_PERSIST_EVERY_N_CANDLES = max(0, _int("LIVE_SR_PERSIST_EVERY_N_CANDLES", 5))


# ===========================================================================
# Mid-day startup backfill via intraday API
# ===========================================================================
# If the process starts mid-session (between SR_BACKFILL_START_HOUR/
# MINUTE and SR_BACKFILL_END_HOUR/MINUTE on a trading day), the SR
# backfill service fetches today's intraday 1-min candles from Upstox
# for every tracked instrument and seeds the SR engine with them:
#
#     upstox_client.HistoryV3Api.get_intra_day_candle_data(
#         instrument_key, "minutes", "1"
#     )
#
# That makes OR + SR levels immediately available on the dashboard,
# without waiting for the live feed to accumulate 31+ candles. The
# seed is idempotent: it clears in-memory candles/levels, rebuilds
# from the intraday response, and persists once at the end.
#
# Window default: 09:15 -> 23:59 IST. Weekends and holidays are
# skipped automatically. A Telegram summary is sent on the feed_start
# path (a log line at INFO is always written).
SR_BACKFILL_ENABLED = _bool("SR_BACKFILL_ENABLED", True)
SR_BACKFILL_MAX_WORKERS = max(1, min(16, _int("SR_BACKFILL_MAX_WORKERS", 4)))
SR_BACKFILL_START_HOUR = _int("SR_BACKFILL_START_HOUR", 9)
SR_BACKFILL_START_MINUTE = _int("SR_BACKFILL_START_MINUTE", 15)
SR_BACKFILL_END_HOUR = _int("SR_BACKFILL_END_HOUR", 23)
SR_BACKFILL_END_MINUTE = _int("SR_BACKFILL_END_MINUTE", 59)