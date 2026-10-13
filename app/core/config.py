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

def _env_str(name: str, default: str = "") -> str:
    """Read a string env var and strip surrounding whitespace/quotes."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().strip('"').strip("'")

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

    # Daily maintenance refresh (jobs 1 → 4) on market days (Mon–Fri).
    # Runs once per day at ``daily_refresh_time`` in ``market_timezone``.
    daily_refresh_enabled: bool = _env_bool("DAILY_REFRESH_ENABLED", True)
    daily_refresh_time: str = os.getenv("DAILY_REFRESH_TIME", "08:40")

    # ------------------------------------------------------------------
    # CORS
    # ------------------------------------------------------------------
    # Comma-separated list of allowed origins.
    #   "*"                    -> allow every origin (no credentials)
    #   "https://a.com,..."    -> explicit allow-list
    cors_allowed_origins: str = os.getenv("CORS_ALLOWED_ORIGINS", "*")

    # Only honoured when CORS_ALLOWED_ORIGINS is NOT "*".
    # Required for cookie-based auth; leave false for token auth.
    cors_allow_credentials: bool = _env_bool("CORS_ALLOW_CREDENTIALS", False)

    # ------------------------------------------------------------------
    # Public WebSocket base URL (optional)
    # ------------------------------------------------------------------
    # Used by /api/instruments and /api/hard-refresh to build the
    # per-instrument ``connect_url`` pointing at our live-EMA WebSocket.
    #
    #   ""  (default) -> fall back to the incoming request's scheme/host,
    #                    honouring X-Forwarded-Proto / X-Forwarded-Host.
    #   "wss://feed.novag7.in" -> hard-code a public base URL (useful
    #                    when the app sits behind a reverse proxy).
    public_ws_base_url: str = _env_str("PUBLIC_WS_BASE_URL", "")

    # ------------------------------------------------------------------
    # Telegram notifications (optional, best-effort)
    # ------------------------------------------------------------------
    #   true  -> send formatted startup/shutdown messages to TELEGRAM_CHAT_ID
    #   false -> notifications disabled entirely
    #
    # No Telegram package is required; the Bot HTTP API is called directly
    # through ``requests`` from app/services/telegram_service.py.
    telegram_enabled: bool = _env_bool("TELEGRAM_ENABLED", False)

    # Bot token from @BotFather (e.g. "123456789:AA...").
    telegram_bot_token: str = _env_str("TELEGRAM_BOT_TOKEN", "")

    # Target chat id. Accepts numeric user ids, negative group ids, or
    # a channel handle like "@mychannel". Surrounding quotes are stripped.
    telegram_chat_id: str = _env_str("TELEGRAM_CHAT_ID", "")

    # Per-request timeout for the Telegram API, in seconds.
    telegram_timeout_seconds: int = int(
        os.getenv("TELEGRAM_TIMEOUT_SECONDS", "10")
    )

    # ------------------------------------------------------------------
    # WebSocket recorder (self-connect for offline replay / analysis)
    # ------------------------------------------------------------------
    # When true, a background service opens one WebSocket connection to
    # our own live-EMA endpoint for the Nifty 50 index, waits for the
    # first tick, then opens a second connection to a randomly chosen
    # instrument whose strike is nearest to Nifty 50's LTP. Every frame
    # received is appended as a single JSON line to a per-session .jsonl
    # file under ``ws_recorder_dir``.
    #
    # NOTE: the recorder is a WebSocket *client* of our own server. It
    # therefore only sees data when the live EMA backend is active
    # (USE_LIVE_LTP_FEED=true, LIVE_EMA_ENABLED=true).
    ws_recorder_enabled: bool = _env_bool("WS_RECORDER_ENABLED", False)

    # Base URL of our own server (the recorder connects to this).
    # Empty -> fall back to PUBLIC_WS_BASE_URL, then ws://127.0.0.1:8002.
    ws_recorder_base_url: str = _env_str("WS_RECORDER_BASE_URL", "")

    # Root folder for recorded files.
    #   <ws_recorder_dir>/<safe_instrument_key>/<YYYYMMDD_HHMMSS>.jsonl
    ws_recorder_dir: Path = BASE_DIR / os.getenv(
        "WS_RECORDER_DIR", "ws_records"
    )

    # The index instrument to record first (the "spot" reference).
    ws_recorder_nifty_key: str = os.getenv(
        "WS_RECORDER_NIFTY_KEY", "NSE_INDEX|Nifty 50"
    )

    # Reconnect backoff for the recorder's own client connections.
    ws_recorder_reconnect_seconds: int = int(
        os.getenv("WS_RECORDER_RECONNECT_SECONDS", "5")
    )

settings = Settings()