# core/config.py
"""Configuration for the foundational market-data application.

G1 v3.2 additions (2026-10-28)
------------------------------
* Multi-index boundary surveillance — STRATEGY_MONITORED_INDICES,
  STRATEGY_TOUCH_PRIORITY_ORDER, STRATEGY_ROUTED_EXECUTION_INDEX.
* Auto-reverse on bias-side rejection — STRATEGY_ENTRY_ALLOW_AUTO_REVERSE,
  STRATEGY_ENTRY_PRIORITIZE_SUPPORT.
* LEVEL_FILTER_INDEXES now defaults to all four monitored indices
  (NIFTY, BANKNIFTY, MIDCPNIFTY, SENSEX) so that OR levels exist for
  every index the touch engine watches.
* LEVEL_FILTER_STRIKE_STEP_{BANKNIFTY,MIDCPNIFTY} added so the filter
  resolves strikes for those underlyings too (informational only;
  execution always routes to NIFTY).
* LEVEL_ISOLATION_BACKFILL_WINDOW opens at 09:15 (instead of 09:16) so
  the 09:15 candle that opens the OR window is included in the replay.

G1 v4.1 additions (2026-10-29)
------------------------------
Six new opt-out flags, each tied to one fix from the ``/isolated`` page
analysis (see ``/isolated page against G1 v4.0``). All default to
``True`` — the fixes are enabled out of the box and no ``.env`` change
is required. Each flag exists purely as a kill switch so an operator
can disable one change in isolation without reverting the whole v4.1
bundle.

    STRATEGY_ENTRY_REGISTER_COUNTER_SIDE        -> §5.A
    STRATEGY_ENTRY_CANDLE_FALLBACK_ENABLED      -> §5.B
    STRATEGY_ENTRY_TARGET_REQUIRE_POST_ENTRY    -> §5.C
    LEVEL_ISOLATION_REPLAY_USE_SERIES_OR        -> §5.D (replay)
    LEVEL_ISOLATION_OR_SELF_HEAL_VERIFY_HL      -> §5.D (self-heal)
    STRATEGY_ENTRY_FREEZE_ENTRY_ON_PROMOTION    -> §5.E

G1 v4.1 hotfix (2026-10-29)
---------------------------
``STRATEGY_RELATIONAL_R3_BIAS`` and ``STRATEGY_RELATIONAL_S3_BIAS`` are
read by ``level_isolated_instrument_service._process_winning_touch`` but
were missing from this file — the startup backfill crashed with::

    AttributeError: module 'core.config' has no attribute
    'STRATEGY_RELATIONAL_R3_BIAS'

Both are restored below under LEVEL ISOLATION.

G1 v4.1 dead-flag removal (2026-10-29)
--------------------------------------
Three constants were defined here but **never read by any service** —
they were documented as behaviour switches but silently fell through
their ``getattr(..., True)`` defaults (or were hard-coded by
construction) in the consuming code. All three have been removed so
operators are not misled into thinking they do something:

    STRATEGY_ENTRY_ONLY_ONCE_PER_DAY       (no reader — enforced by
                                            the closed 10-min buffer
                                            window)
    STRATEGY_ENTRY_FIRST_TO_CONFIRM_WINS   (no reader — bias side
                                            wins by iteration order,
                                            not by confirmed_time)
    STRATEGY_ENTRY_INCLUDE_PRETOUCH        (no reader — pre-touch
                                            pivots are excluded
                                            unconditionally in
                                            _compute_candidate())

Existing ``.env`` files that still set any of the three will have the
value silently ignored (``os.getenv`` with a default). The behaviours
those flags claimed are enforced by construction as noted above.

A fourth flag, ``STRATEGY_ENTRY_BLOCKED_BY_RESISTANCE``, remains
defined because it **is** read by ``_arm_bias_on_support()`` — it
gates the fast-window resistance blocker. Its docstring still notes
that the rule is a no-op today (spec §9.1) but the flag is live.
"""

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

def _csv_or_json_list(name: str, default: str = "") -> list[str]:
    """Accept either a comma-separated list or a JSON array in .env.

    * ``KEY=NSE_INDEX|Nifty 50,NSE_INDEX|Nifty Bank``      (CSV)
    * ``KEY=["NSE_INDEX|Nifty 50","NSE_INDEX|Nifty Bank"]`` (JSON)

    Both forms produce the same list of stripped strings. This exists
    because STRATEGY_MONITORED_INDICES and STRATEGY_TOUCH_PRIORITY_ORDER
    are documented as JSON arrays in the G1 v3.2 spec, but our other
    settings use CSV. Supporting both means an operator can copy the
    spec sample verbatim OR follow the project's existing CSV style.
    """
    import json
    raw = _str(name, default)
    if not raw:
        return []
    if raw.lstrip().startswith("["):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            return [
                str(part).strip()
                for part in parsed
                if str(part).strip()
            ]
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
    "data,downloads,output,__pycache__,.pytest_cache,htmlcov,test_logs",
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
#
# G1 v3.2 — LEVEL_FILTER_INDEXES now covers all four monitored indices
# so first_candle.json exists for every index the touch engine watches.
# Only NIFTY's level_instruments.json is consumed for execution (the
# other three are informational). The strike step per underlying is
# configurable below so BANKNIFTY (100) and MIDCPNIFTY (25) resolve
# cleanly if you ever route to those indices.
# ===========================================================================
LEVEL_FILTER_ENABLED = _bool("LEVEL_FILTER_ENABLED", True)

LEVEL_FILTER_INDEXES = _csv(
    "LEVEL_FILTER_INDEXES",
    "NIFTY,BANKNIFTY,MIDCPNIFTY,SENSEX",
)

LEVEL_FILTER_STRIKE_STEP_NIFTY = max(1, _int("LEVEL_FILTER_STRIKE_STEP_NIFTY", 50))
LEVEL_FILTER_STRIKE_STEP_SENSEX = max(1, _int("LEVEL_FILTER_STRIKE_STEP_SENSEX", 100))
LEVEL_FILTER_STRIKE_STEP_BANKNIFTY = max(1, _int("LEVEL_FILTER_STRIKE_STEP_BANKNIFTY", 100))
LEVEL_FILTER_STRIKE_STEP_MIDCPNIFTY = max(1, _int("LEVEL_FILTER_STRIKE_STEP_MIDCPNIFTY", 25))

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
#
# LIVE_SR_LEFT_BARS / LIVE_SR_RIGHT_BARS control the DISPLAY window
# (chart, /isolated SR Tracking card, Telegram). The fast entry scan
# uses STRATEGY_ENTRY_PIVOT_* below.
# ===========================================================================
LIVE_SR_ENABLED = _bool("LIVE_SR_ENABLED", True)
LIVE_SR_TRACK_ALL_INSTRUMENTS = _bool("LIVE_SR_TRACK_ALL_INSTRUMENTS", True)
LIVE_SR_LEFT_BARS = max(1, _int("LIVE_SR_LEFT_BARS", 15))
LIVE_SR_RIGHT_BARS = max(1, _int("LIVE_SR_RIGHT_BARS", 3))
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
# G1 v3.2 — MULTI-INDEX BOUNDARY SURVEILLANCE
# ===========================================================================
# Monitored indices and priority arbitration. When the project starts
# after the market close, the level-isolation engine replays the day
# for ALL configured indices (not just NIFTY and SENSEX), applies the
# priority order below, and reconstructs the "would-have-isolated"
# NIFTY strike — this is the simulate mode.
#
# STRATEGY_MONITORED_INDICES accepts either form in .env:
#     STRATEGY_MONITORED_INDICES=NSE_INDEX|Nifty 50,NSE_INDEX|Nifty Bank,NSE_INDEX|NIFTY MID SELECT,BSE_INDEX|SENSEX
#     STRATEGY_MONITORED_INDICES=["NSE_INDEX|Nifty 50","NSE_INDEX|Nifty Bank","NSE_INDEX|NIFTY MID SELECT","BSE_INDEX|SENSEX"]
#
# Every entry must be one of the four keys the isolation engine knows
# about (defined in services/level_isolated_instrument_service.INDEX_METADATA).
# ===========================================================================
STRATEGY_MONITORED_INDICES = _csv_or_json_list(
    "STRATEGY_MONITORED_INDICES",
    "NSE_INDEX|Nifty 50,"
    "NSE_INDEX|Nifty Bank,"
    "NSE_INDEX|NIFTY MID SELECT,"
    "BSE_INDEX|SENSEX",
)

# Priority order for simultaneous touches (first = highest priority).
# Values MUST be a subset of the canonical names derived from
# STRATEGY_MONITORED_INDICES: NIFTY / BANKNIFTY / MIDCPNIFTY / SENSEX.
#
# G1 v4.0 default order — MIDCPNIFTY leads, BANKNIFTY next, NIFTY is
# the routed execution index, SENSEX is the BSE anchor. The .env.example
# documents the same order. Changing this re-ranks touch arbitration
# without touching service code.
STRATEGY_TOUCH_PRIORITY_ORDER = _csv_or_json_list(
    "STRATEGY_TOUCH_PRIORITY_ORDER",
    "MIDCPNIFTY,BANKNIFTY,NIFTY,SENSEX",
)

# Which index gets the strike selection (execution routing). Under G1
# v3.2+ this is always NIFTY 50 regardless of which index fired the touch.
STRATEGY_ROUTED_EXECUTION_INDEX = _str(
    "STRATEGY_ROUTED_EXECUTION_INDEX", "NSE_INDEX|Nifty 50"
)

# Priority rank per canonical index name. Derived from
# STRATEGY_TOUCH_PRIORITY_ORDER so changing the order above re-ranks
# the touch arbitration without touching any service code.
def _priority_map() -> dict[str, int]:
    return {
        str(name).strip().upper(): i + 1
        for i, name in enumerate(STRATEGY_TOUCH_PRIORITY_ORDER)
        if str(name).strip()
    }

STRATEGY_INDEX_PRIORITY = _priority_map()

# ===========================================================================
# LEVEL ISOLATION — G1 v3.2 multi-index arbitration + NIFTY routing
# ===========================================================================
# Monitored indices: NIFTY / BANKNIFTY / MIDCPNIFTY / SENSEX (see
# STRATEGY_MONITORED_INDICES above).
#
# Priority arbitration (one-shot per day):
#
#     NIFTY-R3 -> NIFTY-S3 -> BANKNIFTY-R3 -> BANKNIFTY-S3
#         -> MIDCPNIFTY-R3 -> MIDCPNIFTY-S3 -> SENSEX-R3 -> SENSEX-S3
#
# (Effective order is determined at runtime by
# STRATEGY_TOUCH_PRIORITY_ORDER above.)
#
# The FIRST touch to resolve (by priority rank when multiple arrive
# within the 2-second polling window) becomes the winning touch.
#
# ALWAYS routes to a NIFTY strike:
#     * Bias = CE if the winning touch was R3, PE if it was S3.
#     * Strike = nearest round 50 NIFTY strike to the NIFTY spot LTP
#       at the moment of the winning touch.
#
# Persisted to data/<LEVEL_ISOLATION_FILENAME>; a full event log for
# the day is preserved in the same file under "events".
#
# The record carries:
#     * trigger_index / trigger_side  (which index fired)
#     * trigger_priority              (arbitration rank)
#     * simultaneous_touches          (other indices that fired in the
#                                      same 2-second window)
#     * routed_index                  (always NIFTY)
#     * routed_nifty_ltp              (NIFTY spot at touch)
#     * instrument_*                  (own option data snapshot)
#     * source                        ("live" or "backfill" /
#                                      "simulate" — the UI shows
#                                      "SIMULATE MODE" for the latter)
# ===========================================================================
LEVEL_ISOLATION_ENABLED = _bool("LEVEL_ISOLATION_ENABLED", True)

# Output filename under data/ (resolved against LIVE_DATA_DIR).
LEVEL_ISOLATION_FILENAME = _str(
    "LEVEL_ISOLATION_FILENAME", "level_isolated_instrument.json"
)

# ---- G1 v4.1 hotfix — trigger-side → bias-side mapping ---------------- #
# Read by level_isolated_instrument_service._process_winning_touch to
# decide which option side to isolate at the routed NIFTY strike:
#
#     trigger_side == "R3"  →  bias = STRATEGY_RELATIONAL_R3_BIAS  ("CE")
#     trigger_side == "S3"  →  bias = STRATEGY_RELATIONAL_S3_BIAS  ("PE")
#
# Under G1 v4.0 semantics:
#     * R3 (upward break of the opening range)  → CE (calls, upside)
#     * S3 (downward break of the opening range) → PE (puts, downside)
#
# Both default to the canonical value but are configurable in .env if
# you ever want to experiment with the opposite semantics. Values are
# normalised to upper-case so "ce", "Ce", "CE" all work.
STRATEGY_RELATIONAL_R3_BIAS = _str(
    "STRATEGY_RELATIONAL_R3_BIAS", "CE"
).upper()
STRATEGY_RELATIONAL_S3_BIAS = _str(
    "STRATEGY_RELATIONAL_S3_BIAS", "PE"
).upper()

# Throttle for the on-tick touch check (per any index tick event).
# The check runs at most once every N seconds regardless of tick rate.
LEVEL_ISOLATION_CHECK_INTERVAL_SECONDS = max(
    0.5, _float("LEVEL_ISOLATION_CHECK_INTERVAL_SECONDS", 2.0)
)

# Max age (seconds) of the last NIFTY tick before it is treated as
# stale during routing. Under G1 v3.2+ the routing always needs a
# fresh NIFTY spot LTP; when NIFTY is stale for a non-NIFTY trigger
# the touch is still recorded but isolation is deferred until the
# next NIFTY tick.
LEVEL_ISOLATION_NIFTY_TICK_STALE_SECONDS = max(
    1, _int("LEVEL_ISOLATION_NIFTY_TICK_STALE_SECONDS", 30)
)

# Startup backfill window (intraday-candle replay). Widened to start
# at 09:15 (instead of 09:16) so the 09:15 opening candle is included
# in the replay that builds today's OR levels. End is 23:59 so a
# 23:46 startup still reconstructs the whole session.
LEVEL_ISOLATION_BACKFILL_WINDOW_START_HOUR = _int(
    "LEVEL_ISOLATION_BACKFILL_WINDOW_START_HOUR", 9
)
LEVEL_ISOLATION_BACKFILL_WINDOW_START_MINUTE = _int(
    "LEVEL_ISOLATION_BACKFILL_WINDOW_START_MINUTE", 15
)
LEVEL_ISOLATION_BACKFILL_WINDOW_END_HOUR = _int(
    "LEVEL_ISOLATION_BACKFILL_WINDOW_END_HOUR", 23
)
LEVEL_ISOLATION_BACKFILL_WINDOW_END_MINUTE = _int(
    "LEVEL_ISOLATION_BACKFILL_WINDOW_END_MINUTE", 59
)

LEVEL_ISOLATION_TELEGRAM_ENABLED = _bool("LEVEL_ISOLATION_TELEGRAM_ENABLED", True)

# ---- G1 v4.1 — Replay / self-heal integrity (§5.D) --------------------- #
# Both flags close the "phantom R3 touch" class of bug: the replay
# used to check touches against the *on-disk* first_candle.json OR
# levels while iterating a *freshly fetched* intraday candle series.
# When those two disagree (stale file, wider OR window, previous-day
# file that survived a restart), a touch is recorded against an R3
# the index never actually reached — e.g. R3 = 13329.65 vs today's
# high = 13298.75, with the recorded trigger_ltp 13310.60 below R3.
#
# LEVEL_ISOLATION_REPLAY_USE_SERIES_OR
#   When True (default), `_replay()` recomputes OR levels from the
#   same series it is iterating (via
#   `first_candle_service.compute_for_series`) instead of trusting
#   the file. This is the primary fix — the OR values used for the
#   trigger check are guaranteed consistent with the candles.
#
# LEVEL_ISOLATION_OR_SELF_HEAL_VERIFY_HL
#   When True (default), `_maybe_self_heal()` additionally compares
#   the cached OR file's first_candle H / L against today's real
#   first intraday bar (via
#   `live_sr_calc_service.get_first_intraday_candle`) and forces a
#   targeted recompute on mismatch. Catches the "stale-but-today-
#   dated" case that the existing missing-file / stale-date check
#   does not catch. Cheap: one index fetch + (worst case) 1–2
#   option recomputes, not the full registry sweep.
#
# Set either to False to restore pre-v4.1 behaviour for that specific
# path. Both True is the recommended default.
LEVEL_ISOLATION_REPLAY_USE_SERIES_OR = _bool(
    "LEVEL_ISOLATION_REPLAY_USE_SERIES_OR", True
)
LEVEL_ISOLATION_OR_SELF_HEAL_VERIFY_HL = _bool(
    "LEVEL_ISOLATION_OR_SELF_HEAL_VERIFY_HL", True
)

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
# STRATEGY ENTRY SIGNAL — G1 v3.2 (fast pivot + LTP-tier stop + auto-reverse)
# ===========================================================================
# Trigger chain:
#
#   1. Any of NIFTY / BANKNIFTY / MIDCPNIFTY / SENSEX touches its R3 or
#      S3 (multi-index arbitration, see LEVEL ISOLATION above). The bias
#      side is fixed by the touch (R3 -> CE, S3 -> PE) and the nearest
#      NIFTY round-50 strike to NIFTY spot LTP is the isolated strike.
#
#   2. On the isolated NIFTY strike, the entry engine runs a FAST pivot
#      scan (left=15, right=3) on the bias-side option's candle series:
#         • display window:  LIVE_SR_LEFT_BARS / LIVE_SR_RIGHT_BARS
#                            (unchanged; used by the chart, SR Tracking
#                            card, Telegram).
#         • entry window:    STRATEGY_ENTRY_PIVOT_LEFT_BARS /
#                            STRATEGY_ENTRY_PIVOT_RIGHT_BARS.
#
#      With left=15, right=3, a pivot that formed at 10:04 becomes
#      tradeable at 10:07 instead of 10:19.
#
#   3. Buffer window is [T_touch, T_touch + BUFFER_MINUTES]. Supports
#      (and, for the auto-reverse path, resistances) whose pivot_time
#      falls inside the window are iterated in pivot_time order.
#
#      Pivots whose pivot_time is BEFORE T_touch are excluded
#      unconditionally in _compute_candidate(). The previous
#      STRATEGY_ENTRY_INCLUDE_PRETOUCH flag was a no-op — no service
#      read it — and has been removed. See "dead-flag removal" in the
#      header docstring.
#
#   4. Decision priority (SUPPORT TAKES PRIORITY):
#         CASE 1  Fast SUPPORT confirms on the bias side
#                 → ARM THE BIAS SIDE on support bounce.
#
#         CASE 2  Fast RESISTANCE confirms on the bias side with NO
#                 fast support in the window
#                 → AUTO-REVERSE to the COUNTER side, taking the live
#                   LTP of the counter instrument at the exact instant
#                   of the bias-side rejection. entry_type becomes
#                   "auto_reverse_on_rejection".
#
#         CASE 3  Buffer window expires with no valid confirmation
#                 → HARD BLOCK "all_candidates_in_window_blocked".
#
#   5. Entry price (both cases):
#         • candle close at support.confirmed_time_fast, else
#         • live LTP (fallback when the confirming bar is still
#           forming or the candle isn't in the live series).
#
#   6. Stop-loss — LTP-tier (unchanged from G1 v2):
#          sl_pct   = tier_lookup(entry_price)
#          sl_pct   = min(sl_pct, STRATEGY_MAX_STOP_PCT)   # hard cap
#          stop_raw = entry_price × (1 − sl_pct)
#          stop     = round_to_tick(stop_raw, mode = TICK_ROUNDING)
#          stop_pct = (entry_price − stop) / entry_price
#
#   7. Hard blocks: invalid_entry_price (≤0), no_entry_price (both
#      candle close and LTP missing), entry_below_support (entry_price
#      < support.price — the support did not hold during the wait).
#
#   8. Soft warning (does not block): support_far_below_entry
#      (support >20% below entry — informational only).
#
#   9. Tiebreak (both bias and counter armed): the bias side is
#      iterated first in _compute_candidate() and dominates the
#      `chosen` pick, so the bias side wins by iteration order. There
#      is no confirmed-time-based tiebreak. (The previous
#      STRATEGY_ENTRY_FIRST_TO_CONFIRM_WINS flag was a no-op — nothing
#      read it — and has been removed. See "dead-flag removal" in the
#      header docstring.)
#
#  10. Promotion (unchanged mechanics): if the chosen side is the
#      counter side, promote that instrument to be the active isolated
#      one via isolated_instrument_promotion_service.
#
# The signal is exposed on /isolated and via
# GET /admin/level-isolation/strategy-signal. Order placement is
# NOT wired by this feature — that is a separate step.
# ===========================================================================
STRATEGY_ENTRY_ENABLED = _bool("STRATEGY_ENTRY_ENABLED", True)

# When True (default), evaluate CE and PE at the isolated strike
# (crossover).  When False, only the bias side (PE for S3, CE for R3)
# is considered.
STRATEGY_ENTRY_ALLOW_CROSSOVER = _bool(
    "STRATEGY_ENTRY_ALLOW_CROSSOVER", True
)

# Upper bound of the pivot_time window relative to the touch. The
# window is strictly [T_touch, T_touch + BUFFER] — never symmetric.
# A pivot that forms at 10:08 (within the buffer) confirms fast at
# 10:11 — still within the buffer, still valid.
STRATEGY_ENTRY_BUFFER_MINUTES = max(
    0, _int("STRATEGY_ENTRY_BUFFER_MINUTES", 10)
)

# NOTE: STRATEGY_ENTRY_INCLUDE_PRETOUCH was removed in G1 v4.1
# (2026-10-29). It was a no-op — no service read it — and the
# pre-touch exclusion is enforced unconditionally in
# _compute_candidate(). Existing .env files that still set it are
# silently ignored.
#
# NOTE: STRATEGY_ENTRY_ONLY_ONCE_PER_DAY was removed in the same pass.
# It was also a no-op; "arm only once per day" is enforced by the
# closed 10-minute buffer window rather than by a flag.
#
# NOTE: STRATEGY_ENTRY_FIRST_TO_CONFIRM_WINS was removed in the same
# pass. It was also a no-op; the bias side wins by iteration order in
# _compute_candidate(), not by confirmed_time_fast.
#
# NOTE: STRATEGY_ENTRY_PRICE_MODE was removed in G1 v2. Entry is
# always "close at confirmed_time_fast, else live LTP". The old
# "pivot_price" mode was the broken path; deleting it prevents
# accidental re-use.

# When True (default), a resistance confirmed on or before the entry
# support's confirmed_time_fast, with pivot_time strictly after the
# entry support's pivot_time and price strictly above the entry
# support's price, blocks the candidate. When False, resistance never
# blocks.
#
# NOTE: this rule is a NO-OP today (see G1 v3.2 spec §9.1). It is
# retained because _arm_bias_on_support() *does* read this flag — the
# rule is live but has no effect because the fast window's time
# ordering makes it unreachable. Kept for forward compatibility.
STRATEGY_ENTRY_BLOCKED_BY_RESISTANCE = _bool(
    "STRATEGY_ENTRY_BLOCKED_BY_RESISTANCE", True
)

# ---- G1 v3.2 — Auto-reverse on bias-side rejection ----------------------
# When True (default) and the bias side confirms a fast RESISTANCE with
# NO fast SUPPORT inside the buffer window, the engine flips to the
# counter side and arms at the live LTP of the counter instrument at
# the exact instant of the bias-side rejection.
#
# Set to False to disable auto-reverse entirely (the engine then waits
# for a bias-side support or blocks at window expiry — pre-v3.2
# behaviour).
STRATEGY_ENTRY_ALLOW_AUTO_REVERSE = _bool(
    "STRATEGY_ENTRY_ALLOW_AUTO_REVERSE", True
)

# When True (default) and BOTH a support and a resistance confirm on
# the bias side inside the window, the SUPPORT WINS and the bias side
# arms (momentum follow). When False, resistance wins and auto-reverse
# fires even when a support also confirmed.
STRATEGY_ENTRY_PRIORITIZE_SUPPORT = _bool(
    "STRATEGY_ENTRY_PRIORITIZE_SUPPORT", True
)

# ---- G1 v3.2 — Fast entry-pivot window ----------------------------------
# LEFT defaults to LIVE_SR_LEFT_BARS so the pivot bar is identical in
# both the display and entry views. Only the RIGHT bar count differs.
# A smaller RIGHT means faster arming. Range 1..5 recommended.
STRATEGY_ENTRY_PIVOT_LEFT_BARS = max(
    1, _int("STRATEGY_ENTRY_PIVOT_LEFT_BARS", LIVE_SR_LEFT_BARS)
)
STRATEGY_ENTRY_PIVOT_RIGHT_BARS = max(
    1, min(10, _int("STRATEGY_ENTRY_PIVOT_RIGHT_BARS", 3))
)

# ---- G1 v3.2 — LTP-tier stop-loss ---------------------------------------
# JSON array of tiers: [{"max_ltp": <float|null>, "sl_pct": <0..1>}, ...].
# Lookup: first tier whose max_ltp is null or > entry_price. Default
# table gives 15% / 12% / 10% / 5% for LTP buckets <100 / <200 / <300
# / open-ended. Parsed defensively; bad JSON falls back to the default.
STRATEGY_ENTRY_STOP_LTP_TIERS = _str(
    "STRATEGY_ENTRY_STOP_LTP_TIERS",
    '[{"max_ltp":100,"sl_pct":0.15},'
    '{"max_ltp":200,"sl_pct":0.12},'
    '{"max_ltp":300,"sl_pct":0.10},'
    '{"max_ltp":null,"sl_pct":0.05}]',
)

# "down" | "up" | "nearest". "down" moves the stop further from
# entry (wider stop, less whipsaw). Only matters when the exact
# percentage lands between two ticks.
STRATEGY_ENTRY_STOP_TICK_ROUNDING = _str(
    "STRATEGY_ENTRY_STOP_TICK_ROUNDING", "down"
).lower()

# Absolute maximum sl_pct any tier can yield. Tiers whose sl_pct
# exceeds this cap are clamped and an ERROR is logged.
STRATEGY_MAX_STOP_PCT = _float("STRATEGY_MAX_STOP_PCT", 0.35)

# ---- G1 v3.2 — Promotion -------------------------------------------------
# When the entry-signal ARMS on the COUNTER side (either via crossover
# or via auto-reverse), promote that instrument to be the active
# isolated instrument for the rest of the day. The isolation record
# itself is never modified; the promotion is stored in a separate
# sidecar file:
#   data/level_isolated_instrument_promotion.json
STRATEGY_ENTRY_ALLOW_PROMOTION = _bool(
    "STRATEGY_ENTRY_ALLOW_PROMOTION", True
)

# When True, a second promotion may override the first. Default False
# — the first ARMED side wins and the pick is stable for the day.
STRATEGY_ENTRY_ALLOW_RE_PROMOTION = _bool(
    "STRATEGY_ENTRY_ALLOW_RE_PROMOTION", False
)

# ---- G1 v4.1 — Entry-signal correctness (§5.A / §5.B / §5.C / §5.E) ----
# Four opt-out flags covering the /isolated page analysis fixes. All
# default to True (= fixed behaviour); each False restores the pre-v4.1
# path for that single aspect.
#
# STRATEGY_ENTRY_REGISTER_COUNTER_SIDE (§5.A)
#   When True (default) and STRATEGY_ENTRY_ALLOW_CROSSOVER is also
#   True, `_isolate()` registers BOTH the bias-side and counter-side
#   instruments with `live_sr_levels_service` immediately after strike
#   resolution, and includes both sides' candidates (tagged
#   `side: "bias" | "counter"`) in `event["candidates"]`.
#
# STRATEGY_ENTRY_CANDLE_FALLBACK_ENABLED (§5.B)
#   When True (default), `_entry_price_for()` falls through to
#   `live_sr_calc_service.get_candle_close_at()` when the fast path
#   (`live_sr_levels_service._states[key]["candles"]`) cannot resolve
#   the confirming minute.
#
# STRATEGY_ENTRY_TARGET_REQUIRE_POST_ENTRY (§5.C)
#   When True (default), the target resistance must satisfy
#   `r.confirmed_time_fast > entry_support.confirmed_time_fast` in
#   addition to `r.price > entry_price`.
#
# STRATEGY_ENTRY_FREEZE_ENTRY_ON_PROMOTION (§5.E)
#   When True (default), `isolated_instrument_promotion_service.apply_promotion()`
#   writes `frozen_entry = {price, price_source, stop, target, time,
#   frozen_at}` onto the promotion record at the moment the winning
#   side is armed, and returns it on the result dict.
STRATEGY_ENTRY_REGISTER_COUNTER_SIDE = _bool(
    "STRATEGY_ENTRY_REGISTER_COUNTER_SIDE", True
)
STRATEGY_ENTRY_CANDLE_FALLBACK_ENABLED = _bool(
    "STRATEGY_ENTRY_CANDLE_FALLBACK_ENABLED", True
)
STRATEGY_ENTRY_TARGET_REQUIRE_POST_ENTRY = _bool(
    "STRATEGY_ENTRY_TARGET_REQUIRE_POST_ENTRY", True
)
STRATEGY_ENTRY_FREEZE_ENTRY_ON_PROMOTION = _bool(
    "STRATEGY_ENTRY_FREEZE_ENTRY_ON_PROMOTION", True
)

# ===========================================================================
# SIMULATION — dummy-order replay for the isolated instrument (post-close)
# ===========================================================================
# The simulation service replays the isolated instrument's day using
# the authoritative intraday 1-min candles. It builds TWO scenarios
# side by side so the operator can compare the strategy signal's own
# target with the regime-based target computed from the option's own
# OR levels:
#
#   A) "signal_v2"  — the target and stop that the strategy entry
#                     signal produced (nearest resistance above entry
#                     for the target; LTP-tier stop for the stop).
#
#   B) "regime"     — the target and stop picked by the simulation's
#                     regime classifier (see below), using the option's
#                     own OR levels H, L, A, R1, R2, R3, S1, S2, S3.
#
# Regime classification (entry = E):
#
#   LOW_EXTREME   (E < S3)              target = OR.L     stop = tier
#   LOW           (S3 ≤ E < S2)         target = OR.L     stop = tier
#   LOW_MID       (S2 ≤ E < A)          target = OR.A     stop = tier
#   MID           (A ≤ E < R2)          target = nearest R > E   stop = tier
#   HIGH          (R2 ≤ E < R3)         target = E + (R2 − A)
#                                       stop   = min(tier, E − (A − L))
#   HIGH_EXTREME  (E ≥ R3)              target = E + (R2 − A)
#                                       stop   = min(tier, E − (A − L))
#
# Both scenarios are replayed against the SAME intraday candle series,
# starting from the entry fill (entry.time = close of the confirming
# 1-min bar). Exit order per bar:
#
#   * If BOTH target and stop are inside the bar's [low, high] range,
#     SIMULATION_SAME_BAR_STOP_FIRST decides the winner (default True
#     — conservative: assume the stop hit first).
#   * Otherwise, whichever boundary the bar touches first wins.
#   * If neither boundary is touched by the end of the session, the
#     scenario is UNRESOLVED and the exit price = last close.
#
# The endpoint is POST-only and stateless — no disk writes, no
# background jobs, no Telegram. It is purely a "what would have
# happened" diagnostic on the isolated page.
# ===========================================================================
SIMULATION_ENABLED = _bool("SIMULATION_ENABLED", True)

# When True (default) the response carries BOTH scenarios (signal_v2
# and regime) side by side. When False, only the signal_v2 scenario is
# returned (regime is disabled).
SIMULATION_USE_REGIME_TARGETS = _bool(
    "SIMULATION_USE_REGIME_TARGETS", True
)

# How the HIGH / HIGH_EXTREME regime computes the stop:
#   "tier"     -> use the LTP-tier stop only
#   "range"    -> use only the OR half-range: E − (A − L)
#   "tighter"  -> min(tier, range) — default
SIMULATION_STOP_MODE_HIGH = _str(
    "SIMULATION_STOP_MODE_HIGH", "tighter"
).lower()

# When target and stop are BOTH inside the same 1-min bar's [low, high]
# range, which hit first? Default True (conservative — assume stop).
SIMULATION_SAME_BAR_STOP_FIRST = _bool(
    "SIMULATION_SAME_BAR_STOP_FIRST", True
)

# Tick rounding for the target and stop prices. "nearest" / "down" / "up".
SIMULATION_TICK_ROUNDING = _str(
    "SIMULATION_TICK_ROUNDING", "nearest"
).lower()

# ===========================================================================
# DUMMY ORDERS — LIVE-tracked dummy orders persisted to disk
# ===========================================================================
# During live market hours, the dummy_order_service polls the entry
# signal every DUMMY_ORDER_TRACK_INTERVAL_SECONDS seconds. When a
# candidate is "armed", it creates a dummy-order record (never a real
# order) and tracks it forward against live LTP ticks + periodic
# intraday-bar reconciliation. Closed orders are frozen; open orders
# auto-close at DUMMY_ORDER_EOD_HOUR:MINUTE.
#
# Persistence:
#   <LIVE_DATA_DIR>/<DUMMY_ORDER_FILENAME>  (default dummy_orders.json)
#
# This is a **dummy** — no real Upstox order is ever placed by this
# service. Every record is labelled "dummy" in the file so there's no
# ambiguity during audit.
# ===========================================================================
DUMMY_ORDER_ENABLED = _bool("DUMMY_ORDER_ENABLED", True)

# How often the scheduler fires the tracking job, in seconds.
DUMMY_ORDER_TRACK_INTERVAL_SECONDS = max(
    5, _int("DUMMY_ORDER_TRACK_INTERVAL_SECONDS", 30)
)

# How often, in minutes, to reconcile each open order from fresh intraday
# 1-min bars (via Upstox HistoryV3Api). Set to 0 to disable bar reconcile
# entirely (LTP-only tracking).
DUMMY_ORDER_RECONCILE_MINUTES = max(
    0, _int("DUMMY_ORDER_RECONCILE_MINUTES", 2)
)

# Live-tracking window (IST). Orders can only be CREATED after
# DUMMY_ORDER_START_HOUR:MINUTE.
DUMMY_ORDER_START_HOUR = _int("DUMMY_ORDER_START_HOUR", 9)
DUMMY_ORDER_START_MINUTE = _int("DUMMY_ORDER_START_MINUTE", 15)

# After DUMMY_ORDER_EOD_HOUR:MINUTE, the service switches to "retroactive"
# mode: any still-OPEN order is reconciled from the day's complete bar
# history, then auto-closed.
DUMMY_ORDER_EOD_HOUR = _int("DUMMY_ORDER_EOD_HOUR", 15)
DUMMY_ORDER_EOD_MINUTE = _int("DUMMY_ORDER_EOD_MINUTE", 35)

# Persistence filename under LIVE_DATA_DIR.
DUMMY_ORDER_FILENAME = _str(
    "DUMMY_ORDER_FILENAME", "dummy_orders.json"
)

# When True (default), a second order variant (regime target) is created
# for each armed candidate.
DUMMY_ORDER_CREATE_REGIME_VARIANT = _bool(
    "DUMMY_ORDER_CREATE_REGIME_VARIANT", True
)

# Safety cap on the number of records per day.
DUMMY_ORDER_MAX_ORDERS_PER_DAY = max(
    1, _int("DUMMY_ORDER_MAX_ORDERS_PER_DAY", 100)
)
# When True (default), each dummy order fires exactly TWO Telegram
# messages over its lifetime — one on creation, one on close.
DUMMY_ORDER_TELEGRAM_ENABLED = _bool("DUMMY_ORDER_TELEGRAM_ENABLED", True)

# ===========================================================================
# G1 v4.1 — PATH B — EXTERNAL-INDEX TRIGGER (MULTI-ORDER SR WATCH)
# ===========================================================================
# When the isolation trigger originates from an EXTERNAL index
# (MIDCPNIFTY / BANKNIFTY / SENSEX), the 10-minute fast pivot scan and
# auto-reverse from Path A are DISABLED. The bias-side instrument stays
# isolated for the whole session and a dummy BUY order is placed for
# every post-isolation SR level confirmed on that bias instrument:
#
#     Support  on bias side  ->  BUY  bias-side instrument
#     Resistance on bias side ->  BUY  counter-side instrument
#
# Every order is tracked against a Rupee-based profit ladder:
#     peak_profit_rs >= STRATEGY_EXT_PROFIT_ACTIVATION_RS
#         ->  switch state from PRE_TRAIL to TRAILING
#         ->  trailing stop = Entry + (peak_profit_rs - TRAIL_GAP_RS) / lot
#         ->  floor of TRAIL_FLOOR_RS guarantees a minimum locked gain
#
# Only NIFTY (65/lot) and SENSEX (20/lot) produce executable dummy
# orders; other underlyings are tracked for telemetry only.
# ===========================================================================

# Master switch for Path B behaviour. When False, the engine reverts
# to the pre-v4.1 single-path model (Path A only, no multi-order SR watch).
STRATEGY_EXT_PATH_B_ENABLED = _bool("STRATEGY_EXT_PATH_B_ENABLED", True)

# Comma-separated list of underlyings that generate EXECUTABLE dummy
# orders on Path B. Anything not in this list is tracked for telemetry
# (SR levels persisted, dashboard visible) but never produces an order.
STRATEGY_EXT_TRACKED_UNDERLYINGS = _csv(
    "STRATEGY_EXT_TRACKED_UNDERLYINGS", "NIFTY,SENSEX",
)

# Lot sizes used by the Rupee profit calculation on Path B.
STRATEGY_EXT_LOT_SIZE_NIFTY = max(1, _int("STRATEGY_EXT_LOT_SIZE_NIFTY", 65))
STRATEGY_EXT_LOT_SIZE_SENSEX = max(1, _int("STRATEGY_EXT_LOT_SIZE_SENSEX", 20))

# Rupee-based profit trailing configuration.
STRATEGY_EXT_PROFIT_ACTIVATION_RS = _float(
    "STRATEGY_EXT_PROFIT_ACTIVATION_RS", 1000.0,
)
STRATEGY_EXT_TRAIL_GAP_RS = _float(
    "STRATEGY_EXT_TRAIL_GAP_RS", 500.0,
)
STRATEGY_EXT_TRAIL_FLOOR_RS = _float(
    "STRATEGY_EXT_TRAIL_FLOOR_RS", 1000.0,
)

# Alternate trailing-activation trigger: if entry < R2 and LTP >= R3
# (both from the option's own OR levels), start trailing immediately
# even if peak profit has not yet reached the activation threshold.
STRATEGY_EXT_R3_BREAKOUT_ACTIVATION_ENABLED = _bool(
    "STRATEGY_EXT_R3_BREAKOUT_ACTIVATION_ENABLED", True,
)

# Bearish-regime exit while in TRAILING mode.
STRATEGY_EXT_BEARISH_EXIT_ENABLED = _bool(
    "STRATEGY_EXT_BEARISH_EXIT_ENABLED", True,
)
STRATEGY_EXT_BEARISH_CANDLES = max(
    1, _int("STRATEGY_EXT_BEARISH_CANDLES", 2),
)

# When any Path B trade hits its target, freeze the day: no new SR
# orders are evaluated; open positions continue trailing until closed.
STRATEGY_EXT_LOCKDOWN_ON_TARGET = _bool(
    "STRATEGY_EXT_LOCKDOWN_ON_TARGET", True,
)

# Polling cadence for the multi-index boundary watch. Spec says 2 s.
STRATEGY_TOUCH_POLL_INTERVAL_SEC = max(
    0.5, _float("STRATEGY_TOUCH_POLL_INTERVAL_SEC", 2.0),
)

# Convenience maps derived once at import time.
STRATEGY_LOT_SIZE_MAP = {
    "NIFTY": STRATEGY_EXT_LOT_SIZE_NIFTY,
    "SENSEX": STRATEGY_EXT_LOT_SIZE_SENSEX,
}
STRATEGY_EXT_TRACKED_UNDERLYINGS_SET = {
    str(u).strip().upper()
    for u in STRATEGY_EXT_TRACKED_UNDERLYINGS
    if str(u).strip()
}