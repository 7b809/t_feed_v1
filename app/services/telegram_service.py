"""Telegram notification service.

Sends formatted messages to a Telegram chat using the Telegram Bot HTTP
API directly. No third-party Telegram packages are used — only
``requests``, which is already a project dependency.

Design notes
------------
* Best-effort: every public method catches its own errors and logs a
  warning. A Telegram failure never breaks application startup, a
  refresh job, or shutdown.
* Non-blocking: HTTP calls run in a worker thread via
  ``asyncio.to_thread`` so the FastAPI event loop is never stalled.
* Fire-and-forget helpers (``notify_*``) schedule the send and return
  immediately. Awaitable helpers (``send_*``) return the delivery result.
* HTML parse mode: user-supplied values are escaped; bold labels and
  emoji are added by the formatter so the resulting message renders
  cleanly in every Telegram client.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from html import escape
from typing import Any, Iterable

import requests

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"

# Level -> leading emoji. Unknown levels fall back to a neutral bullet.
_LEVEL_EMOJI: dict[str, str] = {
    "STARTUP": "🟢",
    "SHUTDOWN": "🔴",
    "SUCCESS": "✅",
    "INFO": "ℹ️",
    "WARNING": "⚠️",
    "ERROR": "❌",
    "CRASH": "🔥",
}


class TelegramService:
    """Thin wrapper around the Bot API ``sendMessage`` endpoint."""

    def __init__(self) -> None:
        token = (settings.telegram_bot_token or "").strip()
        # Accept the value with or without surrounding quotes.
        chat_id = (
            (settings.telegram_chat_id or "")
            .strip()
            .strip('"')
            .strip("'")
        )

        self._token = token
        self._chat_id = chat_id
        self._timeout = max(1, int(settings.telegram_timeout_seconds))
        self._enabled = bool(
            settings.telegram_enabled and token and chat_id
        )
        self._api_url = (
            f"{_TELEGRAM_API_BASE}/bot{token}/sendMessage"
            if token
            else ""
        )

        if self._enabled:
            logger.info(
                "Telegram notifications enabled; chat_id=%s",
                _mask(self._chat_id),
            )
        elif settings.telegram_enabled:
            logger.warning(
                "TELEGRAM_ENABLED=true but token and/or chat id missing; "
                "notifications will be skipped."
            )

    # ------------------------------------------------------------------ #
    # Introspection                                                      #
    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------ #
    # Public async API                                                   #
    # ------------------------------------------------------------------ #
    async def send_message(
        self,
        text: str,
        *,
        silent: bool = False,
        disable_preview: bool = True,
        timeout: int | None = None,
    ) -> bool:
        """Send a raw, already-HTML-formatted message."""
        if not self._enabled or not text:
            return False
        try:
            return await asyncio.to_thread(
                self._send_sync,
                text,
                silent,
                disable_preview,
                timeout or self._timeout,
            )
        except Exception:
            logger.exception("Telegram send_message failed")
            return False

    async def send_event(
        self,
        title: str,
        lines: Iterable[str] | None = None,
        *,
        level: str = "INFO",
        silent: bool = False,
        timeout: int | None = None,
    ) -> bool:
        """Send a structured multi-line notification."""
        text = self._format_event(title, list(lines or []), level)
        return await self.send_message(
            text,
            silent=silent,
            timeout=timeout,
        )

    async def send_startup(
        self, details: dict[str, Any] | None = None
    ) -> bool:
        return await self.send_event(
            "Application Started",
            self._kv_lines(details or {}),
            level="STARTUP",
        )

    async def send_shutdown(
        self,
        details: dict[str, Any] | None = None,
        *,
        reason: str = "normal shutdown",
        timeout: int = 3,
    ) -> bool:
        lines = self._kv_lines(details or {})
        lines.append(f"<b>Reason:</b> {escape(reason)}")
        return await self.send_event(
            "Application Stopped",
            lines,
            level="SHUTDOWN",
            timeout=timeout,
        )

    # ------------------------------------------------------------------ #
    # Public fire-and-forget API                                         #
    # ------------------------------------------------------------------ #
    def notify_event(
        self,
        title: str,
        lines: Iterable[str] | None = None,
        *,
        level: str = "INFO",
        silent: bool = False,
    ) -> None:
        """Schedule a send and return immediately.

        Never raises. Safe to call from anywhere the event loop is running.
        """
        if not self._enabled:
            return
        try:
            asyncio.create_task(
                self.send_event(title, lines, level=level, silent=silent)
            )
        except RuntimeError:
            # No running loop (e.g. imported from a sync context).
            logger.debug("Telegram notify_event skipped: no event loop")

    def notify_startup(self, details: dict[str, Any] | None = None) -> None:
        self.notify_event(
            "Application Started",
            self._kv_lines(details or {}),
            level="STARTUP",
        )

    def notify_shutdown(
        self,
        details: dict[str, Any] | None = None,
        *,
        reason: str = "normal shutdown",
    ) -> None:
        lines = self._kv_lines(details or {})
        lines.append(f"<b>Reason:</b> {escape(reason)}")
        self.notify_event("Application Stopped", lines, level="SHUTDOWN")

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #
    def _send_sync(
        self,
        text: str,
        silent: bool,
        disable_preview: bool,
        timeout: int,
    ) -> bool:
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
            "disable_notification": silent,
        }

        try:
            response = requests.post(
                self._api_url,
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            logger.warning("Telegram request failed: %s", exc)
            return False

        if response.status_code != 200:
            logger.warning(
                "Telegram API non-200; status=%s body=%s",
                response.status_code,
                response.text[:300],
            )
            return False

        try:
            body = response.json()
        except ValueError:
            # Telegram always returns JSON, but never crash if not.
            return True

        if not body.get("ok"):
            logger.warning("Telegram API returned ok=false: %s", body)
            return False

        return True

    @staticmethod
    def _format_event(
        title: str, lines: list[str], level: str
    ) -> str:
        emoji = _LEVEL_EMOJI.get(level.upper(), "•")
        header = f"{emoji} <b>{escape(title)}</b>"
        if not lines:
            return header
        return header + "\n\n" + "\n".join(lines)

    @staticmethod
    def _kv_lines(data: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        for key, value in data.items():
            if value is None:
                continue
            label = escape(str(key).replace("_", " ").title())
            lines.append(f"<b>{label}:</b> {escape(str(value))}")
        return lines


def _mask(value: str) -> str:
    """Return a redacted form of a chat id for logs."""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


telegram_service = TelegramService()