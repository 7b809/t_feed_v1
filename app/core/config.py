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
        "http://localhost:8000/api/subscriptions",
    )
    request_timeout_seconds: float = float(
        os.getenv("REQUEST_TIMEOUT_SECONDS", "30")
    )
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    # Combined ("project") log file. Every module's records land here.
    log_file: Path = BASE_DIR / os.getenv("LOG_FILE", "logs/app.log")

    # Per-module log files. Each module that calls ``get_logger(__name__)``
    # gets its own rotating file inside ``module_log_dir``.
    module_logs_enabled: bool = _env_bool("MODULE_LOGS_ENABLED", True)
    module_log_dir: Path = BASE_DIR / os.getenv("MODULE_LOG_DIR", "logs/modules")

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

    # Job 5 configuration (REST-based live EMA)
    live_ema_enabled: bool = _env_bool("LIVE_EMA_ENABLED", True)
    live_ema_tick_offset_seconds: int = int(
        os.getenv("LIVE_EMA_TICK_OFFSET_SECONDS", "10")
    )
    live_ema_idle_sleep_seconds: int = int(
        os.getenv("LIVE_EMA_IDLE_SLEEP_SECONDS", "30")
    )

    # Instrument-level logging
    instrument_errors_only: bool = _env_bool("INSTRUMENT_ERRORS_ONLY", False)

    # REST live-EMA test mode (ignored when USE_LIVE_LTP_FEED is true).
    ema_test_mode: bool = _env_bool("EMA_TEST_MODE", False)

    # Job 6 configuration — LTP WebSocket feed backend.
    #   true  -> live EMA is computed from the upstream LTP feed.
    #   false -> live EMA is computed by the REST polling job.
    use_live_ltp_feed: bool = _env_bool("USE_LIVE_LTP_FEED", True)
    live_feed_url: str = os.getenv(
        "LIVE_FEED_URL", "wss://feed.novag7.in/ws/market"
    )
    live_feed_reconnect_seconds: int = int(
        os.getenv("LIVE_FEED_RECONNECT_SECONDS", "5")
    )
    live_feed_ping_interval_seconds: int = int(
        os.getenv("LIVE_FEED_PING_INTERVAL_SECONDS", "20")
    )
    live_feed_max_connections: int = int(
        os.getenv("LIVE_FEED_MAX_CONNECTIONS", "200")
    )

    # Bucket interval for LTP-based live EMA. 60 -> 1-minute candles.
    live_ema_interval_seconds: int = int(
        os.getenv("LIVE_EMA_INTERVAL_SECONDS", "60")
    )

settings = Settings()