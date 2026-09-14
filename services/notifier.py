from telegram import Bot
from core.config import Settings
from core.logger import get_logger

logger = get_logger("telegram_notifier")

class TelegramNotifier:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.bot = Bot(settings.telegram_bot_token) if settings.telegram_bot_token else None

    async def send(self, message: str, chat_id: int | None = None) -> None:
        target = chat_id or self.settings.allowed_chat_id
        if not self.bot or target is None:
            logger.info("Telegram disabled; notification: %s", message)
            return
        try:
            await self.bot.send_message(chat_id=target, text=message[:4096])
        except Exception:
            logger.exception("Could not send Telegram notification")
