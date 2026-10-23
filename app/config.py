# app/config.py

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc

def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc

@dataclass(frozen=True)
class Settings:
    # ---------- App ----------
    app_name: str = os.getenv("APP_NAME", "mega_archive_service")
    app_env: str = os.getenv("APP_ENV", "development")
    app_host: str = os.getenv("APP_HOST", "0.0.0.0")
    app_port: int = _int("APP_PORT", 8000)
    api_key: str = os.getenv("API_KEY", "")

    # ---------- MongoDB ----------
    mongo_url: str = os.getenv("MONGO_URL", "")
    mongo_database: str = os.getenv("MONGO_DATABASE", "UPSTOX_APP_V2")
    mongo_collection: str = os.getenv("MONGO_COLLECTION", "app_tokens")
    mongo_document_id: str = os.getenv("MONGO_DOCUMENT_ID", "")
    mongo_document_id_type: str = os.getenv("MONGO_DOCUMENT_ID_TYPE", "string")
    mongo_query_json: str = os.getenv("MONGO_QUERY_JSON", '{"project":"candle-backup"}')
    mongo_timeout_ms: int = _int("MONGO_TIMEOUT_MS", 5000)

    # ---------- Job 1: MEGA archive ----------
    mega_destination_dir: Path = Path(os.getenv("MEGA_DESTINATION_DIR", "./storage/mega"))
    mega_download_dir: Path = Path(os.getenv("MEGA_DOWNLOAD_DIR", "./storage/downloads"))
    mega_archive_filename: str = os.getenv("MEGA_ARCHIVE_FILENAME", "data.zip")
    max_archive_size_mb: int = _int("MAX_ARCHIVE_SIZE_MB", 2048)
    max_extracted_size_mb: int = _int("MAX_EXTRACTED_SIZE_MB", 10240)
    max_archive_files: int = _int("MAX_ARCHIVE_FILES", 100000)
    refresh_retries: int = _int("REFRESH_RETRIES", 3)
    retry_backoff_seconds: float = _float("RETRY_BACKOFF_SECONDS", 1.0)

    # ---------- Scheduler ----------
    scheduler_enabled: bool = _bool("SCHEDULER_ENABLED", True)
    schedule_timezone: str = os.getenv("SCHEDULE_TIMEZONE", "Asia/Kolkata")
    schedule_hour: int = _int("SCHEDULE_HOUR", 9)
    schedule_minute: int = _int("SCHEDULE_MINUTE", 0)
    schedule_weekdays: str = os.getenv("SCHEDULE_WEEKDAYS", "mon-fri")
    refresh_on_startup: bool = _bool("REFRESH_ON_STARTUP", True)

    # ---------- Cleanup ----------
    cleanup_dirs: str = os.getenv("CLEANUP_DIRS", "storage")

    # ---------- Job 2: EMA crosses ----------
    # NOTE: Upstox is accessed anonymously. No access token is used anywhere
    # in this project; historical + intraday candle endpoints are called
    # without an Authorization header.
    ema_fast_period: int = _int("EMA_FAST_PERIOD", 9)
    ema_slow_period: int = _int("EMA_SLOW_PERIOD", 21)
    ema_candle_count: int = _int("EMA_CANDLE_COUNT", 3000)
    ema_history_days: int = _int("EMA_HISTORY_DAYS", 10)
    ema_max_workers: int = _int("EMA_MAX_WORKERS", 5)
    ema_poll_seconds: int = _int("EMA_POLL_SECONDS", 60)

    # Emit a ``heartbeat`` payload per cycle (or every Nth cycle).
    # Set to 0 to disable, or N > 1 to emit only every Nth cycle.
    ema_heartbeat_cycles: int = _int("EMA_HEARTBEAT_CYCLES", 1)

    # Log the ``Cycle N complete`` line to the COMBINED project log file
    # only every N cycles. Per-module logs and console still print every cycle.
    ema_cycle_log_every: int = _int("EMA_CYCLE_LOG_EVERY", 100)

    # Timeout around the sync Upstox intraday_1m() call.
    ema_fetch_timeout_seconds: float = _float("EMA_FETCH_TIMEOUT_SECONDS", 30.0)

    ema_index_instruments: str = os.getenv(
        "EMA_INDEX_INSTRUMENTS", "NSE_INDEX|Nifty 50,BSE_INDEX|SENSEX"
    )
    ema_instruments_file: Path = Path(
        os.getenv("EMA_INSTRUMENTS_FILE", "./storage/mega/all_instruments.json")
    )

    # ---------- Logging ----------
    logs_dir: Path = Path(os.getenv("LOGS_DIR", "./logs"))

    # ---------- Telegram notifications ----------
    # Enable rich startup / shutdown / refresh / exception / EMA lifecycle
    # notifications in a Telegram chat. Requires BOT_TOKEN + CHAT_ID.
    telegram_enabled: bool = _bool("TELEGRAM_ENABLED", False)
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    def __post_init__(self) -> None:
        if self.app_port < 1 or self.app_port > 65535:
            raise ValueError("APP_PORT must be between 1 and 65535")
        if not self.mongo_database or not self.mongo_collection:
            raise ValueError("MONGO_DATABASE and MONGO_COLLECTION are required")
        if self.mongo_document_id and self.mongo_document_id_type not in {"string", "objectid", "int"}:
            raise ValueError("MONGO_DOCUMENT_ID_TYPE must be string, objectid, or int")
        try:
            json.loads(self.mongo_query_json)
        except json.JSONDecodeError as exc:
            raise ValueError("MONGO_QUERY_JSON must be valid JSON") from exc
        for name in ("max_archive_size_mb", "max_extracted_size_mb", "max_archive_files"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name.upper()} must be greater than zero")
        if self.schedule_hour not in range(24) or self.schedule_minute not in range(60):
            raise ValueError("Invalid schedule hour/minute")
        if self.ema_fast_period <= 1 or self.ema_slow_period <= self.ema_fast_period:
            raise ValueError("EMA periods must satisfy 1 < fast < slow")
        if self.ema_max_workers < 1:
            raise ValueError("EMA_MAX_WORKERS must be >= 1")
        if self.ema_poll_seconds < 5:
            raise ValueError("EMA_POLL_SECONDS must be >= 5")
        if self.ema_heartbeat_cycles < 0:
            raise ValueError("EMA_HEARTBEAT_CYCLES must be >= 0 (0 disables)")
        if self.ema_cycle_log_every < 1:
            raise ValueError("EMA_CYCLE_LOG_EVERY must be >= 1")
        if self.ema_fetch_timeout_seconds <= 0:
            raise ValueError("EMA_FETCH_TIMEOUT_SECONDS must be > 0")

    # ---------- Path helpers ----------
    @property
    def download_dir(self) -> Path:
        return self._resolve(self.mega_download_dir)

    @property
    def destination_dir(self) -> Path:
        return self._resolve(self.mega_destination_dir)

    @property
    def instruments_file(self) -> Path:
        return self._resolve(self.ema_instruments_file)

    @property
    def ema_state_dir(self) -> Path:
        return self.destination_dir / "_ema_state"

    @property
    def logs_path(self) -> Path:
        return self._resolve(self.logs_dir)

    @property
    def index_instrument_keys(self) -> list[str]:
        return [k.strip() for k in self.ema_index_instruments.split(",") if k.strip()]

    @property
    def cleanup_dirs_list(self) -> list[Path]:
        raw = (self.cleanup_dirs or "").strip()
        if not raw:
            return []
        paths: list[Path] = []
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            paths.append(self._resolve(Path(item)))
        return paths

    @staticmethod
    def _resolve(path: Path) -> Path:
        return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    @property
    def archive_limit_bytes(self) -> int:
        return self.max_archive_size_mb * 1024 * 1024

    @property
    def extracted_limit_bytes(self) -> int:
        return self.max_extracted_size_mb * 1024 * 1024

settings = Settings()