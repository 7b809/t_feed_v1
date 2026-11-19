"""Environment-backed configuration for the Upstox order sandbox service."""
from __future__ import annotations

import logging
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
# UPSTOX_API_VERSION must be "1.0" or "2.0". An empty value from .env
# (e.g. a line `UPSTOX_API_VERSION=` with nothing after the '=') is
# silently returned by os.getenv as "" rather than falling back to the
# default; the Upstox SDK then raises ValueError from UserApi.get_profile
# before any HTTP request is made. Sanitize here so the rest of the app
# can trust the value.
_RAW_UPSTOX_API_VERSION = (os.getenv("UPSTOX_API_VERSION") or "2.0").strip()
if _RAW_UPSTOX_API_VERSION not in {"1.0", "2.0"}:
    # Logging before core.logger is importable (circular import) — use the
    # standard library logger so the warning at least reaches stderr if
    # anything is configured.
    logging.getLogger(__name__).warning(
        "Invalid UPSTOX_API_VERSION=%r in .env; falling back to '2.0' "
        "(valid values: '1.0', '2.0')",
        _RAW_UPSTOX_API_VERSION,
    )
    _RAW_UPSTOX_API_VERSION = "2.0"
UPSTOX_API_VERSION = _RAW_UPSTOX_API_VERSION

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

# Port precedence: API_PORT > APP_PORT > 8004.
API_PORT = int(
    os.getenv("API_PORT", os.getenv("APP_PORT", "8004"))
)
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")

# Daily MEGA data backup (mirrors isolation_v1 configuration).
DAILY_DATA_BACKUP_ENABLED = env_bool("DAILY_DATA_BACKUP_ENABLED", True)
DAILY_DATA_BACKUP_DIR = os.getenv("DAILY_DATA_BACKUP_DIR", "./backups")
DAILY_DATA_BACKUP_HOUR = int(os.getenv("DAILY_DATA_BACKUP_HOUR", "16"))
DAILY_DATA_BACKUP_MINUTE = int(os.getenv("DAILY_DATA_BACKUP_MINUTE", "5"))
DAILY_DATA_BACKUP_DAYS = os.getenv("DAILY_DATA_BACKUP_DAYS", "mon-fri").strip()
DAILY_DATA_BACKUP_COLLECTION = os.getenv(
    "DAILY_DATA_BACKUP_COLLECTION", "daily_data_backup_state"
)
MEGA_ENABLED = env_bool("MEGA_ENABLED", True)
MEGA_EMAIL = os.getenv("MEGA_EMAIL", "").strip()
MEGA_PASSWORD = os.getenv("MEGA_PASSWORD", "").strip()
MEGA_BACKUP_ROOT = os.getenv("MEGA_BACKUP_ROOT", "sandbox_v1").strip().strip("/")
APP_NAME = os.getenv("APP_NAME", "sandbox_v1").strip() or "sandbox_v1"

# Optional Telegram notifications (isolation_v1-compatible settings).
TELEGRAM_ENABLED = env_bool("TELEGRAM_ENABLED", True)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Upstox instrument search client and local cache.
UPSTOX_USER_AGENT = os.getenv(
    "UPSTOX_USER_AGENT", "sandbox_v1/1.0 (Upstox instrument-search integration)"
).strip()
UPSTOX_HTTP_TIMEOUT_SECONDS = max(3, int(os.getenv("UPSTOX_HTTP_TIMEOUT_SECONDS", "12")))
UPSTOX_INSTRUMENT_CACHE_ENABLED = env_bool("UPSTOX_INSTRUMENT_CACHE_ENABLED", True)
UPSTOX_INSTRUMENT_CACHE_MAX_STALE_SECONDS = max(
    0, int(os.getenv("UPSTOX_INSTRUMENT_CACHE_MAX_STALE_SECONDS", "604800"))
)

# Periodic Upstox token validation.
TOKEN_CHECK_ENABLED = env_bool("TOKEN_CHECK_ENABLED", True)
TOKEN_CHECK_INTERVAL_MINUTES = max(
    1, int(os.getenv("TOKEN_CHECK_INTERVAL_MINUTES", "30"))
)

ORDERS_VERBOSE: bool = os.getenv("ORDERS_VERBOSE", "false").lower() in ("1", "true", "yes")

# Server-to-server authentication for external trading integrations.
# One shared 32-character secret is configured on the source project and
# supplied in each external API JSON payload as "api_token".
EXTERNAL_API_AUTH_ENABLED = env_bool("EXTERNAL_API_AUTH_ENABLED", False)
EXTERNAL_API_SOURCE_TOKEN = os.getenv("EXTERNAL_API_SOURCE_TOKEN", "").strip()
