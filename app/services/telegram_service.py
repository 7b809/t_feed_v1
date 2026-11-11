# app/services/telegram_service.py
from __future__ import annotations

import logging

import httpx

from ..config import Settings

logger = logging.getLogger(__name__)


class TelegramService:
    """Best-effort Telegram notifier. All methods are safe to await."""

    def __init__(self, config: Settings) -> None:
        self._config = config

    @property
    def _enabled(self) -> bool:
        return (
            self._config.telegram_enabled
            and bool(self._config.telegram_bot_token)
            and bool(self._config.telegram_chat_id)
        )

    async def _send(self, text: str) -> bool:
        if not self._enabled:
            return False
        url = (
            f"https://api.telegram.org/bot{self._config.telegram_bot_token}/sendMessage"
        )
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    url,
                    json={
                        "chat_id": self._config.telegram_chat_id,
                        "text": text,
                        "disable_web_page_preview": True,
                    },
                )
                if resp.status_code >= 400:
                    logger.warning(
                        "telegram send failed status=%s body=%s",
                        resp.status_code,
                        resp.text[:200],
                    )
                    return False
                return True
        except Exception:
            logger.exception("telegram send error")
            return False

    # ---------------------------------------------------------------
    # Generic sender — used by project-update / deploy scripts.
    # Keeps the legacy (title, message, level) contract.
    # ---------------------------------------------------------------
    async def send_message(
        self,
        title: str,
        message: str,
        level: str = "INFO",
    ) -> bool:
        emoji_map = {
            "INFO": "ℹ️",
            "SUCCESS": "✅",
            "WARNING": "⚠️",
            "ERROR": "❌",
            "STARTUP": "🚀",
        }
        emoji = emoji_map.get(level.upper(), "ℹ️")
        text = f"{emoji} {title}\n\n{message}"
        return await self._send(text)

    async def notify_startup(self, app_name: str) -> None:
        await self._send(f"✅ {app_name} started")

    async def notify_shutdown(self, app_name: str) -> None:
        await self._send(f"🛑 {app_name} stopped")

    async def notify_isolation(self, isolation_id: str, instrument_key: str) -> None:
        await self._send(
            f"📥 Isolation received: {isolation_id} | leg={instrument_key}"
        )

    async def notify_ws_state(
        self, instrument_key: str, leg_role: str, state: str, detail: str = ""
    ) -> None:
        tail = f" — {detail}" if detail else ""
        await self._send(f"🔌 WS {leg_role} {instrument_key} → {state}{tail}")