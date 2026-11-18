"""Environment-backed configuration for the order-management service."""
from __future__ import annotations
import os
from functools import lru_cache
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]

def load_env() -> None:
    """Load .env without overriding exported environment variables."""
    load_dotenv(ROOT_DIR / ".env", override=False)

load_env()

class Settings:
    APP_NAME = os.getenv("APP_NAME", "Upstox Order Management API")
    APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
    APP_PORT = int(os.getenv("APP_PORT", "8010"))
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
    LOG_DIR = Path(os.getenv("LOG_DIR", str(ROOT_DIR / "logs")))
    DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT_DIR / "data")))
    ORDER_API_KEY = os.getenv("ORDER_API_KEY", "")
    UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    UPSTOX_API_VERSION = os.getenv("UPSTOX_API_VERSION", "2.0")
    UPSTOX_ORDER_API_VERSION = os.getenv("UPSTOX_ORDER_API_VERSION", "v3")
    TRADING_ENABLED = os.getenv("TRADING_ENABLED", "false").strip().lower() == "true"
    DEFAULT_TRANSACTION_TYPE = os.getenv("DEFAULT_TRANSACTION_TYPE", "BUY").upper()
    DEFAULT_QUANTITY = int(os.getenv("DEFAULT_QUANTITY", "65"))
    DEFAULT_ORDER_TYPE = os.getenv("DEFAULT_ORDER_TYPE", "MARKET").upper()
    DEFAULT_PRODUCT = os.getenv("DEFAULT_PRODUCT", "I").upper()
    DEFAULT_VALIDITY = os.getenv("DEFAULT_VALIDITY", "DAY").upper()
    DEFAULT_MARKET_PROTECTION = int(os.getenv("DEFAULT_MARKET_PROTECTION", "-1"))
    MONGO_URL = os.getenv("MONGO_URL", "")
    MONGO_DATABASE = os.getenv("MONGO_DATABASE", "UPSTOX_APP_V2")
    MONGO_ORDERS_COLLECTION = os.getenv("MONGO_ORDERS_COLLECTION", "sandbox_orders")
    MONGO_SERVER_SELECTION_TIMEOUT_MS = int(os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "5000"))

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

settings = get_settings()
