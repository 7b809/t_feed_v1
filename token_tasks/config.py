"""
token_tasks/config.py

Environment-backed configuration for the token task.
"""
import os

from core.logger import get_logger

logger = get_logger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


class TokenConfig:
    # ---- Mongo document ------------------------------------------------
    COLLECTION_NAME = _env_str("TOKEN_COLLECTION_NAME", "upstox_tokens")
    DOC_ID = _env_str("TOKEN_DOC_ID", "upstox_access_token")

    # ---- Refresh scheduler --------------------------------------------
    REFRESH_INTERVAL_SECONDS = _env_int("TOKEN_REFRESH_INTERVAL_SECONDS", 1800)
    LOAD_ON_STARTUP = _env_bool("TOKEN_LOAD_ON_STARTUP", True)

    # ---- Health watchdog ----------------------------------------------
    WATCHDOG_ENABLED = _env_bool("TOKEN_WATCHDOG_ENABLED", True)
    WATCHDOG_INTERVAL_SEC = _env_int("TOKEN_WATCHDOG_INTERVAL_SEC", 300)
    WATCHDOG_NOTIFY_COOLDOWN_SEC = _env_int("TOKEN_WATCHDOG_NOTIFY_COOLDOWN_SEC", 1800)
    WATCHDOG_RUN_ON_START = _env_bool("TOKEN_WATCHDOG_RUN_ON_START", False)


token_config = TokenConfig()