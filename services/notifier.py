import asyncio
import json
import urllib.parse
import urllib.request

from core.config import Settings
from core.logger import get_logger


logger = get_logger("telegram_notifier")


class TelegramNotifier:
    def __init__(self, settings: Settings):
        self.settings = settings

        self.token = settings.telegram_bot_token

        self.base_url = (
            f"https://api.telegram.org/bot{self.token}"
            if self.token
            else None
        )

    # ============================================================
    # Telegram API Request
    # ============================================================

    async def _api_request(
        self,
        method: str,
        data: dict,
    ):
        if not self.base_url:
            return None

        url = f"{self.base_url}/{method}"

        encoded_data = urllib.parse.urlencode(
            data
        ).encode("utf-8")

        def request():
            req = urllib.request.Request(
                url,
                data=encoded_data,
                method="POST",
            )

            with urllib.request.urlopen(
                req,
                timeout=30,
            ) as response:
                return json.loads(
                    response.read().decode("utf-8")
                )

        return await asyncio.to_thread(request)

    # ============================================================
    # Send Telegram Message
    # ============================================================

    async def send(
        self,
        message: str,
        chat_id: int | None = None,
    ) -> None:

        target = (
            chat_id
            if chat_id is not None
            else self.settings.allowed_chat_id
        )

        # --------------------------------------------------------
        # Telegram disabled
        # --------------------------------------------------------

        if not self.token:
            logger.info(
                "Telegram disabled; notification: %s",
                message,
            )
            return

        # --------------------------------------------------------
        # No chat ID
        # --------------------------------------------------------

        if target is None:
            logger.warning(
                "Telegram notification skipped; "
                "no chat ID configured"
            )
            return

        # --------------------------------------------------------
        # Telegram message limit
        # --------------------------------------------------------

        message = message[:4096]

        try:

            response = await self._api_request(
                "sendMessage",
                {
                    "chat_id": target,
                    "text": message,
                },
            )

            # ----------------------------------------------------
            # Telegram API returned an error
            # ----------------------------------------------------

            if not response or not response.get("ok"):

                logger.error(
                    "Telegram API rejected message | "
                    "chat_id=%s | response=%s",
                    target,
                    response,
                )

                return

            logger.info(
                "Telegram notification sent | chat_id=%s",
            )

        except Exception:
            logger.exception(
                "Could not send Telegram notification | "
                "chat_id=%s",
                target,
            ) 