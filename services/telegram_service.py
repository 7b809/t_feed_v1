# services/telegram_service.py
"""Shared Telegram notification transport.

All outbound notifications are normalized by
``services.telegram_format.format_notification`` and sent with Telegram
HTML parse mode. Existing callers may continue using either
``send_message(title, message, level=...)`` or a single preformatted
message as ``send_message(message)``.

The formatter preserves trusted HTML produced by project builders, escapes
plain text, removes legacy decorative separators, and uses one compact
layout that wraps naturally on desktop and mobile clients. Telegram does not
support custom responsive font sizes in message text.

The optional ``parse_mode`` argument remains accepted for call-site
compatibility; standardized output is always HTML.
"""

from typing import Optional

import requests

from core import config
from core.logger import get_logger
from services.telegram_format import format_notification

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
        parse_mode: Optional[str] = None,
    ) -> bool:
        """Send a Telegram notification.

        Parameters
        ----------
        title      : headline / leading text (required)
        message    : body text. Optional — defaults to ``""`` so callers
                     that pass a single pre-formatted string as ``title``
                     still work (Strategy 2 pattern).
        level      : severity prefix (INFO / SUCCESS / WARNING / ERROR / STARTUP)
        parse_mode : retained for backwards compatibility. The shared
                     formatter always emits HTML, so the wire parse mode is
                     consistently "HTML".

        Returns
        -------
        ``True`` when the message was accepted by Telegram,
        ``False`` when Telegram is disabled / unconfigured, or the
        HTTP call failed. Never raises.
        """
        if not (self.enabled and self.bot_token and self.chat_id):
            return False

        # Every outbound notification uses the same compact HTML layout.
        # Plain text is escaped by the formatter; trusted formatter HTML is
        # retained. Telegram wraps lines naturally on narrow mobile screens.
        text = format_notification(title, message, level)
        parse_mode = "HTML"

        payload = {"chat_id": self.chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json=payload,
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
            level="INFO",
        )


telegram_service = TelegramService()