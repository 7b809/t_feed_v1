# core/config.py
"""Configuration for the foundational market-data application.

Order execution strategy (2026-11-06)
-------------------------------------
All standard/pre-11:30 instrument isolations use one execution mode:
unified Path A. The isolation trigger index is recorded for diagnostics
but does not switch the execution strategy. Standard Path A uses the
post-isolation 15/3 S/R routing and dummy-order engine.

Stop-loss remains controlled by STRATEGY_ORDER_STOP_LOSS_PCT.
The standard target milestone is fixed by entry-price band:
    0 < entry <= 100: entry + entry / 3
    100 < entry <= 200: entry + entry / 4
    entry > 200: entry + entry / 5
After the target milestone, stop-loss remains active and the engine
waits for a new 15/3 Resistance confirmation before exiting at its
confirmation candle close.

Additional live-entry filters:
    * STANDARD 09:30 backup reversal: suppress PE bias by default.
    * STANDARD 09:30 backup reversal: CE bias requires a positive
      isolated-leg EMA slope (default EMA9, two-bar lookback).
    * EARLY_EXTREME_REVERSAL same-leg Resistance entries require an
      inclusive ₹80–₹100 confirmation-candle entry premium by default.

POST-11:30 remains a separate time-based strategy with its existing
entry, target, stop-loss and 15/15 Resistance exit conditions.

The old Path A/Path B strategy-entry scan and promotion dispatch is no
longer used by the standard execution flow.
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

    Both forms produce the same list of stripped strings.
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
ORDER_LEDGER_COLLECTION = _str("ORDER_LEDGER_COLLECTION")
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
    "meta_data,.git,.venv,venv,myenv,templates,routes,"
    "services,core,token_tasks,upstox_app,static,tests",
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

# Live runtime snapshot: one combined document per IST trading date.
# Defaults to the same database used by the daily archive configuration.
LIVE_ORDER_SNAPSHOT_ENABLED = _bool("LIVE_ORDER_SNAPSHOT_ENABLED", True)
LIVE_ORDER_SNAPSHOT_MONGO_DB = _str(
    "LIVE_ORDER_SNAPSHOT_MONGO_DB", DAILY_BACKUP_MONGO_DB
)
LIVE_ORDER_SNAPSHOT_DEBOUNCE_SECONDS = max(
    0.1, _float("LIVE_ORDER_SNAPSHOT_DEBOUNCE_SECONDS", 0.5)
)
LIVE_ORDER_SNAPSHOT_RETRY_INITIAL_SECONDS = max(
    0.1, _float("LIVE_ORDER_SNAPSHOT_RETRY_INITIAL_SECONDS", 1.0)
)
LIVE_ORDER_SNAPSHOT_RETRY_MAX_SECONDS = max(
    LIVE_ORDER_SNAPSHOT_RETRY_INITIAL_SECONDS,
    _float("LIVE_ORDER_SNAPSHOT_RETRY_MAX_SECONDS", 30.0)
)
LIVE_ORDER_SNAPSHOT_SHUTDOWN_TIMEOUT_SECONDS = max(
    1.0, _float("LIVE_ORDER_SNAPSHOT_SHUTDOWN_TIMEOUT_SECONDS", 20.0)
)
DAILY_BACKUP_MAX_BLOB_MB = max(1, _int("DAILY_BACKUP_MAX_BLOB_MB", 15))

# ===========================================================================
# First-candle OR + EMA-cross detection (09:16:10 Mon-Fri)
# ===========================================================================
FIRST_CANDLE_ENABLED = _bool("FIRST_CANDLE_ENABLED", True)
FIRST_CANDLE_HOUR = _int("FIRST_CANDLE_HOUR", 9)
FIRST_CANDLE_MINUTE = _int("FIRST_CANDLE_MINUTE", 17)
FIRST_CANDLE_SECOND = _int("FIRST_CANDLE_SECOND", 10)
FIRST_CANDLE_WORKERS = max(1, min(32, _int("FIRST_CANDLE_WORKERS", 8)))

# ===========================================================================
# Level-instrument filter (per-index CE/PE strike list from OR levels)
# ===========================================================================
LEVEL_FILTER_ENABLED = _bool("LEVEL_FILTER_ENABLED", True)

LEVEL_FILTER_INDEXES = _csv(
    "LEVEL_FILTER_INDEXES",
    "NIFTY,BANKNIFTY,MIDCPNIFTY,SENSEX",
)

LIVE_SR_INTRADAY_UNIT = "minutes"
LIVE_SR_INTRADAY_INTERVAL = "1"
LIVE_SR_CALC_DELAY_SECONDS = 10

# Base data directory definition in core/config.py
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# Isolation output directory: data/isolation
ISOLATION_SR_DIR = DATA_DIR / "isolation"
ISOLATION_SR_DIR.mkdir(parents=True, exist_ok=True)
LEVEL_FILTER_STRIKE_STEP_NIFTY = max(1, _int("LEVEL_FILTER_STRIKE_STEP_NIFTY", 50))
LEVEL_FILTER_STRIKE_STEP_SENSEX = max(1, _int("LEVEL_FILTER_STRIKE_STEP_SENSEX", 100))
LEVEL_FILTER_STRIKE_STEP_BANKNIFTY = max(1, _int("LEVEL_FILTER_STRIKE_STEP_BANKNIFTY", 100))
LEVEL_FILTER_STRIKE_STEP_MIDCPNIFTY = max(1, _int("LEVEL_FILTER_STRIKE_STEP_MIDCPNIFTY", 25))

LEVEL_FILTER_STRIKE_STEPS_JSON = _str("LEVEL_FILTER_STRIKE_STEPS_JSON", "")

LEVEL_FILTER_RANGE_MODE = _str("LEVEL_FILTER_RANGE_MODE", "wide").lower()

LEVEL_FILTER_INCLUDE_ATM_PLUS_MINUS = max(
    0, _int("LEVEL_FILTER_INCLUDE_ATM_PLUS_MINUS", 0)
)

LEVEL_FILTER_INCLUDE_INDEX_SR = _bool("LEVEL_FILTER_INCLUDE_INDEX_SR", True)

# ===========================================================================
# Live SR levels engine (pivot-based, runs on live 1-min candles)
#
# LIVE_SR_LEFT_BARS / LIVE_SR_RIGHT_BARS control the DISPLAY window
# (chart, /isolated SR Tracking card, Telegram). Default 15/3 matches
# the ``t_feed_v1-market-engine-v3`` trigger window.
# ===========================================================================
# S/R levels are calculated by the external EMA V2 service. This project
# consumes the push feed and does not run its own pivot detector.
LIVE_SR_ENABLED = False
SR_BACKFILL_ENABLED = False
LIVE_SR_REFRESH_FROM_API_ENABLED = False
EXTERNAL_SR_WS_ENABLED = _bool("EXTERNAL_SR_WS_ENABLED", True)
EXTERNAL_SR_WS_URL = _str("EXTERNAL_SR_WS_URL", "wss://ema.novag7.in")
EXTERNAL_SR_STALE_SECONDS = max(60, _int("EXTERNAL_SR_STALE_SECONDS", 180))
EXTERNAL_SR_LOG_DIR = PROJECT_ROOT / _str("EXTERNAL_SR_LOG_DIR", "data/live_sr")
ISOLATED_PAIR_SR_MINUTE_ENABLED = False  # external EMA V2 owns all S/R refreshes
LIVE_SR_TRACK_ALL_INSTRUMENTS = _bool("LIVE_SR_TRACK_ALL_INSTRUMENTS", True)
LIVE_SR_LEFT_BARS = max(1, _int("LIVE_SR_LEFT_BARS", 15))
LIVE_SR_RIGHT_BARS = max(1, _int("LIVE_SR_RIGHT_BARS", 3))
LIVE_SR_MAX_CANDLES = max(50, _int("LIVE_SR_MAX_CANDLES", 500))
LIVE_SR_FILENAME = _str("LIVE_SR_FILENAME", "live_sr_levels.json")
LIVE_SR_PERSIST_EVERY_N_CANDLES = max(0, _int("LIVE_SR_PERSIST_EVERY_N_CANDLES", 5))

# ===========================================================================
# Mid-day startup backfill via intraday API
# ===========================================================================
SR_BACKFILL_ENABLED = False  # local pivot backfill removed; EMA V2 supplies S/R
SR_BACKFILL_MAX_WORKERS = max(1, min(16, _int("SR_BACKFILL_MAX_WORKERS", 4)))
SR_BACKFILL_START_HOUR = _int("SR_BACKFILL_START_HOUR", 9)
SR_BACKFILL_START_MINUTE = _int("SR_BACKFILL_START_MINUTE", 15)
SR_BACKFILL_END_HOUR = _int("SR_BACKFILL_END_HOUR", 23)
SR_BACKFILL_END_MINUTE = _int("SR_BACKFILL_END_MINUTE", 59)

# ===========================================================================
# G1 v3.2 — MULTI-INDEX BOUNDARY SURVEILLANCE
# ===========================================================================
STRATEGY_MONITORED_INDICES = _csv_or_json_list(
    "STRATEGY_MONITORED_INDICES",
    "NSE_INDEX|Nifty 50,"
    "NSE_INDEX|Nifty Bank,"
    "NSE_INDEX|NIFTY MID SELECT,"
    "BSE_INDEX|SENSEX",
)

STRATEGY_TOUCH_PRIORITY_ORDER = _csv_or_json_list(
    "STRATEGY_TOUCH_PRIORITY_ORDER",
    "MIDCPNIFTY,BANKNIFTY,NIFTY,SENSEX",
)

STRATEGY_ROUTED_EXECUTION_INDEX = _str(
    "STRATEGY_ROUTED_EXECUTION_INDEX", "NSE_INDEX|Nifty 50"
)

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
LEVEL_ISOLATION_ENABLED = _bool("LEVEL_ISOLATION_ENABLED", True)

LEVEL_ISOLATION_FILENAME = _str(
    "LEVEL_ISOLATION_FILENAME", "level_isolated_instrument.json"
)

# ---- G1 v4.1 hotfix — trigger-side → bias-side mapping ---------------- #
STRATEGY_RELATIONAL_R3_BIAS = _str(
    "STRATEGY_RELATIONAL_R3_BIAS", "CE"
).upper()
STRATEGY_RELATIONAL_S3_BIAS = _str(
    "STRATEGY_RELATIONAL_S3_BIAS", "PE"
).upper()

LEVEL_ISOLATION_CHECK_INTERVAL_SECONDS = max(
    0.5, _float("LEVEL_ISOLATION_CHECK_INTERVAL_SECONDS", 2.0)
)

LEVEL_ISOLATION_NIFTY_TICK_STALE_SECONDS = max(
    1, _int("LEVEL_ISOLATION_NIFTY_TICK_STALE_SECONDS", 30)
)

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
LEVEL_ISOLATION_REPLAY_USE_SERIES_OR = _bool(
    "LEVEL_ISOLATION_REPLAY_USE_SERIES_OR", True
)
LEVEL_ISOLATION_OR_SELF_HEAL_VERIFY_HL = _bool(
    "LEVEL_ISOLATION_OR_SELF_HEAL_VERIFY_HL", True
)

# ===========================================================================
# ISOLATED SR TRACKER — watch SR confirmations for the isolated instrument
# ===========================================================================
LEVEL_ISOLATION_SR_FILENAME = _str(
    "LEVEL_ISOLATION_SR_FILENAME", "level_isolated_instrument_sr.json"
)

LEVEL_ISOLATION_SR_TELEGRAM_ENABLED = _bool(
    "LEVEL_ISOLATION_SR_TELEGRAM_ENABLED", True
)

# ===========================================================================
# LIVE SR CALC — on-demand stateless SR computation
# ===========================================================================
LIVE_SR_CALC_DEFAULT_LOOKBACK_DAYS = max(1, min(30, _int(
    "LIVE_SR_CALC_DEFAULT_LOOKBACK_DAYS", 5
)))

LIVE_SR_CALC_MAX_CANDLES = max(100, min(10000, _int(
    "LIVE_SR_CALC_MAX_CANDLES", 3000
)))

# ===========================================================================
# LIVE SR — accuracy tuning
# ===========================================================================
SR_BACKFILL_INCLUDE_HISTORICAL = _bool("SR_BACKFILL_INCLUDE_HISTORICAL", True)

LIVE_SR_REFRESH_FROM_API_ENABLED = _bool(
    "LIVE_SR_REFRESH_FROM_API_ENABLED", True
)

LIVE_SR_REFRESH_FROM_API_MINUTES = max(
    1, min(60, _int("LIVE_SR_REFRESH_FROM_API_MINUTES", 2))
)

# ===========================================================================
# STRATEGY ENTRY SIGNAL — G1 v3.2 (fast pivot + LTP-tier stop + auto-reverse)
# ===========================================================================
# Used by ``services/strategy_entry_signal_service.py`` for:
#   * the /simulation replay diagnostics, and
#   * the still-exposed GET /admin/level-isolation/strategy-signal view.
#
# NOTE (post order-strategy rewrite): strategy_entry_signal no longer
# drives ORDER PLACEMENT. Order placement lives entirely in
# ``services/dummy_order_service.py`` and follows the
# ``t_feed_v1-market-engine-v3`` spec (see STRATEGY ORDER block at the
# bottom of this file). These keys are retained because the diagnostic
# consumer still reads them.
# ===========================================================================
STRATEGY_ENTRY_ENABLED = _bool("STRATEGY_ENTRY_ENABLED", True)

STRATEGY_ENTRY_ALLOW_CROSSOVER = _bool(
    "STRATEGY_ENTRY_ALLOW_CROSSOVER", True
)

STRATEGY_ENTRY_BUFFER_MINUTES = max(
    0, _int("STRATEGY_ENTRY_BUFFER_MINUTES", 10)
)

# NOTE: STRATEGY_ENTRY_INCLUDE_PRETOUCH was removed in G1 v4.1.
# NOTE: STRATEGY_ENTRY_ONLY_ONCE_PER_DAY was removed in the same pass.
# NOTE: STRATEGY_ENTRY_FIRST_TO_CONFIRM_WINS was removed in the same pass.
# NOTE: STRATEGY_ENTRY_PRICE_MODE was removed in G1 v2.

STRATEGY_ENTRY_BLOCKED_BY_RESISTANCE = _bool(
    "STRATEGY_ENTRY_BLOCKED_BY_RESISTANCE", True
)

STRATEGY_ENTRY_ALLOW_AUTO_REVERSE = _bool(
    "STRATEGY_ENTRY_ALLOW_AUTO_REVERSE", True
)

STRATEGY_ENTRY_PRIORITIZE_SUPPORT = _bool(
    "STRATEGY_ENTRY_PRIORITIZE_SUPPORT", True
)

STRATEGY_ENTRY_PIVOT_LEFT_BARS = max(
    1, _int("STRATEGY_ENTRY_PIVOT_LEFT_BARS", LIVE_SR_LEFT_BARS)
)
STRATEGY_ENTRY_PIVOT_RIGHT_BARS = max(
    1, min(10, _int("STRATEGY_ENTRY_PIVOT_RIGHT_BARS", 3))
)

STRATEGY_ENTRY_STOP_LTP_TIERS = _str(
    "STRATEGY_ENTRY_STOP_LTP_TIERS",
    '[{"max_ltp":100,"sl_pct":0.15},'
    '{"max_ltp":200,"sl_pct":0.12},'
    '{"max_ltp":300,"sl_pct":0.10},'
    '{"max_ltp":null,"sl_pct":0.05}]',
)

STRATEGY_ENTRY_STOP_TICK_ROUNDING = _str(
    "STRATEGY_ENTRY_STOP_TICK_ROUNDING", "down"
).lower()

STRATEGY_MAX_STOP_PCT = _float("STRATEGY_MAX_STOP_PCT", 0.35)

# ---- G1 v3.2 — Promotion (DORMANT after order-strategy rewrite) -------- #
# These two keys are read only inside
# ``isolated_instrument_promotion_service.apply_promotion()``. That
# method is no longer called from any task after the 2026-11-02 rewrite
# (``_try_promote()`` was removed from ``level_isolated_instrument_task``).
# They are kept for backward-compat so a lingering admin route that
# imports the module still works without a NameError.
STRATEGY_ENTRY_ALLOW_PROMOTION = _bool(
    "STRATEGY_ENTRY_ALLOW_PROMOTION", True
)
STRATEGY_ENTRY_ALLOW_RE_PROMOTION = _bool(
    "STRATEGY_ENTRY_ALLOW_RE_PROMOTION", False
)

# ---- G1 v4.1 — Entry-signal correctness (§5.A / §5.B / §5.C / §5.E) ---- #
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
# DIAGNOSTIC ONLY. The /simulation endpoint still calls
# ``strategy_entry_signal_service.compute()`` and replays the two legacy
# scenarios (signal_v2 and regime). It is **not** the order-placement
# path — that is ``services/dummy_order_service.py`` (see STRATEGY ORDER
# at the bottom of this file).
# ===========================================================================
SIMULATION_ENABLED = _bool("SIMULATION_ENABLED", True)

SIMULATION_USE_REGIME_TARGETS = _bool(
    "SIMULATION_USE_REGIME_TARGETS", True
)

SIMULATION_STOP_MODE_HIGH = _str(
    "SIMULATION_STOP_MODE_HIGH", "tighter"
).lower()

SIMULATION_SAME_BAR_STOP_FIRST = _bool(
    "SIMULATION_SAME_BAR_STOP_FIRST", True
)

SIMULATION_TICK_ROUNDING = _str(
    "SIMULATION_TICK_ROUNDING", "nearest"
).lower()

# ===========================================================================
# DUMMY ORDERS — t_feed_v1-market-engine-v3 (post-isolation execution)
# ===========================================================================
# The scheduler fires ``dummy_order_service.run_once()`` every
# DUMMY_ORDER_TRACK_INTERVAL_SECONDS. On each tick the service:
#
#   1. Fetches the isolation record (bias-side instrument + T_iso).
#   2. Waits for the FIRST 15/3 SR level on the bias-side with
#      pivot_time > T_iso AND confirmed_time > T_iso.
#   3. Resolves routing (same leg / opposite leg / 09:30 reversal)
#      from the actual post-isolation 15/3 S/R confirmation.
#   4. Creates the order without OR-grid price eligibility filters.
#   5. Applies the configured stop-loss, dynamic target milestone,
#      and post-target 15/3 Resistance exit engine.
#   6. Auto-closes any surviving OPEN order at
#      DUMMY_ORDER_EOD_HOUR:MINUTE.
#
# Every record is labelled "dummy" — no real Upstox order is ever
# placed by this service.
# ===========================================================================
DUMMY_ORDER_ENABLED = _bool("DUMMY_ORDER_ENABLED", True)

# Scheduler tick, in seconds. The service is safe to fire all day — it
# no-ops outside the live window.
DUMMY_ORDER_TRACK_INTERVAL_SECONDS = max(
    5, _int("DUMMY_ORDER_TRACK_INTERVAL_SECONDS", 30)
)

# Live-tracking window (IST). Order creation is only attempted after
# DUMMY_ORDER_START_HOUR:MINUTE.
DUMMY_ORDER_START_HOUR = _int("DUMMY_ORDER_START_HOUR", 9)
DUMMY_ORDER_START_MINUTE = _int("DUMMY_ORDER_START_MINUTE", 15)

# After the official 15:30 IST market close, startup/tick catch-up
# enters "retroactive" mode and reconstructs today's dummy-order lifecycle
# from the persisted candle/SR series. This is intentionally separate from
# the 15:35 order EOD processing cutoff below.
DUMMY_ORDER_RETROACTIVE_START_HOUR = _int(
    "DUMMY_ORDER_RETROACTIVE_START_HOUR", 15
)
DUMMY_ORDER_RETROACTIVE_START_MINUTE = _int(
    "DUMMY_ORDER_RETROACTIVE_START_MINUTE", 30
)

# At/after this time, any surviving OPEN dummy order is force-closed at
# the final available candle close. Keep this at 15:35 IST.
DUMMY_ORDER_EOD_HOUR = _int("DUMMY_ORDER_EOD_HOUR", 15)
DUMMY_ORDER_EOD_MINUTE = _int("DUMMY_ORDER_EOD_MINUTE", 35)

# Persistence filename under LIVE_DATA_DIR.
DUMMY_ORDER_FILENAME = _str(
    "DUMMY_ORDER_FILENAME", "dummy_orders.json"
)

# When True (default), each dummy order fires exactly TWO Telegram
# messages over its lifetime — one on creation, one on close. SKIPPED
# records fire a single WARNING message.
DUMMY_ORDER_TELEGRAM_ENABLED = _bool("DUMMY_ORDER_TELEGRAM_ENABLED", True)

# ---- Deterministic replay / safety guards (2026-11-04) ----------------- #
# These flags make the post-isolation execution pipeline auditable. All
# default to the SAFE DETERMINISTIC behaviour — no .env change required.
#
#   DUMMY_ORDER_ALLOW_LTP_ENTRY_FALLBACK
#       When True, the service may fall back to the live LTP when no
#       deterministic SR-based entry price can be resolved. This
#       restores the pre-2026-11-04 behaviour and is intended for
#       DEBUGGING ONLY — it undermines reproducibility.
#       Default: False.
#
#   DUMMY_ORDER_STRICT_DETERMINISTIC_REPLAY
#       When True, unresolved entries are SKIPPED (never substituted
#       with LTP). This is the primary "deterministic by default"
#       switch. If both this flag and
#       DUMMY_ORDER_ALLOW_LTP_ENTRY_FALLBACK are set, the strict flag
#       wins (skip-on-unresolved takes precedence).
#       Default: True.
#
#   DUMMY_ORDER_WAIT_FOR_SR_BACKFILL
#       When True, ``run_once()`` waits for the SR backfill to have
#       completed before attempting the first post-isolation trigger.
#       Prevents racing a mid-day restart into an incomplete SR state.
#       Default: True.
#
#   DUMMY_ORDER_WAIT_FOR_SR_BACKFILL_TIMEOUT_SECONDS
#       Hard cap on the wait above. If the backfill is not complete
#       within this window the tick is skipped and the next tick
#       retries. Only meaningful when DUMMY_ORDER_WAIT_FOR_SR_BACKFILL
#       is True.
#       Default: 120 seconds.
#
#   DUMMY_ORDER_DISABLE_RUNTIME_PATH_SWITCH
#       Retained for backward compatibility. Standard path selection is
#       now fixed: all pre-11:30 isolations use PATH_A_UNIFIED regardless
#       of trigger_index. This flag no longer changes execution behavior.
#       Default: True.
#
#   DUMMY_ORDER_DRIFT_GUARD
#       When True, an existing stored entry must NOT be overwritten by
#       a later catch-up recomputation. If a mismatch is detected a
#       WARNING is logged and the original entry is retained. Prevents
#       silent drift when the service restarts mid-day and re-derives
#       an entry from a different candle window.
#       Default: True.
DUMMY_ORDER_ALLOW_LTP_ENTRY_FALLBACK = _bool(
    "DUMMY_ORDER_ALLOW_LTP_ENTRY_FALLBACK", False
)
DUMMY_ORDER_STRICT_DETERMINISTIC_REPLAY = _bool(
    "DUMMY_ORDER_STRICT_DETERMINISTIC_REPLAY", True
)
DUMMY_ORDER_WAIT_FOR_SR_BACKFILL = _bool(
    "DUMMY_ORDER_WAIT_FOR_SR_BACKFILL", True
)
DUMMY_ORDER_WAIT_FOR_SR_BACKFILL_TIMEOUT_SECONDS = max(
    1, _int("DUMMY_ORDER_WAIT_FOR_SR_BACKFILL_TIMEOUT_SECONDS", 120)
)
DUMMY_ORDER_DISABLE_RUNTIME_PATH_SWITCH = _bool(
    "DUMMY_ORDER_DISABLE_RUNTIME_PATH_SWITCH", True
)
DUMMY_ORDER_DRIFT_GUARD = _bool(
    "DUMMY_ORDER_DRIFT_GUARD", True
)

# ---------------------------------------------------------------------------
# DEPRECATION NOTICE — Path A/B runtime switch and STRATEGY_ENTRY_* flags
# ---------------------------------------------------------------------------
# ``STRATEGY_EXT_PATH_B_ENABLED`` is DEPRECATED and is no longer consumed.
# All pre-11:30 isolations use unified standard Path A regardless of the
# isolation record's ``trigger_index``. If .env still sets this variable,
# its value is silently ignored.
#
# The following ``STRATEGY_ENTRY_*`` / ``STRATEGY_MAX_STOP_PCT`` flags
# historically drove the order-placement pipeline. After the 2026-11-02
# rewrite they are DIAGNOSTICS / SIMULATION ONLY — they feed
# ``services/strategy_entry_signal_service.py`` (used by ``/simulation``
# and ``GET /admin/level-isolation/strategy-signal``) but DO NOT
# influence ``services/dummy_order_service.py``:
#
#     STRATEGY_ENTRY_ENABLED
#     STRATEGY_ENTRY_ALLOW_CROSSOVER
#     STRATEGY_ENTRY_BUFFER_MINUTES
#     STRATEGY_ENTRY_BLOCKED_BY_RESISTANCE
#     STRATEGY_ENTRY_ALLOW_AUTO_REVERSE
#     STRATEGY_ENTRY_PRIORITIZE_SUPPORT
#     STRATEGY_ENTRY_PIVOT_LEFT_BARS
#     STRATEGY_ENTRY_PIVOT_RIGHT_BARS
#     STRATEGY_ENTRY_STOP_LTP_TIERS
#     STRATEGY_ENTRY_STOP_TICK_ROUNDING
#     STRATEGY_MAX_STOP_PCT
#     STRATEGY_ENTRY_REGISTER_COUNTER_SIDE
#     STRATEGY_ENTRY_CANDLE_FALLBACK_ENABLED
#     STRATEGY_ENTRY_TARGET_REQUIRE_POST_ENTRY
#     STRATEGY_ENTRY_FREEZE_ENTRY_ON_PROMOTION
#
# Order-placement tunables live exclusively under the ``STRATEGY_ORDER_*``
# and ``DUMMY_ORDER_*`` blocks in this file.

# The following legacy keys were removed in the 2026-11-02 rewrite
# because they were ONLY consumed by the removed dummy-order logic:
#
#   DUMMY_ORDER_CREATE_REGIME_VARIANT      (Path A scenario variants)
#   DUMMY_ORDER_MAX_ORDERS_PER_DAY         (Path A / Path B order cap)
#   DUMMY_ORDER_RECONCILE_MINUTES          (old LTP + bar reconcile cadence)
#   STRATEGY_EXT_PATH_B_ENABLED            (Path B master switch)
#   STRATEGY_EXT_TRACKED_UNDERLYINGS       (Path B underlying allow-list)
#   STRATEGY_EXT_LOT_SIZE_NIFTY            (Path B Rupee math)
#   STRATEGY_EXT_LOT_SIZE_SENSEX           (Path B Rupee math)
#   STRATEGY_EXT_PROFIT_ACTIVATION_RS      (Path B trailing activation)
#   STRATEGY_EXT_TRAIL_GAP_RS              (Path B trailing gap)
#   STRATEGY_EXT_TRAIL_FLOOR_RS            (Path B trailing floor)
#   STRATEGY_EXT_R3_BREAKOUT_ACTIVATION_ENABLED  (Path B alt trigger)
#   STRATEGY_EXT_BEARISH_EXIT_ENABLED      (Path B trailing exit)
#   STRATEGY_EXT_BEARISH_CANDLES           (Path B trailing exit)
#   STRATEGY_EXT_LOCKDOWN_ON_TARGET        (Path B lockdown)
#   STRATEGY_TOUCH_POLL_INTERVAL_SEC       (old polling cadence)
#   STRATEGY_LOT_SIZE_MAP                  (derived; only Path B read it)
#   STRATEGY_EXT_TRACKED_UNDERLYINGS_SET   (derived; only Path B read it)
#
# If your .env still sets any of these, the values are silently ignored
# (the ``os.getenv`` default is used for anything still referenced).

# ===========================================================================
# STRATEGY ORDER — unified Path A
# ===========================================================================
# Standard-session tunables consumed by services/dummy_order_service.py:
#   * Stop-loss: configured percentage below entry (default 30%).
#   * Target milestone: fixed entry-price-band formula described above.
#   * Exit: first new 15/3 Resistance confirmation after the milestone.
#   * Reversal cutoff: 09:30 IST backup-routing rule remains unchanged.
#
# OR-grid levels are not consulted for entry eligibility, stop-loss,
# target, or exit calculations.
# ===========================================================================

# Fixed risk cap: stop = P_entry × (1 − STRATEGY_ORDER_STOP_LOSS_PCT).
# Default 0.30 = spec's 30 %.
STRATEGY_ORDER_STOP_LOSS_PCT = _float(
    "STRATEGY_ORDER_STOP_LOSS_PCT", 0.30
)

# Live-entry filters kept in parity with backtest_order_strategy.py.
# STANDARD 09:30 backup reversal: PE bias is suppressed by default;
# CE bias requires a rising isolated-leg EMA over the configured lookback.
STRATEGY_ORDER_BACKUP_PE_SUPPRESS = _bool(
    "STRATEGY_ORDER_BACKUP_PE_SUPPRESS", True
)
STRATEGY_ORDER_BACKUP_CE_REQUIRE_POSITIVE_EMA_SLOPE = _bool(
    "STRATEGY_ORDER_BACKUP_CE_REQUIRE_POSITIVE_EMA_SLOPE", True
)
STRATEGY_ORDER_BACKUP_CE_EMA_PERIOD = max(
    1, _int("STRATEGY_ORDER_BACKUP_CE_EMA_PERIOD", 9)
)
STRATEGY_ORDER_BACKUP_CE_EMA_SLOPE_LOOKBACK = max(
    1, _int("STRATEGY_ORDER_BACKUP_CE_EMA_SLOPE_LOOKBACK", 2)
)

# EARLY_EXTREME_REVERSAL same-leg (Resistance -> isolated leg) premium gate.
# Inclusive bounds: ₹80 <= routed entry premium <= ₹100 by default.
STRATEGY_ORDER_EER_SAME_LEG_PREMIUM_BAND_ENABLED = _bool(
    "STRATEGY_ORDER_EER_SAME_LEG_PREMIUM_BAND_ENABLED", True
)
STRATEGY_ORDER_EER_SAME_LEG_PREMIUM_MIN = _float(
    "STRATEGY_ORDER_EER_SAME_LEG_PREMIUM_MIN", 80.0
)
STRATEGY_ORDER_EER_SAME_LEG_PREMIUM_MAX = _float(
    "STRATEGY_ORDER_EER_SAME_LEG_PREMIUM_MAX", 100.0
)

# Standard unified Path A dynamic target is a fixed rule implemented
# in services/dummy_order_service.py; the entry-price band calculation
# is intentionally not controlled by an environment flag.

# 09:30 IST backup-reversal cutoff (spec §2.2).
STRATEGY_ORDER_REVERSAL_CUTOFF_HOUR = _int(
    "STRATEGY_ORDER_REVERSAL_CUTOFF_HOUR", 9
)
STRATEGY_ORDER_REVERSAL_CUTOFF_MINUTE = _int(
    "STRATEGY_ORDER_REVERSAL_CUTOFF_MINUTE", 30
)

# ===========================================================================
# POST-11:30 AM ISOLATED INSTRUMENT STRATEGY
# ===========================================================================
# Companion strategy that only arms AFTER 11:30 IST. It re-uses the
# same isolated-instrument record produced by LEVEL ISOLATION, but
# applies a WIDER confirmation window on entry and a NARROWER window
# on exit, with a straight-line +50 pt target and a fixed 30 % stop.
#
#   * Entry confirmation:  15/30 SR (wide right-edge — fewer false
#                          triggers after the midday chop).
#   * Exit  confirmation:  15/15 SR (symmetric — fast reaction on the
#                          post-entry resistance that closes the trade).
#   * Buffer:              up to 10 minutes before the isolated_time a
#                          pivot may still be counted as the trigger.
#   * Target:              +50 points from fill.
#   * Stop-loss:           30 % below fill (same convention as
#                          STRATEGY_ORDER_STOP_LOSS_PCT).
#
# NOTE: values are intentionally left as plain literals (not env
# driven) for v1 of this strategy — they will be promoted to
# ``_int`` / ``_float`` loaders once the replay harness stabilises.
# ===========================================================================

# Hard cutoff — the strategy is inert before this wall-clock time (IST).
POST_1130_REGIME_TIME = "11:30:00"

# (left_bars, right_bars) for the ENTRY pivot window.
POST_1130_ENTRY_SR_CONFIG = (15, 30)

# (left_bars, right_bars) for the EXIT pivot window.
POST_1130_EXIT_SR_CONFIG = (15, 15)

# Allow a confirmation up to this many minutes PRIOR to isolated_time
# to still count as the entry trigger.
POST_1130_BUFFER_MINUTES = 10

# Profit target, in index points, from the fill price.
POST_1130_TARGET_POINTS = 50.0

# Stop-loss fraction below fill (0.30 = 30 %).
POST_1130_STOPLOSS_PERCENT = 0.30

# ===========================================================================
# SANDBOX EXECUTION — live sandbox order mirror of dummy-order transitions
# ===========================================================================
# Driven by ``services/sandbox_execution_service.py`` (2026-11-04).
#
# The sandbox executor consumes ``dummy_order_service``'s post-persistence
# transition callback (see ``dummy_order_service.set_transition_callback``)
# and mirrors each transition onto a real broker order — but ONLY when
# EVERY one of the following gates passes:
#
#     1. reason == "scheduled"            (startup catch-up is ignored)
#     2. SANDBOX_EXECUTION_ENABLED        (this block, default False)
#     3. SANDBOX_EXECUTION_MODE=="live"   (this block, default "observe")
#     4. order_runtime_service.status()["enabled"] == True
#        (POST /admin/orders/runtime-enable / runtime-disable)
#     5. sr_backfill_service.is_ready_for_today() == True
#     6. current time is inside [SANDBOX_MARKET_OPEN, SANDBOX_MARKET_CLOSE]
#     7. ledger idempotency key has not already fired
#     8. single-position lock is free
#
# TRANSLATION TABLE:
#
#     dummy transition                → sandbox action
#     --------------------------------→--------------------------------
#     CREATED                         → place_order(BUY, MARKET, product=D)
#     STAGE_2_MILESTONE               → log only (no broker action)
#     STOP_HIT                        → exit_all_positions(dry_run=False)
#     STAGE_2_RESISTANCE_EXIT         → exit_all_positions(dry_run=False)
#     NEXT_15_3_RESISTANCE_EXIT       → exit_all_positions(dry_run=False)
#     EOD_CLOSE                       → exit_all_positions(dry_run=False),
#                                       unless the sandbox already exited
#                                       at 15:15 via force_eod_exit()
#
# TWO ENGINEERED DIVERGENCES from the dummy engine:
#
#   (Bug A) Stop-loss latency.
#     Dummy STOP_HIT is detected on the 30 s scheduler tick. When the
#     sandbox mirrors it as an `exit_all` call, the effective stop is a
#     30 s-latency market exit, not a resting SL order. Accepted for v1;
#     the executor records `entry/exit_latency_ms` and `fill_slippage_pct`
#     on every transition so the divergence can be quantified before
#     deciding whether to add a resting SL-M order later.
#
#   (Bug B) EOD ownership.
#     The dummy engine closes at DUMMY_ORDER_EOD_HOUR:MINUTE (default
#     15:35). That is TOO LATE for real execution — the broker auto-
#     squares-off intraday F&O positions ~15:15-15:25 IST. So the
#     sandbox runs its OWN EOD at SANDBOX_EOD_HOUR:SANDBOX_EOD_MINUTE
#     (default 15:15) via the `sandbox_eod_exit` scheduler job. The
#     later 15:35 dummy EOD_CLOSE then falls into the executor's
#     "already exited" no-op branch and is logged as the expected
#     divergence between the two engines.
#
# DEFAULT DEPLOYMENT IS INERT:
#     SANDBOX_EXECUTION_ENABLED=false + SANDBOX_EXECUTION_MODE="observe"
#     means the executor evaluates every gate, logs every decision, and
#     makes ZERO broker calls. To actually place live orders you must
#     BOTH flip the env gate (this block) AND call
#     POST /admin/orders/runtime-enable after startup — one switch
#     cannot override the other. That is deliberate; see the kill-switch
#     note in routes/admin_orders.py.
# ===========================================================================

# Master gate for live sandbox execution.
# Default FALSE (safety — no broker calls until an operator opts in).
# For a live-sandbox testing deployment set SANDBOX_EXECUTION_ENABLED=true
# in .env AND also POST /admin/orders/runtime-enable after startup.
SANDBOX_EXECUTION_ENABLED = _bool("SANDBOX_EXECUTION_ENABLED", True)

# Execution mode:
#   "observe" — evaluate every gate, log the decision, place NO orders.
#   "live"    — place real orders through place_order / exit_all_positions.
# Any other value is normalised to "observe" by the executor.
# Default "observe" — a deployment that accidentally sets only
# SANDBOX_EXECUTION_ENABLED=true will still not place orders until the
# mode is explicitly flipped.
SANDBOX_EXECUTION_MODE = _str("SANDBOX_EXECUTION_MODE", "live").lower()

# Number of lots per sandbox entry. The place_order call takes this many
# lots of the option contract. For test deployments use 1 — you do not
# want to discover the notional of one MIDCPNIFTY lot with real money.
SANDBOX_LOTS = max(1, _int("SANDBOX_LOTS", 1))

# Sandbox-owned end-of-day exit (Bug B fix). Must be strictly EARLIER
# than both:
#   * the broker's intraday auto-square-off (~15:15-15:25 IST), and
#   * DUMMY_ORDER_EOD_HOUR:DUMMY_ORDER_EOD_MINUTE (default 15:35).
# Fired by the `sandbox_eod_exit` job in services/scheduler_service.py.
SANDBOX_EOD_HOUR = _int("SANDBOX_EOD_HOUR", 15)
SANDBOX_EOD_MINUTE = _int("SANDBOX_EOD_MINUTE", 15)

# Persistence path for the sandbox ledger. Separate file from
# data/dummy_orders.json — the dummy state is governed by the
# determinism contract and DUMMY_ORDER_DRIFT_GUARD, and mixing
# sandbox state into it would violate both.
#
# Accepts a relative path (resolved by the executor via Path(...) which
# anchors to the process cwd) or an absolute path. Matches the value
# the executor reads from os.getenv today — kept here for consistency
# and admin-route visibility.
SANDBOX_LEDGER_PATH = _str(
    "SANDBOX_LEDGER_PATH", "data/sandbox_orders.json"
)

# Retry policy for exit_all failures. On each failure the executor
# waits SANDBOX_EXIT_RETRY_BACKOFF_SECONDS * attempt (attempt=1,2,...)
# then retries. After SANDBOX_EXIT_RETRY_ATTEMPTS consecutive failures
# the executor sets sandbox_exit_stuck=true and blocks all new entries.
SANDBOX_EXIT_RETRY_ATTEMPTS = max(
    1, _int("SANDBOX_EXIT_RETRY_ATTEMPTS", 3)
)
SANDBOX_EXIT_RETRY_BACKOFF_SECONDS = max(
    0.0, _float("SANDBOX_EXIT_RETRY_BACKOFF_SECONDS", 1.5)
)

# Entry-fill confirmation polling. After place_order returns ok, the
# executor polls get_order_details every SANDBOX_FILL_POLL_SECONDS
# until filled_quantity > 0 or SANDBOX_FILL_TIMEOUT_SECONDS elapses.
# The entry is only marked ENTERED after a confirmed fill; timeout →
# ENTRY_FAILED (no phantom exit will be scheduled).
SANDBOX_FILL_TIMEOUT_SECONDS = max(
    1.0, _float("SANDBOX_FILL_TIMEOUT_SECONDS", 15.0)
)
SANDBOX_FILL_POLL_SECONDS = max(
    0.1, _float("SANDBOX_FILL_POLL_SECONDS", 1.0)
)

# When True, the sandbox executor appends its own Telegram messages on
# each action (placed / exited / failed / skipped-by-gate). Same channel
# as DUMMY_ORDER_TELEGRAM_ENABLED so operators see the two engines side
# by side. Default True for parity with the dummy engine.
SANDBOX_TELEGRAM_ENABLED = _bool("SANDBOX_TELEGRAM_ENABLED", True)

# ===========================================================================
# EMA CALC — on-demand stateless EMA-cross calculation
# ===========================================================================
EMA_CALC_DEFAULT_LOOKBACK_DAYS = max(1, min(30, _int(
    "EMA_CALC_DEFAULT_LOOKBACK_DAYS", 11
)))

EMA_CALC_MAX_CANDLES = max(100, min(10000, _int(
    "EMA_CALC_MAX_CANDLES", 3000
)))

# ---------------------------------------------------------------------------
# Admin / API authentication
# ---------------------------------------------------------------------------
# Shared secret for machine clients (curl, CI, internal tools).
# Send as either:
#     X-API-Key: <ADMIN_API_KEY>
#     Authorization: Bearer <ADMIN_API_KEY>
#
# Leave EMPTY to disable header auth entirely.
# Generate one with:
#     python -c "import secrets; print(secrets.token_urlsafe(48))"
ADMIN_API_KEY = _str("ADMIN_API_KEY", "")

# HTTP Basic credentials — for humans opening /docs, /logs, /admin in a
# browser. Leave ADMIN_BASIC_PASSWORD empty to disable Basic auth.
ADMIN_BASIC_USER = _str("ADMIN_BASIC_USER", "admin")
ADMIN_BASIC_PASSWORD = _str("ADMIN_BASIC_PASSWORD", "")

# CORS: comma-separated list of allowed origins.
# Default "*" is only safe for a fully local, isolated box. In production
# set your real UI origin(s), e.g.:
#     CORS_ALLOW_ORIGINS=https://ui.example.com,https://admin.example.com
CORS_ALLOW_ORIGINS = _csv("CORS_ALLOW_ORIGINS", "*")

# Cookie-session for browser logins (see core/auth.py)
ADMIN_SESSION_SECRET = _str("ADMIN_SESSION_SECRET", "")
ADMIN_SESSION_TTL_SECONDS = max(300, _int("ADMIN_SESSION_TTL_SECONDS", 8 * 3600))
ADMIN_COOKIE_NAME = _str("ADMIN_COOKIE_NAME", "novag_admin")
ADMIN_COOKIE_SECURE = _bool("ADMIN_COOKIE_SECURE", True)  # require HTTPS
PROD_MODE = _bool("PROD_MODE", True)  # disable debug / reload / dev-only features