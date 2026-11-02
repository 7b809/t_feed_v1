# core/config.py
"""Configuration for the foundational market-data application.

Order strategy rewrite (2026-11-02)
-----------------------------------
The previous dummy-order placement logic (G1 v4.1 — Path A / Path B,
Rupee-trailing, promotion-based counter-side order creation) has been
**completely removed**. ``services/dummy_order_service.py`` now
implements the ``t_feed_v1-market-engine-v3`` spec:

    1. First post-isolation 15/3 SR confirmation on the bias-side
       instrument triggers the order (``pivot_time > T_iso`` AND
       ``confirmed_time > T_iso``).
    2. 09:30 IST backup reversal — if a pre-``T_iso`` support was
       already confirmed AND the current post-isolation level is a
       resistance on or before 09:30, buy the same isolated leg.
       Otherwise Support → same leg, Resistance → opposite leg.
    3. Regime validation via the traded instrument's OWN OR grid:
           P_entry > R2                       → SKIP
           P_entry < S2                       → OR Low / +50 pt gain
           S2 ≤ P_entry < R1                  → R2, R3_Threshold, R3
           R1 ≤ P_entry ≤ R2 (Zone 2)         → R3_Threshold, R3
    4. Stop-loss = P_entry × (1 − 0.30) — fixed 30 % below fill.
    5. Stage 1: milestone touch OR ≥50 pt gain.
       Stage 2: exit at the NEXT 15/3 resistance confirmation close.

All the ``STRATEGY_EXT_*`` keys (Path B, Rupee trailing, R3-breakout
activation, bearish exit, lockdown-on-target, lot-size map, tracked
underlyings, polling cadence) that were only consumed by the removed
logic have been deleted. The replacement section (``STRATEGY_ORDER_*``
and the reduced ``DUMMY_ORDER_*`` set) is at the bottom of this file.

The promotion service (``services/isolated_instrument_promotion_service
.py``) is now **dormant** — no code path calls ``apply_promotion()``.
Its two config keys (``STRATEGY_ENTRY_ALLOW_PROMOTION``,
``STRATEGY_ENTRY_ALLOW_RE_PROMOTION``) are kept for backward-compat in
case an admin route still references the module, but they are inert.

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
analysis. All default to ``True`` — the fixes are enabled out of the
box and no ``.env`` change is required.

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

A fourth flag, ``STRATEGY_ENTRY_BLOCKED_BY_RESISTANCE``, remains
defined because it **is** read by ``_arm_bias_on_support()`` — it
gates the fast-window resistance blocker.
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
#   3. Resolves the routing (same leg / opposite leg / 09:30 reversal)
#      and the traded instrument's OWN OR grid (first_candle.json).
#   4. Creates EXACTLY ONE order per day (or a SKIPPED record if the
#      regime rejects: P_entry > R2, or missing OR levels).
#   5. Applies the 30 % stop-loss and the two-stage milestone engine.
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

# After DUMMY_ORDER_EOD_HOUR:MINUTE the service falls into "retroactive"
# mode and force-closes any surviving OPEN order at the last bar close.
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
# STRATEGY ORDER — t_feed_v1-market-engine-v3
# ===========================================================================
# These are the tunables consumed by ``services/dummy_order_service.py``.
# Read from .env if present, otherwise the defaults match the spec:
#
#   * Stop-loss:        30 % below fill price.
#   * Stage 2 trigger:  +50 pt net gain (in addition to any structural
#                       milestone defined by the OR grid).
#   * Reversal cutoff:  09:30:00 IST — post-isolation confirmations on
#                       or before this time can revert to the SAME leg
#                       when a prior support was already confirmed and
#                       the current level is a resistance.
#
# Note: the trigger window itself is 15/3 and is governed by
# LIVE_SR_LEFT_BARS / LIVE_SR_RIGHT_BARS (both default to 15 / 3, which
# already matches the spec).
# ===========================================================================

# Fixed risk cap: stop = P_entry × (1 − STRATEGY_ORDER_STOP_LOSS_PCT).
# Default 0.30 = spec's 30 %.
STRATEGY_ORDER_STOP_LOSS_PCT = _float(
    "STRATEGY_ORDER_STOP_LOSS_PCT", 0.30
)

# Stage 1 → Stage 2 activation when net gain ≥ N points.
# Default 50.0 = spec's "+50 pt gain".
STRATEGY_ORDER_MILESTONE_GAIN_POINTS = _float(
    "STRATEGY_ORDER_MILESTONE_GAIN_POINTS", 50.0
)

# 09:30 IST backup-reversal cutoff (spec §2.2).
STRATEGY_ORDER_REVERSAL_CUTOFF_HOUR = _int(
    "STRATEGY_ORDER_REVERSAL_CUTOFF_HOUR", 9
)
STRATEGY_ORDER_REVERSAL_CUTOFF_MINUTE = _int(
    "STRATEGY_ORDER_REVERSAL_CUTOFF_MINUTE", 30
)

# ===========================================================================
# EMA CALC — on-demand stateless EMA-cross calculation
# ===========================================================================
EMA_CALC_DEFAULT_LOOKBACK_DAYS = max(1, min(30, _int(
    "EMA_CALC_DEFAULT_LOOKBACK_DAYS", 11
)))

EMA_CALC_MAX_CANDLES = max(100, min(10000, _int(
    "EMA_CALC_MAX_CANDLES", 3000
)))