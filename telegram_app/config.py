"""
telegram_app/config.py

Reads telegram configuration from the environment.

Master switch
-------------
TELEGRAM_ENABLED is the top-level gate.

  TELEGRAM_ENABLED=false  -> every telegram feature is disabled, regardless
                             of what the per-event flags say. The bot does
                             not poll, no messages are sent, no cooldowns
                             run. `telegram_config.enabled` returns False
                             and every `send_message` call is a no-op.

  TELEGRAM_ENABLED=true   -> telegram is active. Credentials are required.
                             Each notify_* call is then governed by its own
                             per-event flag (NOTIFY_STARTUP, NOTIFY_SHUTDOWN,
                             NOTIFY_DAILY_REFRESH, NOTIFY_HARD_REFRESH,
                             NOTIFY_TOKEN_EVENTS).

  TELEGRAM_ENABLED unset  -> falls back to a derived default: enabled iff
                             both BOT_TOKEN and CHAT_ID are present.
                             (Backwards compatible with the previous
                             behaviour.)

Per-event flags
---------------
- TELEGRAM_NOTIFY_STARTUP
- TELEGRAM_NOTIFY_SHUTDOWN
- TELEGRAM_NOTIFY_DAILY_REFRESH
- TELEGRAM_NOTIFY_HARD_REFRESH
- TELEGRAM_NOTIFY_TOKEN_EVENTS

Each defaults to true and is only consulted when the master switch is on.
"""
import os
from typing import Optional

from core.logger import get_logger

logger = get_logger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_bool_optional(name: str) -> Optional[bool]:
    """Return True/False if the env var is set, None if it is absent."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return None
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class TelegramConfig:
    # ---- master switch --------------------------------------------------
    # Tri-state: True, False, or None (meaning "derive from credentials").
    ENABLED_RAW: Optional[bool] = _env_bool_optional("TELEGRAM_ENABLED")

    # ---- credentials ----------------------------------------------------
    BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    # ---- endpoints ------------------------------------------------------
    API_BASE: str = "https://api.telegram.org"

    # ---- polling --------------------------------------------------------
    POLLING_ENABLED: bool = _env_bool("TELEGRAM_POLLING_ENABLED", True)
    POLLING_TIMEOUT_SEC: int = _env_int("TELEGRAM_POLLING_TIMEOUT_SEC", 25)
    POLLING_RETRY_SEC: int = _env_int("TELEGRAM_POLLING_RETRY_SEC", 5)
    HTTP_TIMEOUT_SEC: int = _env_int("TELEGRAM_HTTP_TIMEOUT_SEC", 15)

    # ---- per-event flags ------------------------------------------------
    NOTIFY_STARTUP: bool = _env_bool("TELEGRAM_NOTIFY_STARTUP", True)
    NOTIFY_SHUTDOWN: bool = _env_bool("TELEGRAM_NOTIFY_SHUTDOWN", True)
    NOTIFY_DAILY_REFRESH: bool = _env_bool("TELEGRAM_NOTIFY_DAILY_REFRESH", True)
    NOTIFY_HARD_REFRESH: bool = _env_bool("TELEGRAM_NOTIFY_HARD_REFRESH", True)
    NOTIFY_TOKEN_EVENTS: bool = _env_bool("TELEGRAM_NOTIFY_TOKEN_EVENTS", True)

    @property
    def has_credentials(self) -> bool:
        return bool(self.BOT_TOKEN and self.CHAT_ID)

    @property
    def enabled(self) -> bool:
        """
        The single source of truth for "is telegram active?".

        Rules:
          1. TELEGRAM_ENABLED explicitly false -> disabled, always.
          2. TELEGRAM_ENABLED explicitly true  -> enabled iff credentials exist.
          3. TELEGRAM_ENABLED unset            -> enabled iff credentials exist.
        """
        if self.ENABLED_RAW is False:
            return False
        return self.has_credentials

    @property
    def reason(self) -> str:
        """Human-readable explanation for the current state."""
        if self.ENABLED_RAW is False:
            return "master switch TELEGRAM_ENABLED=false"
        if not self.BOT_TOKEN:
            return "TELEGRAM_BOT_TOKEN missing"
        if not self.CHAT_ID:
            return "TELEGRAM_CHAT_ID missing"
        return "enabled"

    # ---- URL helpers ----------------------------------------------------
    def send_url(self) -> str:
        return f"{self.API_BASE}/bot{self.BOT_TOKEN}/sendMessage"

    def updates_url(self) -> str:
        return f"{self.API_BASE}/bot{self.BOT_TOKEN}/getUpdates"


telegram_config = TelegramConfig()

# One log line at import time so the state is visible in startup logs.
if telegram_config.enabled:
    logger.info("Telegram enabled | polling=%s", telegram_config.POLLING_ENABLED)
else:
    logger.info("Telegram disabled | reason=%s", telegram_config.reason)