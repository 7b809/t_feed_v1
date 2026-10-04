"""
telegram_app/notify.py

High-level message templates. One function per event, each returns the
message_id (or None) after sending.
"""
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from core.logger import get_logger
from telegram_app.config import telegram_config
from telegram_app.telegram_msg import send_message

logger = get_logger(__name__)

LINE = "─" * 28


def _now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _bullets(items: Iterable[str]) -> str:
    return "\n".join(f"  • {x}" for x in items)


def _send(text: str) -> Optional[int]:
    return send_message(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Project lifecycle
# ---------------------------------------------------------------------------


def notify_project_starting(version: str = "1.0.0") -> Optional[int]:
    if not telegram_config.NOTIFY_STARTUP:
        return None
    return _send(
        f"🚀 <b>UpstoxAppV2</b> starting\n"
        f"{LINE}\n"
        f"  version : {version}\n"
        f"  at      : {_now()} IST"
    )


def notify_project_started(
    steps_done: List[str],
    steps_skipped: List[str],
    token_valid: bool,
) -> Optional[int]:
    if not telegram_config.NOTIFY_STARTUP:
        return None
    icon = "🟢" if token_valid else "🟡"
    msg = [f"{icon} <b>Project started</b>", LINE, f"  at : {_now()} IST", ""]
    if steps_done:
        msg.append("✅ <b>Completed</b>")
        msg.append(_bullets(steps_done))
        msg.append("")
    if steps_skipped:
        msg.append("⏭ <b>Skipped</b>")
        msg.append(_bullets(steps_skipped))
        msg.append("")
    if not token_valid:
        msg.append("⚠️ Token invalid — awaiting user action.")
        msg.append("Send /save_token to submit one.")
    return _send("\n".join(msg))


def notify_project_stopping() -> Optional[int]:
    if not telegram_config.NOTIFY_SHUTDOWN:
        return None
    return _send(
        f"🔴 <b>Project stopping</b>\n"
        f"{LINE}\n"
        f"  at : {_now()} IST"
    )


# ---------------------------------------------------------------------------
# Token events
# ---------------------------------------------------------------------------


def notify_token_health(health: Dict[str, Any]) -> Optional[int]:
    if not telegram_config.NOTIFY_TOKEN_EVENTS:
        return None
    valid = bool(health.get("valid"))
    icon = "✅" if valid else "❌"
    lines = [
        f"{icon} <b>Token status</b>",
        LINE,
        f"  source     : {health.get('token_source', 'none')}",
        f"  cached     : {health.get('cached', False)}",
        f"  validation : {health.get('validation_status', 'not_run')}",
    ]
    if health.get("error"):
        lines.append(f"  error      : {health['error']}")
    lines.append(f"  at         : {_now()} IST")
    return _send("\n".join(lines))


def notify_token_saved(source: str) -> Optional[int]:
    if not telegram_config.NOTIFY_TOKEN_EVENTS:
        return None
    return _send(
        f"💾 <b>Token saved</b>\n"
        f"{LINE}\n"
        f"  source : {source}\n"
        f"  at     : {_now()} IST"
    )


def notify_token_invalid(health: Dict[str, Any]) -> Optional[int]:
    if not telegram_config.NOTIFY_TOKEN_EVENTS:
        return None
    lines = [
        "🚫 <b>Token invalid — startup blocked</b>",
        LINE,
        f"  source     : {health.get('token_source', 'none')}",
        f"  validation : {health.get('validation_status', 'not_run')}",
    ]
    if health.get("error"):
        lines.append(f"  error      : {health['error']}")
    lines.append("")
    lines.append("👉 Send <code>/save_token</code> to submit a new token.")
    return _send("\n".join(lines))


def notify_token_expired(
    reason: str,
    invalid_since: Optional[str] = None,
    reminder_number: int = 0,
    is_reminder: bool = False,
) -> Optional[int]:
    """Periodic watchdog alert: token expired or invalid."""
    if not telegram_config.NOTIFY_TOKEN_EVENTS:
        return None

    if is_reminder:
        icon = "⏰"
        title = f"Token still invalid (reminder #{reminder_number})"
    else:
        icon = "🚨"
        title = "Token expired or invalid"

    lines = [f"{icon} <b>{title}</b>", LINE]
    lines.append(f"  reason : {reason}")
    if invalid_since:
        lines.append(f"  since  : {invalid_since} IST")
    lines.append(f"  at     : {_now()} IST")
    lines.append("")
    lines.append("👉 Send <code>/save_token</code> to submit a new token.")
    lines.append("   Or POST /token/save with the new token.")
    return _send("\n".join(lines))


def notify_token_recovered() -> Optional[int]:
    if not telegram_config.NOTIFY_TOKEN_EVENTS:
        return None
    return _send(
        f"✅ <b>Token recovered</b>\n"
        f"{LINE}\n"
        f"  status : valid\n"
        f"  at     : {_now()} IST"
    )


def notify_save_token_prompt() -> Optional[int]:
    return _send(
        "🔐 <b>Send your Upstox access token</b>\n"
        f"{LINE}\n"
        "Paste the token in your next message.\n"
        "It will be saved and then <b>deleted from this chat</b>.\n\n"
        "Send /cancel to abort."
    )


def notify_save_token_accepted() -> Optional[int]:
    return _send("🔐 Token received. Verifying and saving…")


def notify_save_token_cancelled() -> Optional[int]:
    return _send("✋ Token submission cancelled.")


def notify_save_token_result(ok: bool, message: str, source: str = "telegram") -> Optional[int]:
    icon = "✅" if ok else "❌"
    title = "Token saved" if ok else "Token save failed"
    return _send(
        f"{icon} <b>{title}</b>\n"
        f"{LINE}\n"
        f"  source : {source}\n"
        f"  detail : {message}\n"
        f"  at     : {_now()} IST"
    )


# ---------------------------------------------------------------------------
# Refresh events
# ---------------------------------------------------------------------------


def _refresh_body(title: str, icon: str, summary: Dict[str, Any]) -> str:
    lines = [f"{icon} <b>{title}</b>", LINE]
    for key, value in summary.items():
        lines.append(f"  {key:<16}: {value}")
    lines.append(f"  at              : {_now()} IST")
    return "\n".join(lines)


def notify_hard_refresh_started(trigger: str) -> Optional[int]:
    if not telegram_config.NOTIFY_HARD_REFRESH:
        return None
    return _send(
        f"🔄 <b>Hard refresh started</b>\n"
        f"{LINE}\n"
        f"  trigger : {trigger}\n"
        f"  at      : {_now()} IST"
    )


def notify_hard_refresh_done(summary: Dict[str, Any]) -> Optional[int]:
    if not telegram_config.NOTIFY_HARD_REFRESH:
        return None
    return _send(_refresh_body("Hard refresh done", "✅", summary))


def notify_hard_refresh_failed(summary: Dict[str, Any]) -> Optional[int]:
    if not telegram_config.NOTIFY_HARD_REFRESH:
        return None
    return _send(_refresh_body("Hard refresh failed", "❌", summary))


def notify_daily_refresh_done(summary: Dict[str, Any]) -> Optional[int]:
    if not telegram_config.NOTIFY_DAILY_REFRESH:
        return None
    return _send(_refresh_body("Daily refresh done", "🗓", summary))


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def notify_error(where: str, what_stopped: str, short_error: str) -> Optional[int]:
    return _send(
        f"⚠️ <b>Error</b>\n"
        f"{LINE}\n"
        f"  where : {where}\n"
        f"  effect: {what_stopped}\n"
        f"  reason: {short_error}\n"
        f"  at    : {_now()} IST"
    )


def notify_help() -> Optional[int]:
    return _send(
        "🤖 <b>UpstoxAppV2 bot</b>\n"
        f"{LINE}\n"
        "  /status       – current token + scheduler state\n"
        "  /save_token   – submit a new token (prompts, then deletes it)\n"
        "  /refresh      – run a hard refresh now\n"
        "  /cancel       – cancel a pending token submission\n"
        "  /help         – this message"
    )