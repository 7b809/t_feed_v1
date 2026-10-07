import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()


# ===========================================================================
# ENV HELPERS (kept for backward compatibility)
# ===========================================================================
def get_string(name, default=""):
    return os.getenv(name, default)


def get_int(name, default):
    return int(os.getenv(name, str(default)))


def get_float(name, default):
    return float(os.getenv(name, str(default)))


def get_bool(name, default=False):
    default_value = "true" if default else "false"
    return os.getenv(name, default_value).strip().lower() == "true"


def get_string_list(name, default="", uppercase=False):
    values = [
        value.strip() for value in os.getenv(name, default).split(",") if value.strip()
    ]
    if uppercase:
        return [value.upper() for value in values]
    return values


def get_int_list(name, default=""):
    return [
        int(value.strip())
        for value in os.getenv(name, default).split(",")
        if value.strip()
    ]


# ===========================================================================
# CONFIG FILE LOADING
# ===========================================================================
# All settings are loaded from a JSON file. Resolution order for the path:
#   1) env APP_CONFIG_FILE
#   2) <project_root>/config/app_config.json
#   3) <project_root>/config.json
#
# Environment variables still take precedence over the JSON file (so secrets
# like MONGO_URL / TELEGRAM_BOT_TOKEN can stay in .env).
# ===========================================================================

_DEFAULT_CONFIG_FILENAMES = (
    "config/app_config.json",
    "config.json",
)


def _resolve_config_path() -> Path:
    """
    Resolves the JSON config file path.
    """
    env_path = os.getenv("APP_CONFIG_FILE", "").strip()
    if env_path:
        return Path(env_path).expanduser()

    for filename in _DEFAULT_CONFIG_FILENAMES:
        candidate = Path(filename)
        if candidate.exists():
            return candidate

    here = Path(__file__).resolve()
    for parent in here.parents:
        for filename in _DEFAULT_CONFIG_FILENAMES:
            candidate = parent / filename
            if candidate.exists():
                return candidate

    return Path(_DEFAULT_CONFIG_FILENAMES[0])


def _load_json_config() -> dict[str, Any]:
    """
    Loads the JSON config file. Returns an empty dict on any failure.
    """
    path = _resolve_config_path()
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                return loaded
    except Exception:
        # Config module must never crash on import; fail silently and use
        # env + defaults instead.
        pass
    return {}


_JSON_CONFIG: dict[str, Any] = _load_json_config()


def _cfg(key: str, env_key: str | None = None, default: Any = None) -> Any:
    """
    Reads a value using this priority:
        1) environment variable (env_key or key)
        2) JSON config file
        3) provided default

    Nested JSON keys are supported via dot notation, e.g. "mongo.uri".
    """
    # 1) env
    lookup_env = env_key or key
    env_value = os.getenv(lookup_env, None)
    if env_value is not None and str(env_value).strip() != "":
        return env_value

    # 2) JSON (dot notation)
    node: Any = _JSON_CONFIG
    for part in str(key).split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            node = None
            break

    if node is not None:
        return node

    # 3) default
    return default


def _cfg_str(key: str, env_key: str | None = None, default: str = "") -> str:
    value = _cfg(key, env_key, default)
    if value is None:
        return default
    return str(value)


def _cfg_int(key: str, env_key: str | None = None, default: int = 0) -> int:
    value = _cfg(key, env_key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _cfg_float(key: str, env_key: str | None = None, default: float = 0.0) -> float:
    value = _cfg(key, env_key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _cfg_bool(key: str, env_key: str | None = None, default: bool = False) -> bool:
    value = _cfg(key, env_key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _cfg_list(
    key: str,
    env_key: str | None = None,
    default: list[Any] | None = None,
    uppercase: bool = False,
) -> list[str]:
    if default is None:
        default = []

    value = _cfg(key, env_key, None)

    if value is None:
        items: list[Any] = list(default)
    elif isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = [v.strip() for v in value.split(",") if v.strip()]
    else:
        items = [value]

    result = [str(v).strip() for v in items if str(v).strip()]
    if uppercase:
        return [v.upper() for v in result]
    return result


def _cfg_int_list(
    key: str,
    env_key: str | None = None,
    default: list[int] | None = None,
) -> list[int]:
    if default is None:
        default = []
    raw = _cfg_list(key, env_key, [str(v) for v in default])
    result: list[int] = []
    for item in raw:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


# ===========================================================================
# SERVICE CONTROL
# ===========================================================================
SERVICE_CONTROL_ENABLED = _cfg_bool(
    "service_control.enabled", "SERVICE_CONTROL_ENABLED", False
)

SERVICE_CONTROL_TOKEN = _cfg_str(
    "service_control.token", "SERVICE_CONTROL_TOKEN", ""
).strip()

SERVICE_MANAGER = (
    _cfg_str("service_control.manager", "SERVICE_MANAGER", "internal").strip().lower()
)

SYSTEMD_SERVICE_NAME = _cfg_str(
    "service_control.systemd_service_name",
    "SYSTEMD_SERVICE_NAME",
    "option-feed-engine",
).strip()

REDEPLOY_COMMAND = _cfg_str(
    "service_control.redeploy_command", "REDEPLOY_COMMAND", ""
).strip()

SERVICE_COMMAND_TIMEOUT_SECONDS = _cfg_int(
    "service_control.command_timeout_seconds",
    "SERVICE_COMMAND_TIMEOUT_SECONDS",
    120,
)


# ===========================================================================
# MONGO / RUNTIME CONFIG
# ===========================================================================
MONGO_URI = _cfg_str("mongo.uri", "MONGO_URL", "")
MONGO_DB = _cfg_str("mongo.db", "MONGO_DB", "")
TOKENS_COLLECTION = _cfg_str("mongo.tokens_collection", "TOKENS_COLLECTION", "")

REFRESH_INTERVAL_MINUTES = _cfg_int(
    "mongo.refresh_interval_minutes", "REFRESH_INTERVAL_MINUTES", 60
)

RUNTIME_CONFIG_ENABLED = _cfg_bool(
    "runtime_config.enabled", "RUNTIME_CONFIG_ENABLED", True
)

RUNTIME_CONFIG_COLLECTION = _cfg_str(
    "runtime_config.collection", "RUNTIME_CONFIG_COLLECTION", "runtime_configs"
)

RUNTIME_CONFIG_AUDIT_COLLECTION = _cfg_str(
    "runtime_config.audit_collection",
    "RUNTIME_CONFIG_AUDIT_COLLECTION",
    "runtime_config_audit",
)


# ===========================================================================
# ISOLATED INSTRUMENT EVENT
# ===========================================================================
ISOLATED_INSTRUMENT_EVENT_ENABLED = _cfg_bool(
    "isolated_instrument_event.enabled",
    "ISOLATED_INSTRUMENT_EVENT_ENABLED",
    True,
)

ISOLATED_INSTRUMENT_EVENT_COLLECTION = _cfg_str(
    "isolated_instrument_event.collection",
    "ISOLATED_INSTRUMENT_EVENT_COLLECTION",
    "isolated_instrumentevent",
)

ISOLATED_INSTRUMENT_EVENT_FIELD_NAME = _cfg_str(
    "isolated_instrument_event.field_name",
    "ISOLATED_INSTRUMENT_EVENT_FIELD_NAME",
    "events",
)

ISOLATED_INSTRUMENT_EVENT_FAIL_OPEN = _cfg_bool(
    "isolated_instrument_event.fail_open",
    "ISOLATED_INSTRUMENT_EVENT_FAIL_OPEN",
    True,
)

ISOLATED_INSTRUMENT_EVENT_INCLUDE_SIMULATION = _cfg_bool(
    "isolated_instrument_event.include_simulation",
    "ISOLATED_INSTRUMENT_EVENT_INCLUDE_SIMULATION",
    True,
)

ISOLATED_INSTRUMENT_EVENT_INCLUDE_DRY_RUN = _cfg_bool(
    "isolated_instrument_event.include_dry_run",
    "ISOLATED_INSTRUMENT_EVENT_INCLUDE_DRY_RUN",
    True,
)


# ===========================================================================
# UPSTOX ORDER / DAILY ORDER ARCHIVE
# ===========================================================================
UPSTOX_ORDER_COLLECTION = _cfg_str(
    "upstox_order.collection", "UPSTOX_ORDER_COLLECTION", "upstox_orders"
)

DAILY_ORDER_ARCHIVE_ENABLED = _cfg_bool(
    "daily_order_archive.enabled", "DAILY_ORDER_ARCHIVE_ENABLED", True
)

DAILY_ORDER_ARCHIVE_COLLECTION = _cfg_str(
    "daily_order_archive.collection",
    "DAILY_ORDER_ARCHIVE_COLLECTION",
    "daily_order_book",
).strip()

DAILY_ORDER_ARCHIVE_HOUR = min(
    23,
    max(
        0,
        _cfg_int("daily_order_archive.hour", "DAILY_ORDER_ARCHIVE_HOUR", 15),
    ),
)

DAILY_ORDER_ARCHIVE_MINUTE = min(
    59,
    max(
        0,
        _cfg_int("daily_order_archive.minute", "DAILY_ORDER_ARCHIVE_MINUTE", 35),
    ),
)

DAILY_ORDER_ARCHIVE_WEEKDAYS_ONLY = _cfg_bool(
    "daily_order_archive.weekdays_only",
    "DAILY_ORDER_ARCHIVE_WEEKDAYS_ONLY",
    True,
)

DAILY_ORDER_ARCHIVE_FAIL_OPEN = _cfg_bool(
    "daily_order_archive.fail_open",
    "DAILY_ORDER_ARCHIVE_FAIL_OPEN",
    True,
)

DAILY_ORDER_ARCHIVE_NOTIFY_TELEGRAM = _cfg_bool(
    "daily_order_archive.notify_telegram",
    "DAILY_ORDER_ARCHIVE_NOTIFY_TELEGRAM",
    True,
)


# ===========================================================================
# ORDER EXECUTION FLAGS
# ===========================================================================
PLACE_ORDER = _cfg_bool("order.place_order", "PLACE_ORDER", True)
UPSTOX_ORDER_ENABLED = _cfg_bool(
    "order.upstox_order_enabled", "UPSTOX_ORDER_ENABLED", True
)
SELL_EXIT = _cfg_bool("order.sell_exit", "SELL_EXIT", True)
DUMMY_ORDERS = _cfg_bool("order.dummy_orders", "DUMMY_ORDERS", False)
ALGO_TELE_APP = _cfg_bool("order.algo_tele_app", "ALGO_TELE_APP", True)


# ===========================================================================
# RUNTIME CONFIG (extra flags)
# ===========================================================================
RUNTIME_CONFIG_CACHE_ENABLED = _cfg_bool(
    "runtime_config.cache_enabled", "RUNTIME_CONFIG_CACHE_ENABLED", True
)
RUNTIME_CONFIG_FAIL_OPEN = _cfg_bool(
    "runtime_config.fail_open", "RUNTIME_CONFIG_FAIL_OPEN", True
)
RUNTIME_CONFIG_AUDIT_ENABLED = _cfg_bool(
    "runtime_config.audit_enabled", "RUNTIME_CONFIG_AUDIT_ENABLED", True
)
RUNTIME_CONFIG_MAX_AUDIT_RECORDS = max(
    1,
    _cfg_int(
        "runtime_config.max_audit_records",
        "RUNTIME_CONFIG_MAX_AUDIT_RECORDS",
        5000,
    ),
)


# ===========================================================================
# TELEGRAM
# ===========================================================================
TELEGRAM_ENABLED = _cfg_bool("telegram.enabled", "TELEGRAM_ENABLED", False)
TELEGRAM_BOT_TOKEN = _cfg_str("telegram.bot_token", "TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _cfg_str("telegram.chat_id", "TELEGRAM_CHAT_ID", "")
TELEGRAM_TIMEOUT_SECONDS = max(
    1, _cfg_int("telegram.timeout_seconds", "TELEGRAM_TIMEOUT_SECONDS", 10)
)
TELEGRAM_RUNTIME_CONFIG_ENABLED = _cfg_bool(
    "telegram.runtime_config_enabled",
    "TELEGRAM_RUNTIME_CONFIG_ENABLED",
    True,
)
TELEGRAM_RUNTIME_CONFIG_CONFIRMATION_REQUIRED = _cfg_bool(
    "telegram.runtime_config_confirmation_required",
    "TELEGRAM_RUNTIME_CONFIG_CONFIRMATION_REQUIRED",
    True,
)
TELEGRAM_RUNTIME_CONFIG_CUSTOM_VALUE_ENABLED = _cfg_bool(
    "telegram.runtime_config_custom_value_enabled",
    "TELEGRAM_RUNTIME_CONFIG_CUSTOM_VALUE_ENABLED",
    True,
)
TELEGRAM_RUNTIME_CONFIG_TIMEOUT_SECONDS = max(
    1,
    _cfg_int(
        "telegram.runtime_config_timeout_seconds",
        "TELEGRAM_RUNTIME_CONFIG_TIMEOUT_SECONDS",
        120,
    ),
)
TELEGRAM_RUNTIME_CONFIG_SHOW_SENSITIVE_VALUES = _cfg_bool(
    "telegram.runtime_config_show_sensitive_values",
    "TELEGRAM_RUNTIME_CONFIG_SHOW_SENSITIVE_VALUES",
    False,
)

UPSTOX_TOKEN_DOC_ID = _cfg_str(
    "upstox.token_doc_id", "UPSTOX_TOKEN_DOC_ID", "upstox_access_token"
)
UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES = max(
    1,
    _cfg_int(
        "upstox.token_check_interval_minutes",
        "UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES",
        30,
    ),
)

TELEGRAM_TOKEN_BOT_ENABLED = _cfg_bool(
    "telegram_token_bot.enabled", "TELEGRAM_TOKEN_BOT_ENABLED", True
)
TELEGRAM_TOKEN_BOT_POLL_SECONDS = max(
    1,
    _cfg_int("telegram_token_bot.poll_seconds", "TELEGRAM_TOKEN_BOT_POLL_SECONDS", 3),
)
TELEGRAM_TOKEN_BOT_LONG_POLL_TIMEOUT = max(
    1,
    _cfg_int(
        "telegram_token_bot.long_poll_timeout",
        "TELEGRAM_TOKEN_BOT_LONG_POLL_TIMEOUT",
        20,
    ),
)
TELEGRAM_TOKEN_BOT_RESTRICT_TO_CHAT = _cfg_bool(
    "telegram_token_bot.restrict_to_chat",
    "TELEGRAM_TOKEN_BOT_RESTRICT_TO_CHAT",
    True,
)


# ===========================================================================
# ALGO APP
# ===========================================================================
ALGO_APP_ENABLED = _cfg_bool("algo_app.enabled", "ALGO_APP_ENABLED", False)
ALGO_APP_URL = _cfg_str("algo_app.url", "ALGO_APP_URL", "")
ALGO_APP_AUTH_TYPE = (
    _cfg_str("algo_app.auth_type", "ALGO_APP_AUTH_TYPE", "none").strip().lower()
)
ALGO_APP_AUTH_TOKEN = _cfg_str("algo_app.auth_token", "ALGO_APP_AUTH_TOKEN", "")
ALGO_APP_API_KEY = _cfg_str("algo_app.api_key", "ALGO_APP_API_KEY", "")
ALGO_APP_API_KEY_HEADER = _cfg_str(
    "algo_app.api_key_header", "ALGO_APP_API_KEY_HEADER", "X-API-Key"
)
ALGO_APP_TIMEOUT_SECONDS = max(
    0.1, _cfg_float("algo_app.timeout_seconds", "ALGO_APP_TIMEOUT_SECONDS", 10.0)
)
ALGO_APP_VERIFY_SSL = _cfg_bool("algo_app.verify_ssl", "ALGO_APP_VERIFY_SSL", True)
ALGO_APP_MAX_RETRIES = max(
    0, _cfg_int("algo_app.max_retries", "ALGO_APP_MAX_RETRIES", 3)
)
ALGO_APP_RETRY_DELAY_SECONDS = max(
    0.0,
    _cfg_float("algo_app.retry_delay_seconds", "ALGO_APP_RETRY_DELAY_SECONDS", 2.0),
)
ALGO_APP_SEND_IN_BACKGROUND = _cfg_bool(
    "algo_app.send_in_background", "ALGO_APP_SEND_IN_BACKGROUND", True
)
ALGO_APP_INCLUDE_EVENT_ID = _cfg_bool(
    "algo_app.include_event_id", "ALGO_APP_INCLUDE_EVENT_ID", True
)
ALGO_APP_PAYLOAD_SCHEMA_VERSION = _cfg_str(
    "algo_app.payload_schema_version",
    "ALGO_APP_PAYLOAD_SCHEMA_VERSION",
    "1.0",
)
ALGO_APP_SOURCE_NAME = _cfg_str(
    "algo_app.source_name", "ALGO_APP_SOURCE_NAME", "option_feed_engine"
)
ALGO_APP_MAX_RESPONSE_BODY_LENGTH = max(
    0,
    _cfg_int(
        "algo_app.max_response_body_length",
        "ALGO_APP_MAX_RESPONSE_BODY_LENGTH",
        2000,
    ),
)
ALGO_APP_BACKGROUND_QUEUE_COUNTS_AS_ACCEPTED = _cfg_bool(
    "algo_app.background_queue_counts_as_accepted",
    "ALGO_APP_BACKGROUND_QUEUE_COUNTS_AS_ACCEPTED",
    True,
)
ALGO_APP_BACKGROUND_MAX_WORKERS = max(
    1,
    _cfg_int("algo_app.background_max_workers", "ALGO_APP_BACKGROUND_MAX_WORKERS", 2),
)


# ===========================================================================
# MARKET
# ===========================================================================
MARKET_TIMEZONE = _cfg_str("market.timezone", "MARKET_TIMEZONE", "Asia/Kolkata")
MARKET_TIME_FORMAT = _cfg_str(
    "market.time_format", "MARKET_TIME_FORMAT", "%Y-%m-%d %H:%M:%S %Z"
)
MARKET_OPEN_HOUR = _cfg_int("market.open_hour", "MARKET_OPEN_HOUR", 9)
MARKET_OPEN_MINUTE = _cfg_int("market.open_minute", "MARKET_OPEN_MINUTE", 15)
MARKET_CLOSE_HOUR = _cfg_int("market.close_hour", "MARKET_CLOSE_HOUR", 15)
MARKET_CLOSE_MINUTE = _cfg_int("market.close_minute", "MARKET_CLOSE_MINUTE", 30)

MAIN_NIFTY_SECURITY = _cfg_str(
    "market.main_nifty_security", "MAIN_NIFTY_SECURITY", "NSE_INDEX|Nifty 50"
)

STRIKE_FROM = _cfg_float("market.strike_from", "STRIKE_FROM", 22500)
STRIKE_TO = _cfg_float("market.strike_to", "STRIKE_TO", 25000)
if STRIKE_FROM > STRIKE_TO:
    STRIKE_FROM, STRIKE_TO = STRIKE_TO, STRIKE_FROM


# ===========================================================================
# WEBSOCKET / HISTORICAL CANDLES
# ===========================================================================
WEBSOCKET_FEED_MODE = _cfg_str("websocket.feed_mode", "WEBSOCKET_FEED_MODE", "full")
LIVE_FEED_PROVIDER = _cfg_str("live_feed.provider", "LIVE_FEED_PROVIDER", "novag7").lower()
NOVAG7_ENABLED = _cfg_bool("novag7.enabled", "NOVAG7_ENABLED", True)
NOVAG7_WS_BASE_URL = _cfg_str("novag7.ws_base_url", "NOVAG7_WS_BASE_URL", "wss://feed.novag7.in/ws/market")
NOVAG7_PING_INTERVAL = _cfg_int("novag7.ping_interval", "NOVAG7_PING_INTERVAL", 20)
NOVAG7_PING_TIMEOUT = _cfg_int("novag7.ping_timeout", "NOVAG7_PING_TIMEOUT", 20)
NOVAG7_CLOSE_TIMEOUT = _cfg_int("novag7.close_timeout", "NOVAG7_CLOSE_TIMEOUT", 10)
NOVAG7_RECONNECT_INITIAL_DELAY = _cfg_int("novag7.reconnect_initial_delay", "NOVAG7_RECONNECT_INITIAL_DELAY", 5)
NOVAG7_RECONNECT_MAX_DELAY = _cfg_int("novag7.reconnect_max_delay", "NOVAG7_RECONNECT_MAX_DELAY", 30)
NOVAG7_RECONCILE_INTERVAL = _cfg_int("novag7.reconcile_interval", "NOVAG7_RECONCILE_INTERVAL", 5)

HISTORICAL_CANDLE_ENABLED = _cfg_bool(
    "historical_candle.enabled", "HISTORICAL_CANDLE_ENABLED", True
)
HISTORICAL_CANDLE_DAYS = max(
    1, _cfg_int("historical_candle.days", "HISTORICAL_CANDLE_DAYS", 10)
)
HISTORICAL_CANDLE_INTERVAL = _cfg_str(
    "historical_candle.interval", "HISTORICAL_CANDLE_INTERVAL", "1minute"
)
HISTORICAL_CANDLE_API_VERSION = _cfg_str(
    "historical_candle.api_version", "HISTORICAL_CANDLE_API_VERSION", "2.0"
)
HISTORICAL_CANDLE_MAX_DAYS_PER_REQUEST = max(
    1,
    _cfg_int(
        "historical_candle.max_days_per_request",
        "HISTORICAL_CANDLE_MAX_DAYS_PER_REQUEST",
        7,
    ),
)
HISTORICAL_CANDLE_OUTPUT_DIR = _cfg_str(
    "historical_candle.output_dir",
    "HISTORICAL_CANDLE_OUTPUT_DIR",
    "data/historical_candles",
)
HISTORICAL_CANDLE_MAX_WORKERS = max(
    1,
    _cfg_int("historical_candle.max_workers", "HISTORICAL_CANDLE_MAX_WORKERS", 8),
)
HISTORICAL_CANDLE_REQUEST_SLEEP_SECONDS = max(
    0.0,
    _cfg_float(
        "historical_candle.request_sleep_seconds",
        "HISTORICAL_CANDLE_REQUEST_SLEEP_SECONDS",
        0.15,
    ),
)


# ===========================================================================
# OPENING RANGE
# ===========================================================================
OPENING_RANGE_ENABLED = _cfg_bool(
    "opening_range.enabled", "OPENING_RANGE_ENABLED", True
)
OPENING_RANGE_INTERVAL = _cfg_str(
    "opening_range.interval", "OPENING_RANGE_INTERVAL", "1minute"
)
OPENING_RANGE_CANDLE_COUNT = max(
    1, _cfg_int("opening_range.candle_count", "OPENING_RANGE_CANDLE_COUNT", 1)
)
OPENING_RANGE_MARKET_OPEN_HOUR = _cfg_int(
    "opening_range.market_open_hour", "OPENING_RANGE_MARKET_OPEN_HOUR", 9
)
OPENING_RANGE_MARKET_OPEN_MINUTE = _cfg_int(
    "opening_range.market_open_minute", "OPENING_RANGE_MARKET_OPEN_MINUTE", 15
)
OPENING_RANGE_FETCH_HOUR = _cfg_int(
    "opening_range.fetch_hour", "OPENING_RANGE_FETCH_HOUR", 9
)
OPENING_RANGE_FETCH_MINUTE = _cfg_int(
    "opening_range.fetch_minute", "OPENING_RANGE_FETCH_MINUTE", 18
)
OPENING_RANGE_INTRADAY_UNIT = _cfg_str(
    "opening_range.intraday_unit", "OPENING_RANGE_INTRADAY_UNIT", "minutes"
)
OPENING_RANGE_INTRADAY_INTERVAL = _cfg_str(
    "opening_range.intraday_interval", "OPENING_RANGE_INTRADAY_INTERVAL", "1"
)
OPENING_RANGE_MAX_WORKERS = max(
    1, _cfg_int("opening_range.max_workers", "OPENING_RANGE_MAX_WORKERS", 8)
)
OPENING_RANGE_REQUEST_SLEEP_SECONDS = max(
    0.0,
    _cfg_float(
        "opening_range.request_sleep_seconds",
        "OPENING_RANGE_REQUEST_SLEEP_SECONDS",
        0.15,
    ),
)
OPENING_RANGE_SAVE_FILE = _cfg_bool(
    "opening_range.save_file", "OPENING_RANGE_SAVE_FILE", True
)
OPENING_RANGE_OUTPUT_FILE = _cfg_str(
    "opening_range.output_file",
    "OPENING_RANGE_OUTPUT_FILE",
    "data/opening_range_results.json",
)
OPENING_RANGE_MAX_EVENTS_IN_MEMORY = max(
    1,
    _cfg_int(
        "opening_range.max_events_in_memory",
        "OPENING_RANGE_MAX_EVENTS_IN_MEMORY",
        5000,
    ),
)

OPENING_RANGE_BACKFILL_TOUCH_SCAN_ENABLED = _cfg_bool(
    "opening_range.backfill_touch_scan_enabled",
    "OPENING_RANGE_BACKFILL_TOUCH_SCAN_ENABLED",
    True,
)
OPENING_RANGE_BACKFILL_TOUCH_SCAN_SOURCE = _cfg_str(
    "opening_range.backfill_touch_scan_source",
    "OPENING_RANGE_BACKFILL_TOUCH_SCAN_SOURCE",
    "intraday_api",
)

OPENING_RANGE_TOUCH_ALERT_ENABLED = _cfg_bool(
    "opening_range.touch_alert_enabled",
    "OPENING_RANGE_TOUCH_ALERT_ENABLED",
    True,
)
OPENING_RANGE_TOUCH_ALERT_MAX_INSTRUMENTS = max(
    1,
    _cfg_int(
        "opening_range.touch_alert_max_instruments",
        "OPENING_RANGE_TOUCH_ALERT_MAX_INSTRUMENTS",
        5,
    ),
)
OPENING_RANGE_TOUCH_ALERT_BATCH_SECONDS = max(
    1,
    _cfg_int(
        "opening_range.touch_alert_batch_seconds",
        "OPENING_RANGE_TOUCH_ALERT_BATCH_SECONDS",
        10,
    ),
)
OPENING_RANGE_TOUCH_ALERT_ONCE_PER_LEVEL = _cfg_bool(
    "opening_range.touch_alert_once_per_level",
    "OPENING_RANGE_TOUCH_ALERT_ONCE_PER_LEVEL",
    True,
)
OPENING_RANGE_TOUCH_ALERT_OPTIONS_ONLY = _cfg_bool(
    "opening_range.touch_alert_options_only",
    "OPENING_RANGE_TOUCH_ALERT_OPTIONS_ONLY",
    True,
)
OPENING_RANGE_TOUCH_ALERT_SORT_BY_NEAREST_INDEX = _cfg_bool(
    "opening_range.touch_alert_sort_by_nearest_index",
    "OPENING_RANGE_TOUCH_ALERT_SORT_BY_NEAREST_INDEX",
    True,
)
OPENING_RANGE_TOUCH_ALERT_MAIN_INDEX_KEY = _cfg_str(
    "opening_range.touch_alert_main_index_key",
    "OPENING_RANGE_TOUCH_ALERT_MAIN_INDEX_KEY",
    MAIN_NIFTY_SECURITY,
)
OPENING_RANGE_BACKFILL_TOUCH_ALERT_ENABLED = _cfg_bool(
    "opening_range.backfill_touch_alert_enabled",
    "OPENING_RANGE_BACKFILL_TOUCH_ALERT_ENABLED",
    True,
)
OPENING_RANGE_LIVE_TOUCH_ALERT_ENABLED = _cfg_bool(
    "opening_range.live_touch_alert_enabled",
    "OPENING_RANGE_LIVE_TOUCH_ALERT_ENABLED",
    True,
)
OPENING_RANGE_TOUCH_CHECK_MODE = _cfg_str(
    "opening_range.touch_check_mode",
    "OPENING_RANGE_TOUCH_CHECK_MODE",
    "high_low",
).lower()
OPENING_RANGE_STORE_TOUCH_STATUS = _cfg_bool(
    "opening_range.store_touch_status",
    "OPENING_RANGE_STORE_TOUCH_STATUS",
    True,
)
OPENING_RANGE_TOUCH_EVENTS_OUTPUT_FILE = _cfg_str(
    "opening_range.touch_events_output_file",
    "OPENING_RANGE_TOUCH_EVENTS_OUTPUT_FILE",
    "data/opening_range_touch_events.json",
)
OPENING_RANGE_TOUCH_EVENTS_SAVE_TEST_FILE = _cfg_bool(
    "opening_range.touch_events_save_test_file",
    "OPENING_RANGE_TOUCH_EVENTS_SAVE_TEST_FILE",
    True,
)


# ===========================================================================
# OPENING RANGE — ISOLATED INSTRUMENT
# ===========================================================================
OPENING_RANGE_ISOLATED_INSTRUMENT_ENABLED = _cfg_bool(
    "opening_range_isolated.enabled",
    "OPENING_RANGE_ISOLATED_INSTRUMENT_ENABLED",
    True,
)
OPENING_RANGE_ISOLATION_AVERAGE_WINDOW_POINTS = max(
    0.0,
    _cfg_float(
        "opening_range_isolated.average_window_points",
        "OPENING_RANGE_ISOLATION_AVERAGE_WINDOW_POINTS",
        500,
    ),
)
OPENING_RANGE_ISOLATION_MIN_WINDOW_POINTS = max(
    0.0,
    _cfg_float(
        "opening_range_isolated.min_window_points",
        "OPENING_RANGE_ISOLATION_MIN_WINDOW_POINTS",
        0,
    ),
)
OPENING_RANGE_ISOLATION_MAX_WINDOW_POINTS = max(
    OPENING_RANGE_ISOLATION_MIN_WINDOW_POINTS,
    _cfg_float(
        "opening_range_isolated.max_window_points",
        "OPENING_RANGE_ISOLATION_MAX_WINDOW_POINTS",
        5000,
    ),
)
OPENING_RANGE_ISOLATION_AVERAGE_WINDOW_POINTS = max(
    OPENING_RANGE_ISOLATION_MIN_WINDOW_POINTS,
    min(
        OPENING_RANGE_ISOLATION_AVERAGE_WINDOW_POINTS,
        OPENING_RANGE_ISOLATION_MAX_WINDOW_POINTS,
    ),
)
OPENING_RANGE_ISOLATION_TOUCH_LEVELS = _cfg_list(
    "opening_range_isolated.touch_levels",
    "OPENING_RANGE_ISOLATION_TOUCH_LEVELS",
    ["R3"],
    uppercase=True,
)
OPENING_RANGE_ISOLATION_PRIORITY_LEVELS = _cfg_list(
    "opening_range_isolated.priority_levels",
    "OPENING_RANGE_ISOLATION_PRIORITY_LEVELS",
    ["R3"],
    uppercase=True,
)
OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED = _cfg_bool(
    "opening_range_isolated.notify_enabled",
    "OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED",
    True,
)
OPENING_RANGE_ISOLATION_ALLOW_BACKFILL_TOUCH = _cfg_bool(
    "opening_range_isolated.allow_backfill_touch",
    "OPENING_RANGE_ISOLATION_ALLOW_BACKFILL_TOUCH",
    True,
)
OPENING_RANGE_ISOLATION_ALLOW_LIVE_TOUCH = _cfg_bool(
    "opening_range_isolated.allow_live_touch",
    "OPENING_RANGE_ISOLATION_ALLOW_LIVE_TOUCH",
    True,
)
OPENING_RANGE_ISOLATION_OPTIONS_ONLY = _cfg_bool(
    "opening_range_isolated.options_only",
    "OPENING_RANGE_ISOLATION_OPTIONS_ONLY",
    True,
)
OPENING_RANGE_ISOLATED_INSTRUMENT_OUTPUT_FILE = _cfg_str(
    "opening_range_isolated.output_file",
    "OPENING_RANGE_ISOLATED_INSTRUMENT_OUTPUT_FILE",
    "data/isolated_opening_range_instrument.json",
)
OPENING_RANGE_ISOLATION_RESET_DAILY = _cfg_bool(
    "opening_range_isolated.reset_daily",
    "OPENING_RANGE_ISOLATION_RESET_DAILY",
    True,
)
OPENING_RANGE_FIRST_TOUCH_SELECTION_ENABLED = _cfg_bool(
    "opening_range_isolated.first_touch_selection_enabled",
    "OPENING_RANGE_FIRST_TOUCH_SELECTION_ENABLED",
    True,
)
OPENING_RANGE_FIRST_TOUCH_SELECTION_SOURCE = _cfg_str(
    "opening_range_isolated.first_touch_selection_source",
    "OPENING_RANGE_FIRST_TOUCH_SELECTION_SOURCE",
    "average_window_level_priority",
)


# ===========================================================================
# SELECTED OR — TOUCH / EMA ALERTS
# ===========================================================================
OPENING_RANGE_SELECTED_OR_TOUCH_NOTIFY_ENABLED = _cfg_bool(
    "opening_range_selected_or.touch_notify_enabled",
    "OPENING_RANGE_SELECTED_OR_TOUCH_NOTIFY_ENABLED",
    True,
)
OPENING_RANGE_SELECTED_OR_EMA_ALERT_ENABLED = _cfg_bool(
    "opening_range_selected_or.ema_alert_enabled",
    "OPENING_RANGE_SELECTED_OR_EMA_ALERT_ENABLED",
    True,
)
OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED = _cfg_bool(
    "opening_range_selected_or.legacy_touch_telegram_enabled",
    "OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED",
    False,
)
OPENING_RANGE_SELECTED_OR_EMA_ALERT_ONCE_PER_CROSS = _cfg_bool(
    "opening_range_selected_or.ema_alert_once_per_cross",
    "OPENING_RANGE_SELECTED_OR_EMA_ALERT_ONCE_PER_CROSS",
    False,
)


# ===========================================================================
# EMA CROSS
# ===========================================================================
EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS = _cfg_bool(
    "ema_cross.include_opening_range_levels",
    "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
    True,
)
EMA_CROSS_BROADCAST_WITHOUT_OPENING_RANGE = _cfg_bool(
    "ema_cross.broadcast_without_opening_range",
    "EMA_CROSS_BROADCAST_WITHOUT_OPENING_RANGE",
    True,
)
EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED = _cfg_bool(
    "ema_cross.isolated_instrument_telegram_enabled",
    "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED",
    True,
)
EMA_ISOLATED_ALERT_EVERY_CROSS = _cfg_bool(
    "ema_cross.isolated_alert_every_cross",
    "EMA_ISOLATED_ALERT_EVERY_CROSS",
    True,
)
EMA_ISOLATED_ALERT_INCLUDE_LEVEL_NAME = _cfg_bool(
    "ema_cross.isolated_alert_include_level_name",
    "EMA_ISOLATED_ALERT_INCLUDE_LEVEL_NAME",
    True,
)
EMA_ISOLATED_ALERT_INCLUDE_NIFTY_LTP = _cfg_bool(
    "ema_cross.isolated_alert_include_nifty_ltp",
    "EMA_ISOLATED_ALERT_INCLUDE_NIFTY_LTP",
    True,
)
EMA_ISOLATED_ALERT_INCLUDE_EMA_DETAILS = _cfg_bool(
    "ema_cross.isolated_alert_include_ema_details",
    "EMA_ISOLATED_ALERT_INCLUDE_EMA_DETAILS",
    True,
)
EMA_ISOLATED_ALERT_INCLUDE_NEAREST_ORDER_INSTRUMENTS = _cfg_bool(
    "ema_cross.isolated_alert_include_nearest_order_instruments",
    "EMA_ISOLATED_ALERT_INCLUDE_NEAREST_ORDER_INSTRUMENTS",
    True,
)
EMA_ISOLATED_ALERT_INCLUDE_BUDGET_INSTRUMENTS = _cfg_bool(
    "ema_cross.isolated_alert_include_budget_instruments",
    "EMA_ISOLATED_ALERT_INCLUDE_BUDGET_INSTRUMENTS",
    True,
)
EMA_ISOLATED_ALERT_INCLUDE_CANDLE_CLOSE = _cfg_bool(
    "ema_cross.isolated_alert_include_candle_close",
    "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_CLOSE",
    True,
)
EMA_ISOLATED_ALERT_INCLUDE_CANDLE_LOW = _cfg_bool(
    "ema_cross.isolated_alert_include_candle_low",
    "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_LOW",
    True,
)
EMA_ISOLATED_ALERT_INCLUDE_CLOSE_LOW_DIFFERENCE = _cfg_bool(
    "ema_cross.isolated_alert_include_close_low_difference",
    "EMA_ISOLATED_ALERT_INCLUDE_CLOSE_LOW_DIFFERENCE",
    True,
)
EMA_ISOLATED_ALERT_INCLUDE_CANDLE_TIME = _cfg_bool(
    "ema_cross.isolated_alert_include_candle_time",
    "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_TIME",
    True,
)
EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES = max(
    0,
    _cfg_int(
        "ema_cross.isolated_alert_price_decimal_places",
        "EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES",
        2,
    ),
)


# ===========================================================================
# EMA ALERT — OPTION / STRIKE SELECTION
# ===========================================================================
EMA_ALERT_BULLISH_OPTION_TYPE = _cfg_str(
    "ema_alert.bullish_option_type",
    "EMA_ALERT_BULLISH_OPTION_TYPE",
    "CE",
).upper()
EMA_ALERT_BEARISH_OPTION_TYPE = _cfg_str(
    "ema_alert.bearish_option_type",
    "EMA_ALERT_BEARISH_OPTION_TYPE",
    "PE",
).upper()
EMA_ALERT_STRIKE_STEP = max(
    1, _cfg_int("ema_alert.strike_step", "EMA_ALERT_STRIKE_STEP", 50)
)
EMA_ALERT_NEAREST_STRIKE_COUNT = max(
    1,
    _cfg_int("ema_alert.nearest_strike_count", "EMA_ALERT_NEAREST_STRIKE_COUNT", 3),
)
EMA_ALERT_NEAREST_STRIKE_OFFSETS = _cfg_int_list(
    "ema_alert.nearest_strike_offsets",
    "EMA_ALERT_NEAREST_STRIKE_OFFSETS",
    [-50, 0, 50],
)
EMA_ALERT_ORDER_STRIKES_CLAMP_TO_FILTER_RANGE = _cfg_bool(
    "ema_alert.order_strikes_clamp_to_filter_range",
    "EMA_ALERT_ORDER_STRIKES_CLAMP_TO_FILTER_RANGE",
    True,
)
EMA_ALERT_INCLUDE_ORDER_INSTRUMENT_LTP = _cfg_bool(
    "ema_alert.include_order_instrument_ltp",
    "EMA_ALERT_INCLUDE_ORDER_INSTRUMENT_LTP",
    True,
)
EMA_ALERT_SHOW_ORDER_INSTRUMENT_WHEN_LTP_MISSING = _cfg_bool(
    "ema_alert.show_order_instrument_when_ltp_missing",
    "EMA_ALERT_SHOW_ORDER_INSTRUMENT_WHEN_LTP_MISSING",
    True,
)
EMA_ALERT_MAX_ORDER_INSTRUMENTS = max(
    1,
    _cfg_int("ema_alert.max_order_instruments", "EMA_ALERT_MAX_ORDER_INSTRUMENTS", 3),
)

# EMA ALERT — budget range
EMA_ORDER_USE_ISOLATED_INSTRUMENT = _cfg_bool(
    "ema_alert.order_use_isolated_instrument",
    "EMA_ORDER_USE_ISOLATED_INSTRUMENT",
    True,
)

EMA_ALERT_BUDGET_RANGE_ENABLED = _cfg_bool(
    "ema_alert.budget_range_enabled", "EMA_ALERT_BUDGET_RANGE_ENABLED", True
)
EMA_ALERT_BUDGET_MIN_PRICE = max(
    0.0,
    _cfg_float("ema_alert.budget_min_price", "EMA_ALERT_BUDGET_MIN_PRICE", 50),
)
EMA_ALERT_BUDGET_MAX_PRICE = max(
    EMA_ALERT_BUDGET_MIN_PRICE,
    _cfg_float("ema_alert.budget_max_price", "EMA_ALERT_BUDGET_MAX_PRICE", 120),
)
EMA_ALERT_BUDGET_MAX_INSTRUMENTS = max(
    1,
    _cfg_int(
        "ema_alert.budget_max_instruments",
        "EMA_ALERT_BUDGET_MAX_INSTRUMENTS",
        2,
    ),
)
EMA_ALERT_BUDGET_USE_SUGGESTED_ORDER_SIDE = _cfg_bool(
    "ema_alert.budget_use_suggested_order_side",
    "EMA_ALERT_BUDGET_USE_SUGGESTED_ORDER_SIDE",
    True,
)
EMA_ALERT_BUDGET_SUBSCRIBED_ONLY = _cfg_bool(
    "ema_alert.budget_subscribed_only",
    "EMA_ALERT_BUDGET_SUBSCRIBED_ONLY",
    True,
)
EMA_ALERT_BUDGET_REQUIRE_LIVE_LTP = _cfg_bool(
    "ema_alert.budget_require_live_ltp",
    "EMA_ALERT_BUDGET_REQUIRE_LIVE_LTP",
    True,
)
EMA_ALERT_BUDGET_SORT_MODE = _cfg_str(
    "ema_alert.budget_sort_mode",
    "EMA_ALERT_BUDGET_SORT_MODE",
    "nearest_to_budget_midpoint",
)
EMA_ALERT_BUDGET_RANGE_INCLUSIVE = _cfg_bool(
    "ema_alert.budget_range_inclusive",
    "EMA_ALERT_BUDGET_RANGE_INCLUSIVE",
    True,
)


# ===========================================================================
# EMA ALGO PAYLOAD
# ===========================================================================
EMA_ALGO_PAYLOAD_INCLUDE_OPENING_RANGE = _cfg_bool(
    "ema_algo_payload.include_opening_range",
    "EMA_ALGO_PAYLOAD_INCLUDE_OPENING_RANGE",
    True,
)
EMA_ALGO_PAYLOAD_INCLUDE_EMA_VALUES = _cfg_bool(
    "ema_algo_payload.include_ema_values",
    "EMA_ALGO_PAYLOAD_INCLUDE_EMA_VALUES",
    True,
)
EMA_ALGO_PAYLOAD_INCLUDE_CANDLE = _cfg_bool(
    "ema_algo_payload.include_candle", "EMA_ALGO_PAYLOAD_INCLUDE_CANDLE", True
)
EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENTS = _cfg_bool(
    "ema_algo_payload.include_nearest_instruments",
    "EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENTS",
    True,
)
EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENTS = _cfg_bool(
    "ema_algo_payload.include_budget_instruments",
    "EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENTS",
    True,
)
EMA_ALGO_PAYLOAD_INCLUDE_DELIVERY_METADATA = _cfg_bool(
    "ema_algo_payload.include_delivery_metadata",
    "EMA_ALGO_PAYLOAD_INCLUDE_DELIVERY_METADATA",
    False,
)
EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENT_CANDLES = _cfg_bool(
    "ema_algo_payload.include_nearest_instrument_candles",
    "EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENT_CANDLES",
    True,
)
EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENT_CANDLES = _cfg_bool(
    "ema_algo_payload.include_budget_instrument_candles",
    "EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENT_CANDLES",
    True,
)
EMA_ALGO_PAYLOAD_INCLUDE_RAW_EMA_EVENT = _cfg_bool(
    "ema_algo_payload.include_raw_ema_event",
    "EMA_ALGO_PAYLOAD_INCLUDE_RAW_EMA_EVENT",
    True,
)


# ===========================================================================
# TEST FLAG / EMA
# ===========================================================================
TEST_FLAG = _cfg_bool("testing.test_flag", "TEST_FLAG", False)

EMA_FAST_PERIOD = max(1, _cfg_int("ema.fast_period", "EMA_FAST_PERIOD", 9))
EMA_SLOW_PERIOD = max(
    EMA_FAST_PERIOD + 1, _cfg_int("ema.slow_period", "EMA_SLOW_PERIOD", 21)
)
EMA_CROSS_OUTPUT_FILE = _cfg_str(
    "ema.cross_output_file", "EMA_CROSS_OUTPUT_FILE", "data/ema_cross_results.json"
)


# ===========================================================================
# LIVE EMA
# ===========================================================================
LIVE_EMA_ENABLED = _cfg_bool("live_ema.enabled", "LIVE_EMA_ENABLED", True)
LIVE_EMA_CALCULATION_MODE = _cfg_bool(
    "live_ema.calculation_mode", "LIVE_EMA_CALCULATION_MODE", False
)
LIVE_EMA_INTERVAL_MINUTES = max(
    1, _cfg_int("live_ema.interval_minutes", "LIVE_EMA_INTERVAL_MINUTES", 1)
)
LIVE_EMA_FAST_PERIOD = max(
    1, _cfg_int("live_ema.fast_period", "LIVE_EMA_FAST_PERIOD", 9)
)
LIVE_EMA_SLOW_PERIOD = max(
    LIVE_EMA_FAST_PERIOD + 1,
    _cfg_int("live_ema.slow_period", "LIVE_EMA_SLOW_PERIOD", 21),
)
LIVE_EMA_TICK_ALERT_ONCE_PER_DIRECTION = _cfg_bool(
    "live_ema.tick_alert_once_per_direction",
    "LIVE_EMA_TICK_ALERT_ONCE_PER_DIRECTION",
    True,
)
LIVE_EMA_TICK_MIN_PRICE_CHANGE = max(
    0.0,
    _cfg_float("live_ema.tick_min_price_change", "LIVE_EMA_TICK_MIN_PRICE_CHANGE", 0),
)
LIVE_EMA_SAVE_TEST_FILE = _cfg_bool(
    "live_ema.save_test_file", "LIVE_EMA_SAVE_TEST_FILE", True
)
LIVE_EMA_OUTPUT_FILE = _cfg_str(
    "live_ema.output_file",
    "LIVE_EMA_OUTPUT_FILE",
    "data/live_ema_cross_results.json",
)
LIVE_EMA_MAX_EVENTS_IN_MEMORY = max(
    1,
    _cfg_int("live_ema.max_events_in_memory", "LIVE_EMA_MAX_EVENTS_IN_MEMORY", 5000),
)


# ===========================================================================
# EMA ALERT SIMULATION
# ===========================================================================
EMA_ALERT_SIMULATION_ENABLED = _cfg_bool(
    "ema_alert_simulation.enabled", "EMA_ALERT_SIMULATION_ENABLED", True
)
EMA_ALERT_SIMULATION_DEFAULT_DRY_RUN = _cfg_bool(
    "ema_alert_simulation.default_dry_run",
    "EMA_ALERT_SIMULATION_DEFAULT_DRY_RUN",
    True,
)
EMA_ALERT_SIMULATION_ALLOW_TELEGRAM = _cfg_bool(
    "ema_alert_simulation.allow_telegram",
    "EMA_ALERT_SIMULATION_ALLOW_TELEGRAM",
    False,
)
EMA_ALERT_SIMULATION_ALLOW_ALGO_APP = _cfg_bool(
    "ema_alert_simulation.allow_algo_app",
    "EMA_ALERT_SIMULATION_ALLOW_ALGO_APP",
    False,
)


def get_ema_alert_simulation_status():
    return {
        "enabled": bool(EMA_ALERT_SIMULATION_ENABLED),
        "default_dry_run": bool(EMA_ALERT_SIMULATION_DEFAULT_DRY_RUN),
        "allow_telegram": bool(EMA_ALERT_SIMULATION_ALLOW_TELEGRAM),
        "allow_algo_app": bool(EMA_ALERT_SIMULATION_ALLOW_ALGO_APP),
    }


# ===========================================================================
# STARTUP CLEANUP
# ===========================================================================
# Paths are relative to PROJECT_ROOT.
# Directories will be deleted and recreated as empty directories.
STARTUP_REMOVE_LIST = _cfg_list("startup.remove_list", None, ["data"])

# Never allow these important paths to be removed accidentally.
STARTUP_CLEANUP_PROTECTED_PATHS = set(
    _cfg_list(
        "startup.protected_paths",
        None,
        ["logs", ".env", ".git", "myenv", ".venv"],
    )
)
