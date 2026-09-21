import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

def load_env() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(dotenv_path=env_path)


def require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing mandatory environment variable: {name}")
    return value.strip()


def env_str(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_optional_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return int(value) if value else None


def parse_clock(value: str) -> time:
    return time.fromisoformat(value)


load_env()

MONGO_URI = require_env("MONGO_URI")
MONGO_DB = require_env("MONGO_DB")
TOKENS_COLLECTION = require_env("TOKENS_COLLECTION")
TOKEN_DOCUMENT_ID = env_str("TOKEN_DOCUMENT_ID", "upstox_access_token")
TOKEN_REFRESH_SECONDS = env_int("TOKEN_REFRESH_SECONDS", 300)

MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
MAIN_NIFTY_SECURITY = "NSE_INDEX|Nifty 50"
STRIKE_FROM = env_float("STRIKE_FROM", 22000)
STRIKE_TO = env_float("STRIKE_TO", 26000)
TEST_FLAG = env_bool("TEST_FLAG", True)
TEST_ITEM_COUNT = env_int("TEST_ITEM_COUNT", 5)
TEST_RANDOM_SEED = env_optional_int("TEST_RANDOM_SEED")
DAILY_REFRESH_TIME = parse_clock(env_str("DAILY_REFRESH_TIME", "09:00"))
MARKET_OPEN_TIME = parse_clock(env_str("MARKET_OPEN_TIME", "09:15"))
MARKET_CLOSE_TIME = parse_clock(env_str("MARKET_CLOSE_TIME", "15:30"))
LIVE_POLL_DELAY_SECONDS = env_int("LIVE_POLL_DELAY_SECONDS", 3)
SCHEDULER_IDLE_SECONDS = env_float("SCHEDULER_IDLE_SECONDS", 1.0)
EMA_FAST_PERIOD = env_int("EMA_FAST_PERIOD", 9)
EMA_SLOW_PERIOD = env_int("EMA_SLOW_PERIOD", 21)
HISTORICAL_TRADING_DAYS = env_int("HISTORICAL_TRADING_DAYS", 7)
HISTORICAL_LOOKBACK_DAYS = env_int("HISTORICAL_LOOKBACK_DAYS", 15)
MINIMUM_LOOKBACK_DAYS = env_int("MINIMUM_LOOKBACK_DAYS", 1)
MAX_WORKERS = env_int("MAX_WORKERS", 5)
OHLC_BATCH_SIZE = env_int("OHLC_BATCH_SIZE", 500)
OHLC_INTERVAL = env_str("OHLC_INTERVAL", "I1")
API_MAX_RETRIES = env_int("API_MAX_RETRIES", 3)
API_RETRY_BASE_DELAY_SECONDS = env_float("API_RETRY_BASE_DELAY_SECONDS", 1.0)
API_CALL_DELAY_SECONDS = env_float("API_CALL_DELAY_SECONDS", 0.25)
SAVE_ALL_COMPLETED_CANDLES = env_bool("SAVE_ALL_COMPLETED_CANDLES", False)
DATA_ROOT = Path(env_str("DATA_ROOT", "data"))
LOG_LEVEL = env_str("LOG_LEVEL", "INFO")

API_HOST = env_str("API_HOST", "0.0.0.0")
API_PORT = env_int("API_PORT", 8000)


API_AUTH_ENABLED = env_bool("API_AUTH_ENABLED", False)
ADMIN_API_KEY = env_str("ADMIN_API_KEY", "")

if API_AUTH_ENABLED and not ADMIN_API_KEY:
    raise RuntimeError(
        "ADMIN_API_KEY is required when API_AUTH_ENABLED=true"
    )
    
TELEGRAM_ENABLED = env_bool("TELEGRAM_ENABLED", False)
TELEGRAM_BOT_TOKEN = env_str("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = env_str("TELEGRAM_CHAT_ID", "")
TELEGRAM_TIMEOUT_SECONDS = env_float("TELEGRAM_TIMEOUT_SECONDS", 10.0)

if TELEGRAM_ENABLED and (not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID):
    raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required when TELEGRAM_ENABLED=true")

if STRIKE_FROM > STRIKE_TO:
    raise RuntimeError("STRIKE_FROM cannot be greater than STRIKE_TO")
if OHLC_INTERVAL not in {"I1", "I30", "1d"}:
    raise RuntimeError("OHLC_INTERVAL must be I1, I30, or 1d")

CONTRACTS_FILE = DATA_ROOT / "nearest_nifty_option_contracts.json"
RUNTIME_ROOT = DATA_ROOT / "runtime"
EMA_STATE_ROOT = DATA_ROOT / "ema_state"
CROSSOVER_ROOT = DATA_ROOT / "crossovers"
CANDLE_ROOT = DATA_ROOT / "candles"
LOOKBACK_ATTEMPTS = tuple(
    value for value in (15, 14, 12, 10, 8, 7, 6, 5, 4, 3, 2, 1)
    if MINIMUM_LOOKBACK_DAYS <= value <= HISTORICAL_LOOKBACK_DAYS
)
