from functools import lru_cache
from pathlib import Path
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    subscriptions_api_url: str = "https://feed.novag7.in/api/subscriptions"
    request_timeout_seconds: float = 30
    raw_feed_base: str = "wss://feed.novag7.in/ws/market"
    market_timezone: str = "Asia/Kolkata"
    market_open_time: str = "09:00"
    market_close_time: str = "15:40"
    ws_ping_interval_seconds: float = 20
    ws_ping_timeout_seconds: float = 20
    ws_open_timeout_seconds: float = 15
    ws_reconnect_initial_seconds: float = 1
    ws_reconnect_multiplier: float = 2
    ws_reconnect_max_seconds: float = 30
    ws_max_concurrent_connections: int = 300
    subscriptions_refresh_seconds: float = 900
    files_dir: Path = Path("files")
    file_flush_interval_seconds: float = 5
    file_flush_every_n_messages: int = 50
    log_level: str = "INFO"
    log_file: Path = Path("logs/app.log")
    host: str = "0.0.0.0"
    port: int = 8000
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False)

    @field_validator("market_open_time", "market_close_time")
    @classmethod
    def validate_clock(cls, value: str) -> str:
        from datetime import time
        try: time.fromisoformat(value)
        except ValueError as exc: raise ValueError("Use HH:MM or HH:MM:SS") from exc
        return value

    @field_validator("ws_max_concurrent_connections", "file_flush_every_n_messages")
    @classmethod
    def validate_nonnegative(cls, value: int) -> int:
        if value < 0: raise ValueError("must be non-negative")
        return value

@lru_cache
def get_settings() -> Settings:
    return Settings()
