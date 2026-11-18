"""Optional Telegram notifications for sandbox_v1. Delivery failures are non-fatal."""
from __future__ import annotations

import threading

import requests

from core.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ENABLED
from core.logger import get_logger

logger = get_logger(__file__)
_disabled_lock = threading.Lock()
_disabled_logged = False

EMOJI = {
    "INFO": "ℹ️",
    "SUCCESS": "✅",
    "WARNING": "⚠️",
    "ERROR": "❌",
    "STARTUP": "🚀",
    "ORDER": "🛒",
    "CANCEL": "🛑",
    "EXIT": "↪️",
}


def is_enabled() -> bool:
    """Return true only when enabled and both credentials are configured."""
    return bool(TELEGRAM_ENABLED and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def _log_disabled_once(reason: str) -> None:
    global _disabled_logged
    if _disabled_logged:
        return
    with _disabled_lock:
        if not _disabled_logged:
            logger.info(
                "Telegram notifications inactive (%s). Configure TELEGRAM_ENABLED=true, "
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.",
                reason,
            )
            _disabled_logged = True


def send_telegram_message(text: str) -> bool:
    """Send one message; never raise into order processing or application startup."""
    if not TELEGRAM_ENABLED:
        _log_disabled_once("TELEGRAM_ENABLED=false")
        return False
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        _log_disabled_once("bot token or chat id missing")
        return False
    if not isinstance(text, str) or not text.strip():
        logger.warning("Empty Telegram message skipped.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4000],
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        result = response.json()
        if not result.get("ok", False):
            logger.warning("Telegram API returned ok=false: %s", str(result)[:500])
            return False
        logger.info("Telegram notification delivered.")
        return True
    except Exception as error:
        # Do not log the request URL: it contains the bot token.
        logger.warning("Telegram delivery failed (%s); application will continue.", type(error).__name__)
        return False


def notify_event(
    title: str,
    details: list[str] | None = None,
    level: str = "INFO",
) -> bool:
    """Send a consistently formatted event message."""
    emoji = EMOJI.get(level.upper(), EMOJI["INFO"])
    lines = [f"{emoji} {title}", "━━━━━━━━━━━━━━━━━━"]
    lines.extend(item for item in (details or []) if item)
    return send_telegram_message("\n".join(lines))


def build_daily_summary(tasks: dict[str, bool]) -> str:
    """Build a compact maintenance summary."""
    lines = ["📋 Sandbox Service Summary", "━━━━━━━━━━━━━━━━━━"]
    lines.extend(("✅ " if passed else "❌ ") + name for name, passed in tasks.items())
    return "\n".join(lines)
