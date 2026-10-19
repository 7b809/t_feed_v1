"""Telegram commands for the runtime order-enable toggle.

Commands
--------
    /orders_status   -> show current state (enabled / disabled)
    /orders_enable   -> turn orders ON
    /orders_disable  -> turn orders OFF

Integration
-----------
In ``token_tasks/telegram_token_bot.py``, add to ``handle_message``:

    from token_tasks.order_toggle_commands import (
        handle_order_toggle_command,
    )

    if command.startswith("/orders"):
        handle_order_toggle_command(
            bot=self,
            chat_id=chat_id,
            command=command,
        )
        return

The helper reuses the bot's existing ``_send`` / ``_request`` methods,
so it inherits the same authorization (``_authorized``) and the same
``TELEGRAM_CHAT_ID`` restriction.
"""

from __future__ import annotations

import logging
from typing import Any

from services.order_runtime_service import order_runtime_service

logger = logging.getLogger(__name__)


def _status_text() -> str:
    st = order_runtime_service.status()
    enabled = st.get("enabled")
    lines = [
        "📊 ORDER RUNTIME STATUS",
        "────────────────────────",
        f"Enabled:      {'✓ YES' if enabled else '✗ NO'}",
        f"Default:      enabled on startup",
    ]
    if st.get("changed_at"):
        lines.append(f"Changed at:   {st['changed_at']}")
    if st.get("changed_by"):
        lines.append(f"Changed by:   {st['changed_by']}")
    lines.append("────────────────────────")
    lines.append(
        "Use /orders_enable or /orders_disable to toggle."
    )
    return "\n".join(lines)


def handle_order_toggle_command(
    *,
    bot: Any,
    chat_id: Any,
    command: str,
) -> None:
    """Dispatch /orders* commands. Never raises."""
    try:
        cmd = str(command or "").strip().lower()

        if cmd == "/orders_status":
            bot._send(_status_text(), chat_id=chat_id)
            return

        if cmd == "/orders_enable":
            st = order_runtime_service.enable(source="telegram")
            bot._send(
                "✅ Orders ENABLED.\n"
                f"Changed by: telegram\n"
                f"Enabled: {st.get('enabled')}",
                chat_id=chat_id,
            )
            return

        if cmd == "/orders_disable":
            st = order_runtime_service.disable(source="telegram")
            bot._send(
                "🚫 Orders DISABLED.\n"
                f"Changed by: telegram\n"
                f"Enabled: {st.get('enabled')}",
                chat_id=chat_id,
            )
            return

        # Unknown /orders* command
        bot._send(
            "Unknown order command.\n"
            "Available: /orders_status, /orders_enable, /orders_disable",
            chat_id=chat_id,
        )
    except Exception:
        logger.exception("order toggle command failed")