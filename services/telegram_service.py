import requests

from core.config import settings
from core.logger import get_logger

logger = get_logger("telegram")


class TelegramService:
    def __init__(self) -> None:
        self.bot_token = settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id

    def send_message(self, message: str) -> bool:
        """
        Send a Telegram message only when TELE_FLG is enabled.
        """

        # Always read the current runtime flag.
        if not settings.tele_flag:
            logger.info("Telegram sending disabled. TELE_FLG=false")
            return False

        if not self.bot_token or not self.chat_id:
            logger.warning(
                "Telegram is not configured. "
                "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID."
            )
            return False

        url = f"https://api.telegram.org/bot" f"{self.bot_token}/sendMessage"

        payload = {
            "chat_id": self.chat_id,
            "text": message,
        }

        try:
            response = requests.post(
                url,
                json=payload,
                timeout=10,
            )

            response.raise_for_status()

            data = response.json()

            if not data.get("ok"):
                logger.error(
                    "Telegram API returned failure: %s",
                    data,
                )
                return False

            logger.info("Telegram message sent successfully.")

            return True

        except requests.RequestException:
            logger.exception("Failed to send Telegram message.")
            return False

        except Exception:
            logger.exception("Unexpected Telegram error.")
            return False


telegram_service = TelegramService()
