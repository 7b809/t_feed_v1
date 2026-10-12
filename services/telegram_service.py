"""Small Telegram notification client used by token lifecycle monitoring."""

import requests

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


class TelegramService:
    def __init__(self):
        self.bot_token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.enabled = config.TELEGRAM_ENABLED

    def send_message(self, title: str, message: str, level: str = "INFO") -> bool:
        if not (self.enabled and self.bot_token and self.chat_id):
            return False
        text = f"[{level}] {title}\n\n{message}"
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text},
                timeout=config.TELEGRAM_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("Telegram notification failed (%s).", type(exc).__name__)
            return False

    def send_shutdown_message(self, details: str = "") -> bool:
        return self.send_message("Application stopped", details or "Shutdown complete.", "INFO")


telegram_service = TelegramService()
