import logging

import httpx

from core import config

logger = logging.getLogger(__name__)


class TelegramService:
    def __init__(self) -> None:
        self.enabled = config.TELEGRAM_ENABLED
        self.url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"

    def send(self, message: str, silent: bool = False) -> bool:
        if not self.enabled:
            return False
        try:
            response = httpx.post(
                self.url,
                json={
                    "chat_id": config.TELEGRAM_CHAT_ID,
                    "text": str(message),
                    "disable_notification": silent,
                },
                timeout=config.TELEGRAM_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(str(payload))
            return True
        except Exception as ex:
            logger.error("Telegram message failed: %s", ex)
            return False


telegram_service = TelegramService()
