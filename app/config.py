# app/config.py

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


# ---------- Strategy index priority (env-overridable) ----------
_DEFAULT_STRATEGY_INDEX_PRIORITY: dict[str, int] = {
    "NIFTY": 1,
    "BANKNIFTY": 2,
    "MIDCPNIFTY": 3,
    "SENSEX": 4,
}


def _load_strategy_index_priority() -> dict[str, int]:
    result: dict[str, int] = {}
    for name, default in _DEFAULT_STRATEGY_INDEX_PRIORITY.items():
        env_key = f"STRATEGY_INDEX_PRIORITY_{name}"
        result[name] = _int(env_key, default)
    return result


STRATEGY_INDEX_PRIORITY: dict[str, int] = _load_strategy_index_priority()


# ---------- Canonical 4-index catalog ----------
INDEX_METADATA: dict[str, dict[str, Any]] = {
    "NSE_INDEX|Nifty 50": {
        "instrument_key": "NSE_INDEX|Nifty 50",
        "canonical": "NIFTY",
        "folder": "nifty",
        "trading_symbol": "NIFTY",
        "underlying": "NIFTY",
        "strike_price": None,
        "strike_type": "INDEX",
        "expiry": None,
        "priority": int(STRATEGY_INDEX_PRIORITY.get("NIFTY", 1)),
    },
    "NSE_INDEX|Nifty Bank": {
        "instrument_key": "NSE_INDEX|Nifty Bank",
        "canonical": "BANKNIFTY",
        "folder": "banknifty",
        "trading_symbol": "BANKNIFTY",
        "underlying": "NIFTY BANK",
        "strike_price": None,
        "strike_type": "INDEX",
        "expiry": None,
        "priority": int(STRATEGY_INDEX_PRIORITY.get("BANKNIFTY", 2)),
    },
    "NSE_INDEX|NIFTY MID SELECT": {
        "instrument_key": "NSE_INDEX|NIFTY MID SELECT",
        "canonical": "MIDCPNIFTY",
        "folder": "midcpnifty",
        "trading_symbol": "MIDCPNIFTY",
        "underlying": "NIFTY MID SELECT",
        "strike_price": None,
        "strike_type": "INDEX",
        "expiry": None,
        "priority": int(STRATEGY_INDEX_PRIORITY.get("MIDCPNIFTY", 3)),
    },
    "BSE_INDEX|SENSEX": {
        "instrument_key": "BSE_INDEX|SENSEX",
        "canonical": "SENSEX",
        "folder": "sensex",
        "trading_symbol": "SENSEX",
        "underlying": "SENSEX",
        "strike_price": None,
        "strike_type": "INDEX",
        "expiry": None,
        "priority": int(STRATEGY_INDEX_PRIORITY.get("SENSEX", 4)),
    },
}


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
    ema_fast_period: int = _int("EMA_FAST_PERIOD", 9)
    ema_slow_period: int = _int("EMA_SLOW_PERIOD", 21)
    ema_candle_count: int = _int("EMA_CANDLE_COUNT", 3000)
    ema_history_days: int = _int("EMA_HISTORY_DAYS", 10)
    ema_max_workers: int = _int("EMA_MAX_WORKERS", 5)
    ema_poll_seconds: int = _int("EMA_POLL_SECONDS", 60)
    ema_heartbeat_cycles: int = _int("EMA_HEARTBEAT_CYCLES", 1)
    ema_cycle_log_every: int = _int("EMA_CYCLE_LOG_EVERY", 100)
    ema_fetch_timeout_seconds: float = _float("EMA_FETCH_TIMEOUT_SECONDS", 30.0)

    ema_index_instruments: str = os.getenv(
        "EMA_INDEX_INSTRUMENTS",
        "NSE_INDEX|Nifty 50,NSE_INDEX|Nifty Bank,NSE_INDEX|NIFTY MID SELECT,BSE_INDEX|SENSEX",
    )
    ema_instruments_file: Path = Path(
        os.getenv("EMA_INSTRUMENTS_FILE", "./storage/mega/all_instruments.json")
    )

    # ---------- Job 3: Pivot levels ----------
    levels_enabled: bool = _bool("LEVELS_ENABLED", True)
    levels_compute_hour: int = _int("LEVELS_COMPUTE_HOUR", 9)
    levels_compute_minute: int = _int("LEVELS_COMPUTE_MINUTE", 17)
    levels_startup_window_start_hour: int = _int("LEVELS_STARTUP_WINDOW_START_HOUR", 9)
    levels_startup_window_start_minute: int = _int("LEVELS_STARTUP_WINDOW_START_MINUTE", 17)
    levels_startup_window_end_hour: int = _int("LEVELS_STARTUP_WINDOW_END_HOUR", 23)
    levels_startup_window_end_minute: int = _int("LEVELS_STARTUP_WINDOW_END_MINUTE", 59)
    levels_first_candle_hour: int = _int("LEVELS_FIRST_CANDLE_HOUR", 9)
    levels_first_candle_minute: int = _int("LEVELS_FIRST_CANDLE_MINUTE", 15)
    levels_first_candle_window_minutes: int = _int("LEVELS_FIRST_CANDLE_WINDOW_MINUTES", 1)

    # --- Job 3 — post-compute: batch retries + MongoDB persistence ---
    # Number of ADDITIONAL batch attempts (on top of the initial one) when
    # the scheduled 09:17 compute does not succeed for all indexes.
    levels_scheduled_retries: int = _int("LEVELS_SCHEDULED_RETRIES", 3)
    # Gap between batch retries (seconds).
    levels_scheduled_retry_gap_seconds: float = _float(
        "LEVELS_SCHEDULED_RETRY_GAP_SECONDS", 60.0
    )
    # MongoDB collection + fixed document _id used to persist today's levels.
    # Daily scheduled runs overwrite the same _id — no new docs accumulate.
    levels_project_info_collection: str = os.getenv(
        "LEVELS_PROJECT_INFO_COLLECTION", "project_info"
    )
    levels_project_info_document_id: str = os.getenv(
        "LEVELS_PROJECT_INFO_DOCUMENT_ID", "pivot_levels"
    )

    # ---------- Logging ----------
    logs_dir: Path = Path(os.getenv("LOGS_DIR", "./logs"))

    # ---------- Telegram notifications ----------
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

        if not (0 <= self.levels_compute_hour < 24 and 0 <= self.levels_compute_minute < 60):
            raise ValueError("Invalid LEVELS_COMPUTE_HOUR/MINUTE")
        if not (
            0 <= self.levels_startup_window_start_hour < 24
            and 0 <= self.levels_startup_window_start_minute < 60
        ):
            raise ValueError("Invalid LEVELS_STARTUP_WINDOW_START_HOUR/MINUTE")
        if not (
            0 <= self.levels_startup_window_end_hour < 24
            and 0 <= self.levels_startup_window_end_minute < 60
        ):
            raise ValueError("Invalid LEVELS_STARTUP_WINDOW_END_HOUR/MINUTE")
        if not (
            0 <= self.levels_first_candle_hour < 24
            and 0 <= self.levels_first_candle_minute < 60
        ):
            raise ValueError("Invalid LEVELS_FIRST_CANDLE_HOUR/MINUTE")
        if self.levels_first_candle_window_minutes < 1:
            raise ValueError("LEVELS_FIRST_CANDLE_WINDOW_MINUTES must be >= 1")

        # Job 3 — post-compute
        if self.levels_scheduled_retries < 0:
            raise ValueError("LEVELS_SCHEDULED_RETRIES must be >= 0")
        if self.levels_scheduled_retry_gap_seconds <= 0:
            raise ValueError("LEVELS_SCHEDULED_RETRY_GAP_SECONDS must be > 0")
        if not self.levels_project_info_collection:
            raise ValueError("LEVELS_PROJECT_INFO_COLLECTION must be non-empty")
        if not self.levels_project_info_document_id:
            raise ValueError("LEVELS_PROJECT_INFO_DOCUMENT_ID must be non-empty")

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