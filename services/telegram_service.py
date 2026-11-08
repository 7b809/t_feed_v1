# services/telegram_service.py
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

HTML support
------------
If the outgoing text contains Telegram HTML tags (``<b>``, ``<i>``,
``<code>``, ``<blockquote>``, etc.) and the caller did **not** pass an
explicit ``parse_mode``, we automatically set ``parse_mode="HTML"`` so the
new ``telegram_format.build(...)`` HTML output renders correctly. Legacy
plain-Unicode callers are unaffected (no tags → no parse_mode).
"""

import re
from typing import Optional

import requests

from core import config
from core.logger import get_logger
from services.telegram_format import format_notification

logger = get_logger(__file__)


# Tags that Telegram's HTML parse_mode understands. If we see any of these,
# assume the caller wants HTML rendering unless they explicitly said otherwise.
_HTML_TAG_RE = re.compile(
    r"<(b|strong|i|em|u|s|code|pre|blockquote|a|tg-spoiler)\b",
    re.IGNORECASE,
)


def _looks_like_html(text: str) -> bool:
    return bool(_HTML_TAG_RE.search(text or ""))


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
        parse_mode : optional Telegram parse_mode ("HTML" / "MarkdownV2").
                     When omitted, HTML is auto-detected from ``title``/``message``
                     and enabled automatically. Plain-text callers are
                     unaffected.

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
        parse_mode = parse_mode or "HTML"

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