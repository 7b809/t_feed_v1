# app/config.py
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    app_name: str
    app_env: str
    app_host: str
    app_port: int

    mongo_url: str
    mongo_database: str
    isolation_events_collection: str

    sr_state_dir: str
    sr_store_heartbeats: bool
    data_dir: str

    sr_ws_base_url: str
    sr_ws_base_url_local: str
    sr_ws_default_config: str
    sr_ws_keepalive_seconds: int
    sr_ws_reconnect_max_backoff_seconds: int

    supertrend_enabled: bool
    supertrend_state_dir: str

    live_feed_enabled: bool
    live_feed_ws_url: str
    live_feed_port: int
    live_feed_mode: str
    live_feed_save_raw: bool
    live_feed_state_dir: str
    live_feed_start_hour: int
    live_feed_start_minute: int
    live_feed_end_hour: int
    live_feed_end_minute: int
    live_feed_reconnect_max_backoff_seconds: int
    nifty_instrument_key: str

    dummy_orders_enabled: bool
    dummy_supertrend_orders_enabled: bool
    dummy_order_quantity: int
    dummy_order_state_dir: str
    dummy_order_stop_loss_percent: float
    dummy_target_upper_threshold: float
    dummy_target_lower_threshold: float
    dummy_target_points_above_upper: float
    dummy_target_points_middle: float
    dummy_target_points_below_lower: float

    # Daily MEGA archive
    daily_data_backup_enabled: bool
    daily_data_backup_dir: str
    daily_data_backup_hour: int
    daily_data_backup_minute: int
    daily_data_backup_days: str
    daily_data_backup_collection: str
    mega_enabled: bool
    mega_email: str
    mega_password: str
    mega_backup_root: str

    isolation_notify_client_key: str
    isolation_notify_client_key_header: str
    require_today_only_scope: bool
    verbose: bool

    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str

    log_level: str
    logs_dir: str

    def __post_init__(self) -> None:
        if not 1 <= self.live_feed_port <= 65535:
            raise ValueError("LIVE_FEED_PORT must be between 1 and 65535")
        feed_url = urlparse(self.live_feed_ws_url_effective)
        if (
            self.live_feed_enabled
            and feed_url.hostname in {"localhost", "127.0.0.1", "::1"}
            and feed_url.port == self.app_port
        ):
            raise ValueError(
                "APP_PORT cannot match the local LIVE_FEED_PORT; the services need separate ports"
            )
        if self.live_feed_mode not in {"ltpc", "full", "full_d30", "option_greeks"}:
            raise ValueError("LIVE_FEED_MODE must be ltpc, full, full_d30, or option_greeks")
        if not (0 <= self.live_feed_start_hour < 24 and 0 <= self.live_feed_end_hour < 24):
            raise ValueError("LIVE_FEED_START_HOUR and LIVE_FEED_END_HOUR must be 0..23")
        if not (0 <= self.live_feed_start_minute < 60 and 0 <= self.live_feed_end_minute < 60):
            raise ValueError("LIVE_FEED_START_MINUTE and LIVE_FEED_END_MINUTE must be 0..59")
        start = self.live_feed_start_hour * 60 + self.live_feed_start_minute
        end = self.live_feed_end_hour * 60 + self.live_feed_end_minute
        if start >= end:
            raise ValueError("Live-feed end time must be after start time on the same day")
        if self.live_feed_reconnect_max_backoff_seconds < 1:
            raise ValueError("LIVE_FEED_RECONNECT_MAX_BACKOFF_SECONDS must be >= 1")
        if self.dummy_order_quantity < 1:
            raise ValueError("DUMMY_ORDER_QUANTITY must be >= 1")
        if not 0 < self.dummy_order_stop_loss_percent < 100:
            raise ValueError("DUMMY_ORDER_STOP_LOSS_PERCENT must be between 0 and 100")
        if self.dummy_target_lower_threshold <= 0 or self.dummy_target_upper_threshold <= self.dummy_target_lower_threshold:
            raise ValueError("Dummy target thresholds must be positive and upper > lower")
        if min(self.dummy_target_points_above_upper, self.dummy_target_points_middle,
               self.dummy_target_points_below_lower) <= 0:
            raise ValueError("Dummy target point bands must be positive")
        if not 0 <= self.daily_data_backup_hour < 24:
            raise ValueError("DAILY_DATA_BACKUP_HOUR must be 0..23")
        if not 0 <= self.daily_data_backup_minute < 60:
            raise ValueError("DAILY_DATA_BACKUP_MINUTE must be 0..59")


    @property
    def live_feed_ws_url_effective(self) -> str:
        base = (self.live_feed_ws_url or "").strip()
        return base.rstrip("/") if base else f"ws://127.0.0.1:{self.live_feed_port}"

    @property
    def supertrend_ws_base_url_effective(self) -> str:
        return self.sr_ws_base_url_effective

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def sr_ws_base_url_effective(self) -> str:
        if self.is_production:
            return self.sr_ws_base_url.rstrip("/")
        local = (self.sr_ws_base_url_local or "").strip()
        return (local or self.sr_ws_base_url).rstrip("/")

    @property
    def data_dir_enabled(self) -> bool:
        return bool(self.data_dir.strip())


def load_settings() -> Settings:
    return Settings(
        app_name=os.getenv("APP_NAME", "orderflow"),
        app_env=os.getenv("APP_ENV", "development"),
        app_host=os.getenv("APP_HOST", "0.0.0.0"),
        app_port=_env_int("APP_PORT", 8003),
        mongo_url=os.getenv("MONGO_URL", "mongodb://localhost:27017"),
        mongo_database=os.getenv("MONGO_DATABASE", "ISOLATION_CONSUMER"),
        isolation_events_collection=os.getenv("ISOLATION_EVENTS_COLLECTION", "isolation_events"),
        sr_state_dir=os.getenv("SR_STATE_DIR", "./data/sr_state"),
        sr_store_heartbeats=_env_bool("SR_STORE_HEARTBEATS", False),
        data_dir=os.getenv("DATA_DIR", "./data"),
        sr_ws_base_url=os.getenv("SR_WS_BASE_URL", "wss://ema.novag7.in"),
        sr_ws_base_url_local=os.getenv("SR_WS_BASE_URL_LOCAL", ""),
        sr_ws_default_config=os.getenv("SR_WS_DEFAULT_CONFIG", "all"),
        sr_ws_keepalive_seconds=_env_int("SR_WS_KEEPALIVE_SECONDS", 30),
        sr_ws_reconnect_max_backoff_seconds=_env_int("SR_WS_RECONNECT_MAX_BACKOFF_SECONDS", 30),
        supertrend_enabled=_env_bool("SUPERTREND_ENABLED", True),
        supertrend_state_dir=os.getenv("SUPERTREND_STATE_DIR", "./data/supertrend_state"),
        live_feed_enabled=_env_bool("LIVE_FEED_ENABLED", True),
        live_feed_ws_url=os.getenv("LIVE_FEED_WS_URL", ""),
        live_feed_port=_env_int("LIVE_FEED_PORT", 8010),
        live_feed_mode=os.getenv("LIVE_FEED_MODE", "full").strip().lower(),
        live_feed_save_raw=_env_bool("LIVE_FEED_SAVE_RAW", True),
        live_feed_state_dir=os.getenv("LIVE_FEED_STATE_DIR", "./data/live_feed"),
        live_feed_start_hour=_env_int("LIVE_FEED_START_HOUR", 9),
        live_feed_start_minute=_env_int("LIVE_FEED_START_MINUTE", 15),
        live_feed_end_hour=_env_int("LIVE_FEED_END_HOUR", 15),
        live_feed_end_minute=_env_int("LIVE_FEED_END_MINUTE", 30),
        live_feed_reconnect_max_backoff_seconds=_env_int("LIVE_FEED_RECONNECT_MAX_BACKOFF_SECONDS", 30),
        nifty_instrument_key=os.getenv("NIFTY_INSTRUMENT_KEY", "NSE_INDEX|Nifty 50"),
        dummy_orders_enabled=_env_bool("DUMMY_ORDERS_ENABLED", True),
        dummy_supertrend_orders_enabled=_env_bool("DUMMY_SUPERTREND_ORDERS_ENABLED", True),
        dummy_order_quantity=_env_int("DUMMY_ORDER_QUANTITY", 1),
        dummy_order_state_dir=os.getenv("DUMMY_ORDER_STATE_DIR", "./data/dummy_orders"),
        dummy_order_stop_loss_percent=float(os.getenv("DUMMY_ORDER_STOP_LOSS_PERCENT", "30")),
        dummy_target_upper_threshold=float(os.getenv("DUMMY_TARGET_UPPER_THRESHOLD", "200")),
        dummy_target_lower_threshold=float(os.getenv("DUMMY_TARGET_LOWER_THRESHOLD", "100")),
        dummy_target_points_above_upper=float(os.getenv("DUMMY_TARGET_POINTS_ABOVE_UPPER", "60")),
        dummy_target_points_middle=float(os.getenv("DUMMY_TARGET_POINTS_MIDDLE", "50")),
        dummy_target_points_below_lower=float(os.getenv("DUMMY_TARGET_POINTS_BELOW_LOWER", "40")),
        daily_data_backup_enabled=_env_bool("DAILY_DATA_BACKUP_ENABLED", False),
        daily_data_backup_dir=os.getenv("DAILY_DATA_BACKUP_DIR", "./backups"),
        daily_data_backup_hour=_env_int("DAILY_DATA_BACKUP_HOUR", 16),
        daily_data_backup_minute=_env_int("DAILY_DATA_BACKUP_MINUTE", 5),
        daily_data_backup_days=os.getenv("DAILY_DATA_BACKUP_DAYS", "mon-fri").strip(),
        daily_data_backup_collection=os.getenv("DAILY_DATA_BACKUP_COLLECTION", "daily_data_backup_state"),
        mega_enabled=_env_bool("MEGA_ENABLED", True),
        mega_email=os.getenv("MEGA_EMAIL", ""),
        mega_password=os.getenv("MEGA_PASSWORD", ""),
        mega_backup_root=os.getenv("MEGA_BACKUP_ROOT", "orderflow").strip().strip("/"),
        isolation_notify_client_key=os.getenv("ISOLATION_NOTIFY_CLIENT_KEY", ""),
        isolation_notify_client_key_header=os.getenv("ISOLATION_NOTIFY_CLIENT_KEY_HEADER", "X-Isolation-Key"),
        require_today_only_scope=_env_bool("REQUIRE_TODAY_ONLY_SCOPE", True),
        verbose=_env_bool("VERBOSE", False),
        telegram_enabled=_env_bool("TELEGRAM_ENABLED", False),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        logs_dir=os.getenv("LOGS_DIR", "./logs"),
    )
