"""Environment-backed configuration for the Upstox order sandbox service."""
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# MongoDB token source and daily order ledger.
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "UPSTOX_APP_V2")
APP_TOKENS_COLLECTION = os.getenv("APP_TOKENS_COLLECTION", "app_tokens")
TOKEN_DOC_ID = os.getenv("TOKEN_DOC_ID", "upstox_access_token")
ORDERS_COLLECTION = os.getenv("ORDERS_COLLECTION", "sandbox_orders")

# Upstox SDK and trade-safety controls.
UPSTOX_API_VERSION = os.getenv("UPSTOX_API_VERSION", "2.0")
UPSTOX_SANDBOX = env_bool("UPSTOX_SANDBOX", True)
TRADING_ENABLED = env_bool("TRADING_ENABLED", False)
DEFAULT_ORDER_QUANTITY = int(os.getenv("DEFAULT_ORDER_QUANTITY", "65"))
DEFAULT_TRANSACTION_TYPE = os.getenv("DEFAULT_TRANSACTION_TYPE", "BUY").upper()
DEFAULT_PRODUCT = os.getenv("DEFAULT_PRODUCT", "I").upper()
DEFAULT_ORDER_TYPE = os.getenv("DEFAULT_ORDER_TYPE", "MARKET").upper()
DEFAULT_VALIDITY = os.getenv("DEFAULT_VALIDITY", "DAY").upper()

# Runtime folders.
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
if not DATA_DIR.is_absolute():
    DATA_DIR = (PROJECT_ROOT / DATA_DIR).resolve()
ORDERS_DATA_DIR = DATA_DIR / "orders"
LOGS_DIR = PROJECT_ROOT / os.getenv("LOGS_DIR", "logs")
LOGS_TEXT_DIR = PROJECT_ROOT / os.getenv("LOGS_TEXT_DIR", "logs_text")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Network binding. By default the service listens on all interfaces.
ENABLE_IP_BINDING = env_bool("ENABLE_IP_BINDING", False)
BIND_IP = os.getenv("BIND_IP", "127.0.0.1").strip()
API_HOST = BIND_IP if ENABLE_IP_BINDING else "0.0.0.0"
API_PORT = int(os.getenv("API_PORT", "8004"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")

# Optional Telegram notifications (isolation_v1-compatible settings).
TELEGRAM_ENABLED = env_bool("TELEGRAM_ENABLED", True)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
