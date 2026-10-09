import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Project root: ordered_instrument_jobs_fastapi/
BASE_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = BASE_DIR / ".env"

# Loads secrets/configuration from .env when it exists.
# Existing system environment variables are not overwritten.
load_dotenv(dotenv_path=ENV_FILE, override=False)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True, slots=True)
class Settings:
    subscriptions_api_url: str = os.getenv(
        "SUBSCRIPTIONS_API_URL",
        "https://feed.novag7.in/api/subscriptions",
    )
    request_timeout_seconds: float = float(
        os.getenv("REQUEST_TIMEOUT_SECONDS", "30")
    )
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_file: Path = BASE_DIR / os.getenv("LOG_FILE", "logs/app.log")

    # Job 2 configuration
    data_dir: Path = BASE_DIR / os.getenv("DATA_DIR", "data")
    history_lookback_days: int = int(os.getenv("HISTORY_LOOKBACK_DAYS", "10"))
    history_max_concurrency: int = int(os.getenv("HISTORY_MAX_CONCURRENCY", "8"))

    # Job 3 configuration
    market_timezone: str = os.getenv("MARKET_TIMEZONE", "Asia/Kolkata")
    market_open_time: str = os.getenv("MARKET_OPEN_TIME", "09:15")
    market_close_time: str = os.getenv("MARKET_CLOSE_TIME", "15:40")

    # Job 4 configuration
    ema_fast_period: int = int(os.getenv("EMA_FAST_PERIOD", "9"))
    ema_slow_period: int = int(os.getenv("EMA_SLOW_PERIOD", "21"))

    # Job 5 configuration
    live_ema_enabled: bool = _env_bool("LIVE_EMA_ENABLED", True)
    live_ema_tick_offset_seconds: int = int(
        os.getenv("LIVE_EMA_TICK_OFFSET_SECONDS", "10")
    )
    live_ema_idle_sleep_seconds: int = int(
        os.getenv("LIVE_EMA_IDLE_SLEEP_SECONDS", "30")
    )


settings = Settings()