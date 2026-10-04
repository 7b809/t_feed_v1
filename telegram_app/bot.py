"""
telegram_app/bot.py

Long-polling telegram bot. Runs on a daemon thread, dispatches incoming
text commands to a user-supplied handler, and isolates every failure so
the main app is never affected.

On start():
  - Drains any updates that were queued while the app was offline so
    commands issued before the current run are ignored.
  - Records the process start time so the handler can additionally
    reject stale message timestamps as a belt-and-suspenders check.
"""
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from core.logger import get_logger
from telegram_app.config import telegram_config
from telegram_app.telegram_msg import (
    flush_pending_updates,
    get_updates,
    send_message,
)

logger = get_logger(__name__)

# (text, chat_id, message_id) -> None
CommandHandler = Callable[[str, str, int], None]


class TelegramBot:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._handler: Optional[CommandHandler] = None
        self._offset: Optional[int] = None
        self._started_at: Optional[float] = None  # unix seconds

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def started_at(self) -> Optional[float]:
        return self._started_at

    # ---- lifecycle -----------------------------------------------------
    def start(self, handler: CommandHandler) -> None:
        if not telegram_config.enabled or not telegram_config.POLLING_ENABLED:
            logger.info("Telegram bot polling disabled")
            return
        if self.running:
            logger.info("Telegram bot already running")
            return

        self._handler = handler
        self._stop_event.clear()
        self._started_at = time.time()

        # Drain the backlog BEFORE entering the polling loop.
        try:
            last_id = flush_pending_updates()
            if isinstance(last_id, int):
                self._offset = last_id + 1
            else:
                self._offset = None
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "bot::start failed | step=flush backlog | reason=%s",
                f"{type(exc).__name__}: {str(exc)[:150]}",
            )
            self._offset = None

        self._thread = threading.Thread(
            target=self._loop, name="telegram-bot", daemon=True
        )
        self._thread.start()
        logger.info(
            "Telegram bot polling started | started_at=%s | offset=%s",
            datetime.fromtimestamp(self._started_at, tz=timezone.utc).astimezone().isoformat(timespec="seconds"),
            self._offset,
        )

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        try:
            self._thread.join(timeout=telegram_config.POLLING_TIMEOUT_SEC + 5)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "bot::stop failed | step=join | reason=%s",
                f"{type(exc).__name__}: {str(exc)[:150]}",
            )
        self._thread = None
        self._started_at = None
        logger.info("Telegram bot polling stopped")

    # ---- internals -----------------------------------------------------
    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                body = get_updates(self._offset, telegram_config.POLLING_TIMEOUT_SEC)
                if not body or not body.get("ok"):
                    if self._stop_event.wait(telegram_config.POLLING_RETRY_SEC):
                        return
                    continue
                for update in body.get("result", []):
                    self._handle_update(update)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "bot::_loop iteration failed | step=poll cycle | reason=%s",
                    f"{type(exc).__name__}: {str(exc)[:150]}",
                )
                if self._stop_event.wait(telegram_config.POLLING_RETRY_SEC):
                    return

    def _handle_update(self, update: dict) -> None:
        try:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._offset = update_id + 1

            message = update.get("message") or update.get("edited_message") or {}
            chat_id = str(message.get("chat", {}).get("id", ""))
            message_id = int(message.get("message_id", 0) or 0)
            message_ts = message.get("date")  # unix seconds, UTC
            text = (message.get("text") or "").strip()

            # Only respond to the configured chat id.
            if chat_id != str(telegram_config.CHAT_ID):
                logger.debug(
                    "bot | ignoring message from unknown chat | chat_id=%s", chat_id
                )
                return

            # Belt-and-suspenders: drop messages older than process start.
            if self._started_at is not None and isinstance(message_ts, int):
                if message_ts + 1 < int(self._started_at):
                    logger.info(
                        "bot | dropping stale update | update_id=%s | msg_age_s=%d",
                        update_id, int(self._started_at) - message_ts,
                    )
                    return

            if not text:
                return

            if self._handler is None:
                send_message("⚠️ Bot is not ready yet.")
                return

            self._handler(text, chat_id, message_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "bot::_handle_update failed | step=dispatch | reason=%s",
                f"{type(exc).__name__}: {str(exc)[:150]}",
            )


telegram_bot = TelegramBot()