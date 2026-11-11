# app/config.py
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

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
    # App
    app_name: str
    app_env: str
    app_host: str
    app_port: int

    # Mongo — isolation documents only
    mongo_url: str
    mongo_database: str
    isolation_events_collection: str

    # Local SR state (filesystem — replaces the old Mongo sr_state collection)
    sr_state_dir: str
    sr_store_heartbeats: bool

    # Local JSON mirror of isolation documents (empty string → disabled)
    data_dir: str

    # Upstream SR feed
    sr_ws_base_url: str
    sr_ws_base_url_local: str
    sr_ws_default_config: str
    sr_ws_keepalive_seconds: int
    sr_ws_reconnect_max_backoff_seconds: int

    # Inbound auth
    isolation_notify_client_key: str
    isolation_notify_client_key_header: str

    # Scope enforcement
    require_today_only_scope: bool

    # Verbose per-frame logging (connection_ack / snapshot / sr_update)
    # false → those frames are logged at DEBUG instead of INFO
    verbose: bool

    # Telegram
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str

    # Logging
    log_level: str
    logs_dir: str

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
        app_name=os.getenv("APP_NAME", "isolation_consumer_service"),
        app_env=os.getenv("APP_ENV", "development"),
        app_host=os.getenv("APP_HOST", "0.0.0.0"),
        app_port=_env_int("APP_PORT", 8003),
        mongo_url=os.getenv("MONGO_URL", "mongodb://localhost:27017"),
        mongo_database=os.getenv("MONGO_DATABASE", "ISOLATION_CONSUMER"),
        isolation_events_collection=os.getenv(
            "ISOLATION_EVENTS_COLLECTION", "isolation_events"
        ),
        sr_state_dir=os.getenv("SR_STATE_DIR", "./data/sr_state"),
        sr_store_heartbeats=_env_bool("SR_STORE_HEARTBEATS", False),
        data_dir=os.getenv("DATA_DIR", "./data"),
        sr_ws_base_url=os.getenv("SR_WS_BASE_URL", "wss://ema.novag7.in"),
        sr_ws_base_url_local=os.getenv("SR_WS_BASE_URL_LOCAL", ""),
        sr_ws_default_config=os.getenv("SR_WS_DEFAULT_CONFIG", "all"),
        sr_ws_keepalive_seconds=_env_int("SR_WS_KEEPALIVE_SECONDS", 30),
        sr_ws_reconnect_max_backoff_seconds=_env_int(
            "SR_WS_RECONNECT_MAX_BACKOFF_SECONDS", 30
        ),
        isolation_notify_client_key=os.getenv("ISOLATION_NOTIFY_CLIENT_KEY", ""),
        isolation_notify_client_key_header=os.getenv(
            "ISOLATION_NOTIFY_CLIENT_KEY_HEADER", "X-Isolation-Key"
        ),
        require_today_only_scope=_env_bool("REQUIRE_TODAY_ONLY_SCOPE", True),
        verbose=_env_bool("VERBOSE", False),
        telegram_enabled=_env_bool("TELEGRAM_ENABLED", False),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        logs_dir=os.getenv("LOGS_DIR", "./logs"),
    )