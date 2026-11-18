"""Optional Telegram notifications for sandbox_v1. Delivery failures are non-fatal."""
from __future__ import annotations
import threading
import requests
from core.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ENABLED
from core.logger import get_logger
logger = get_logger(__file__)
_lock = threading.Lock()
_disabled_logged = False
EMOJI = {"INFO":"ℹ️","SUCCESS":"✅","WARNING":"⚠️","ERROR":"❌","STARTUP":"🚀","ORDER":"🛒","CANCEL":"🛑","EXIT":"↪️"}

def is_enabled() -> bool:
    return bool(TELEGRAM_ENABLED and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

def _disabled_notice(reason: str) -> None:
    global _disabled_logged
    if _disabled_logged: return
    with _lock:
        if not _disabled_logged:
            logger.info("Telegram notifications inactive (%s). Configure TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.", reason)
            _disabled_logged = True

def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_ENABLED:
        _disabled_notice("TELEGRAM_ENABLED=false"); return False
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        _disabled_notice("token or chat id missing"); return False
    if not text or not text.strip():
        logger.warning("Empty Telegram message skipped."); return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(url, json={"chat_id":TELEGRAM_CHAT_ID,"text":text[:4000],"disable_web_page_preview":True}, timeout=10)
        response.raise_for_status()
        if not response.json().get("ok", False):
            logger.warning("Telegram API returned ok=false: %s", response.text[:500]); return False
        logger.info("Telegram notification delivered."); return True
    except Exception:
        logger.exception("Telegram delivery failed; application will continue."); return False

def notify_event(title: str, details: list[str] | None = None, level: str = "INFO") -> bool:
    lines = [f"{EMOJI.get(level.upper(), "ℹ️")} {title}", "━━━━━━━━━━━━━━━━━━"]
    lines.extend(item for item in (details or []) if item)
    return send_telegram_message("\n".join(lines))

def build_daily_summary(tasks: dict[str, bool]) -> str:
    lines = ["📋 Sandbox Service Summary", "━━━━━━━━━━━━━━━━━━"]
    lines.extend(("✅ " if ok else "❌ ") + name for name, ok in tasks.items())
    return "\n".join(lines)
