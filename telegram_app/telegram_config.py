import os
from core.logger import get_logger

logger = get_logger(__name__)

class TelegramConfig:
    def __init__(self):
        self.BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.ENABLED = bool(self.BOT_TOKEN) and bool(self.CHAT_ID)
        self.API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org")
        self.POLL_TIMEOUT = int(os.getenv("TELEGRAM_POLL_TIMEOUT", "20"))

telegram_config = TelegramConfig()
if telegram_config.ENABLED:
    logger.info(f"Telegram enabled | chat_id={telegram_config.CHAT_ID}")
else:
    logger.info("Telegram disabled (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID)")