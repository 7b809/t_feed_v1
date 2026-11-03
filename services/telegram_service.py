# services/telegram_service.py
import html
import re
import requests
from typing import Optional

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

LEVEL_ICONS = {
    "INFO": "ℹ️",
    "SUCCESS": "✅",
    "WARNING": "⚠️",
    "ERROR": "🚨",
    "STARTUP": "🚀",
    "SHUTDOWN": "🛑",
}

TELEGRAM_MAX_LEN = 4000


class TelegramService:
    def __init__(self):
        self.bot_token = getattr(config, "TELEGRAM_BOT_TOKEN", None)
        self.chat_id = getattr(config, "TELEGRAM_CHAT_ID", None)
        self.enabled = getattr(config, "TELEGRAM_ENABLED", False)
        self.timeout = getattr(config, "TELEGRAM_TIMEOUT_SECONDS", 10)

    def _safe_html(self, text: str) -> str:
        """Sanitizes text for Telegram HTML parse_mode.
        
        If caller already passed pre-formatted HTML (contains tags like <b>, <code>),
        it preserves valid tags and escapes loose ampersands.
        If caller passed plain raw text, it escapes <, >, and & completely.
        """
        if not text:
            return ""

        # Check if caller already formatted with supported Telegram tags
        has_tags = bool(re.search(r"</?(?:b|i|code|pre|blockquote|a)\b", text))

        if not has_tags:
            # Raw text: completely escape everything so nothing breaks
            return html.escape(str(text))

        # Already has HTML: only escape unescaped ampersands
        return re.sub(r"&(?!([a-zA-Z0-9]+|#[0-9]{1,6});)", "&amp;", str(text))

    def send_message(
        self,
        title: str,
        message: str = "",
        level: str = "INFO",
        expandable_details: Optional[str] = None,
        disable_notification: bool = False,
    ) -> bool:
        """Universal notification dispatcher.
        
        Guarantees mobile/desktop formatting regardless of how callers pass data:
        - Style A (title + message): Combines into structured, copy-friendly view.
        - Style B (pre-formatted single string in title): Preserves tags safely.
        - Style C (raw unescaped text / exceptions): Automatically sanitized.
        """
        if not (self.enabled and self.bot_token and self.chat_id):
            return False

        lvl = level.upper() if level else "INFO"
        icon = LEVEL_ICONS.get(lvl, "📌")

        safe_title = self._safe_html(title)
        safe_message = self._safe_html(message)

        # Style B: Single pre-formatted string passed as `title` (message is empty)
        if not safe_message:
            # If title doesn't already have an icon/badge, prefix it
            if not any(safe_title.strip().startswith(ic) for ic in LEVEL_ICONS.values()):
                text = f"{icon} <b>[{lvl}]</b> {safe_title}"
            else:
                text = safe_title
        # Style A: Separate title and body passed
        else:
            text = f"{icon} <b>[{lvl}] {safe_title}</b>\n\n{safe_message}"

        # Attach collapsible details block if provided (e.g. stacktrace / raw json)
        if expandable_details:
            escaped_details = html.escape(str(expandable_details))
            text += f"\n\n<blockquote expandable><code>{escaped_details}</code></blockquote>"

        # Prevent 400 Bad Request: message too long
        if len(text) > TELEGRAM_MAX_LEN:
            text = text[:TELEGRAM_MAX_LEN] + "\n\n<i>... [truncated]</i>"

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": disable_notification,
        }

        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return True
        except Exception as exc:
            # Fallback: if HTML parsing somehow fails, retry as plain text without tags
            try:
                raw_fallback = re.sub(r"<[^>]+>", "", text)
                payload["text"] = raw_fallback
                payload.pop("parse_mode", None)
                requests.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    json=payload,
                    timeout=self.timeout,
                )
                return True
            except Exception:
                logger.warning("Telegram notification failed (%s).", type(exc).__name__)
                return False

    def send_shutdown_message(self, details: str = "") -> bool:
        return self.send_message(
            "Application stopped",
            details or "Shutdown complete.",
            level="SHUTDOWN",
        )


telegram_service = TelegramService()