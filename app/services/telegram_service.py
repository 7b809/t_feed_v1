# app/services/telegram_service.py
"""
Telegram notifier for the mega_archive_service.

Sends rich HTML messages to a Telegram chat for:
  * project startup / shutdown
  * startup / scheduled / manual refresh lifecycle
  * EMA feed bootstrap and live-loop lifecycle
  * unhandled exceptions

All sends are best-effort — a Telegram outage never aborts the caller.

Config (see .env.example):
    TELEGRAM_ENABLED=true
    TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
    TELEGRAM_CHAT_ID=123456789

Every ``notify_*`` method is synchronous so it can be called from worker
threads (APScheduler, asyncio.to_thread, FastAPI's lifespan). For async
call sites use ``asyncio.to_thread(notifier.notify_*, ...)``.
"""

from __future__ import annotations

import asyncio
import html
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


def _ist_now() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _esc(value: Any) -> str:
    return html.escape(str(value)) if value is not None else "—"


# ---------------------------------------------------------------------------
# Low-level Telegram client
# ---------------------------------------------------------------------------
class TelegramService:
    """Minimal Telegram Bot API client (sync + async wrapper)."""

    def __init__(self, config: Settings) -> None:
        self.config = config
        self.enabled = bool(
            config.telegram_enabled
            and config.telegram_bot_token
            and config.telegram_chat_id
        )
        self._client: httpx.Client | None = None
        self._lock = threading.Lock()

    def _client_or_create(self) -> httpx.Client:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = httpx.Client(timeout=10.0)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def send(self, text: str, *, silent: bool = False) -> bool:
        if not self.enabled:
            logger.debug("Telegram disabled — message skipped")
            return False
        url = (
            f"https://api.telegram.org/bot{self.config.telegram_bot_token}"
            f"/sendMessage"
        )
        payload = {
            "chat_id": self.config.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": silent,
        }
        try:
            response = self._client_or_create().post(url, json=payload)
            if response.status_code >= 400:
                logger.warning(
                    "Telegram send failed: %s %s",
                    response.status_code,
                    response.text[:200],
                )
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram send error: %s", exc)
            return False

    async def send_async(self, text: str, *, silent: bool = False) -> bool:
        return await asyncio.to_thread(self.send, text, silent=silent)


# ---------------------------------------------------------------------------
# High-level notifier
# ---------------------------------------------------------------------------
class ProjectNotifier:
    """High-level notification facade used by the app services."""

    def __init__(self, config: Settings) -> None:
        self.config = config
        self.telegram = TelegramService(config)
        self.app_name = config.app_name
        self.app_env = config.app_env

    @property
    def enabled(self) -> bool:
        return self.telegram.enabled

    def close(self) -> None:
        self.telegram.close()

    # ------------------------------------------------------------------
    # Project lifecycle
    # ------------------------------------------------------------------
    def notify_startup(
        self,
        *,
        host: str,
        port: int,
        scheduler_enabled: bool,
        refresh_on_startup: bool,
        ema_instruments: list[str],
        pid: int | None = None,
    ) -> None:
        scheduler = "✅ enabled" if scheduler_enabled else "⛔ disabled"
        refresh = "✅ enabled" if refresh_on_startup else "⛔ disabled"
        instruments = ", ".join(ema_instruments) or "—"
        text = (
            f"🚀 <b>Project Startup</b>\n"
            f"<b>{_esc(self.app_name)}</b>\n"
            f"\n"
            f"🌍 <b>Environment:</b> <code>{_esc(self.app_env)}</code>\n"
            f"🖥 <b>Host:</b> <code>{_esc(host)}:{_esc(port)}</code>\n"
            f"🧩 <b>PID:</b> <code>{_esc(pid)}</code>\n"
            f"⏰ <b>Scheduler:</b> {scheduler}\n"
            f"🔄 <b>Startup refresh:</b> {refresh}\n"
            f"📊 <b>EMA instruments:</b>\n"
            f"<code>{_esc(instruments)}</code>\n"
            f"🕒 <b>Started:</b> <code>{_ist_now()}</code>"
        )
        self.telegram.send(text)

    def notify_shutdown(self, *, reason: str = "lifespan exit") -> None:
        text = (
            f"🛑 <b>Project Shutdown</b>\n"
            f"<b>{_esc(self.app_name)}</b>\n"
            f"\n"
            f"📝 <b>Reason:</b> <code>{_esc(reason)}</code>\n"
            f"🕒 <b>Time:</b> <code>{_ist_now()}</code>"
        )
        self.telegram.send(text)

    def notify_exception(
        self,
        *,
        where: str,
        exc: BaseException,
        context: str | None = None,
    ) -> None:
        details = html.escape(str(exc))[:800] or "—"
        parts = [
            "🚨 <b>Exception Report</b>",
            f"<b>{_esc(self.app_name)}</b>",
            "",
            f"📍 <b>Where:</b> <code>{_esc(where)}</code>",
            f"🧨 <b>Type:</b> <code>{_esc(type(exc).__name__)}</code>",
        ]
        if context:
            parts.append(f"🏷 <b>Context:</b> <code>{_esc(context)}</code>")
        parts.append("💬 <b>Message:</b>")
        parts.append(f"<pre>{details}</pre>")
        parts.append(f"🕒 <b>Time:</b> <code>{_ist_now()}</code>")
        self.telegram.send("\n".join(parts))

    # ------------------------------------------------------------------
    # Job 1 — archive refresh
    # ------------------------------------------------------------------
    def notify_refresh_started(self, *, trigger: str, job_id: str) -> None:
        text = (
            f"🔄 <b>Archive Refresh — STARTED</b>\n"
            f"<b>{_esc(self.app_name)}</b>\n"
            f"\n"
            f"🎯 <b>Trigger:</b> <code>{_esc(trigger)}</code>\n"
            f"🆔 <b>Job ID:</b> <code>{_esc(job_id)}</code>\n"
            f"🕒 <b>Started:</b> <code>{_ist_now()}</code>"
        )
        self.telegram.send(text, silent=(trigger != "manual"))

    def notify_refresh_success(
        self,
        *,
        trigger: str,
        job_id: str,
        archive_filename: str,
        archive_size_bytes: int,
        extracted_file_count: int,
        sha256: str,
        duration_seconds: float,
        destination: str,
    ) -> None:
        size_mb = archive_size_bytes / (1024 * 1024)
        text = (
            f"✅ <b>Archive Refresh — SUCCESS</b>\n"
            f"<b>{_esc(self.app_name)}</b>\n"
            f"\n"
            f"🎯 <b>Trigger:</b> <code>{_esc(trigger)}</code>\n"
            f"🆔 <b>Job ID:</b> <code>{_esc(job_id)}</code>\n"
            f"📦 <b>Archive:</b> <code>{_esc(archive_filename)}</code>\n"
            f"📏 <b>Size:</b> <code>{size_mb:.2f} MB</code>\n"
            f"📂 <b>Extracted files:</b> <code>{extracted_file_count:,}</code>\n"
            f"🔐 <b>SHA256:</b> <code>{_esc(sha256[:16])}…</code>\n"
            f"⏱ <b>Duration:</b> <code>{duration_seconds:.2f}s</code>\n"
            f"📁 <b>Destination:</b> <code>{_esc(destination)}</code>\n"
            f"🕒 <b>Completed:</b> <code>{_ist_now()}</code>"
        )
        self.telegram.send(text)

    def notify_refresh_failed(
        self,
        *,
        trigger: str,
        job_id: str,
        duration_seconds: float,
        error: str,
    ) -> None:
        text = (
            f"❌ <b>Archive Refresh — FAILED</b>\n"
            f"<b>{_esc(self.app_name)}</b>\n"
            f"\n"
            f"🎯 <b>Trigger:</b> <code>{_esc(trigger)}</code>\n"
            f"🆔 <b>Job ID:</b> <code>{_esc(job_id)}</code>\n"
            f"⏱ <b>Duration:</b> <code>{duration_seconds:.2f}s</code>\n"
            f"💬 <b>Error:</b>\n"
            f"<pre>{html.escape(error)[:800]}</pre>\n"
            f"🕒 <b>Time:</b> <code>{_ist_now()}</code>"
        )
        self.telegram.send(text)

    # ------------------------------------------------------------------
    # Job 2 — EMA feed
    # ------------------------------------------------------------------
    def notify_ema_bootstrap(
        self,
        *,
        subscribed_keys: list[str],
        post_market_open: bool,
    ) -> None:
        shown = subscribed_keys[:15]
        extra = max(0, len(subscribed_keys) - len(shown))
        keys_text = (
            "\n".join(f"• <code>{_esc(k)}</code>" for k in shown) or "• —"
        )
        if extra > 0:
            keys_text += f"\n<i>… and {extra} more</i>"
        mode = (
            "🌞 post-09:15 warm-up"
            if post_market_open
            else "🌙 pre-market (cached state)"
        )
        text = (
            f"📡 <b>EMA Feed Bootstrap — COMPLETE</b>\n"
            f"<b>{_esc(self.app_name)}</b>\n"
            f"\n"
            f"📊 <b>Subscribed:</b> <code>{len(subscribed_keys)}</code> instrument(s)\n"
            f"{keys_text}\n"
            f"🧠 <b>Mode:</b> {mode}\n"
            f"🕒 <b>Time:</b> <code>{_ist_now()}</code>"
        )
        self.telegram.send(text)

    def notify_ema_live_started(self, *, poll_seconds: int) -> None:
        text = (
            f"🟢 <b>EMA Live Loop — STARTED</b>\n"
            f"<b>{_esc(self.app_name)}</b>\n"
            f"\n"
            f"⏱ <b>Poll interval:</b> <code>{poll_seconds}s</code>\n"
            f"🕒 <b>Time:</b> <code>{_ist_now()}</code>"
        )
        self.telegram.send(text)