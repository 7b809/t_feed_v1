"""Small Telegram notification client used by token lifecycle monitoring.

Signature note
--------------
``send_message`` accepts ``message`` as an optional keyword with default
``""``. This lets callers use either style:

    # Style A — title + body (used by Strategy 1, token monitor, etc.)
    telegram_service.send_message(
        "Application Started",
        "Instruments loaded: 174",
        level="SUCCESS",
    )

    # Style B — one pre-formatted string (used by Strategy 2 tasks and
    # the S2 live tracker, which pass the output of
    # ``telegram_format.build(...)`` as the single payload)
    telegram_service.send_message(
        build(emoji="🎯", title="Strategy 2 · Stage 3 Tracker · Started", ...),
        level="INFO",
    )

Both styles produce the same wire payload to the Telegram API.
"""

import requests

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


class TelegramService:
    def __init__(self):
        self.bot_token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.enabled = config.TELEGRAM_ENABLED

    def send_message(
        self,
        title: str,
        message: str = "",
        level: str = "INFO",
    ) -> bool:
        """Send a Telegram notification.

        Parameters
        ----------
        title   : headline / leading text (required)
        message : body text. Optional — defaults to ``""`` so callers
                  that pass a single pre-formatted string as ``title``
                  still work (Strategy 2 pattern).
        level   : severity prefix (INFO / SUCCESS / WARNING / ERROR / STARTUP)

        Returns
        -------
        ``True`` when the message was accepted by Telegram,
        ``False`` when Telegram is disabled / unconfigured, or the
        HTTP call failed. Never raises.
        """
        if not (self.enabled and self.bot_token and self.chat_id):
            return False

        if message:
            text = f"[{level}] {title}\n\n{message}"
        else:
            text = f"[{level}] {title}"

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
        return self.send_message(
            "Application stopped",
            details or "Shutdown complete.",
            "INFO",
        )


telegram_service = TelegramService()