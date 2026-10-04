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

Isolation flow:
  This module owns the manual-isolation flow (interactive /isolate,
  /unisolate, and /cancel) via `telegram_app.isolation_flow`. Any
  message that belongs to an in-progress isolation flow is consumed
  here and never reaches the user handler, so the handler stays
  unchanged and focused on its existing commands.
"""
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from core.logger import get_logger
from telegram_app import isolation_flow
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

    # ---- isolation flow dispatch ---------------------------------------
    def _maybe_handle_isolation(self, text: str, chat_id: int) -> bool:
        """
        Handle any message that belongs to the manual-isolation flow.

        Returns True when the message was consumed (reply already sent)
        so the caller should return. Returns False when the message is
        unrelated and should be dispatched to the user handler.

        Priority order:
          1. Active interactive flow  -> feed the message as the next step
          2. Direct /isolate command  -> start one-shot or interactive
          3. Direct /unisolate command-> start one-shot or interactive
          4. /cancel                  -> cancel isolation flow if active,
                                         otherwise fall through so the
                                         existing token-save cancel still
                                         gets a chance
        Never raises: all exceptions are logged and swallowed.
        """
        try:
            # 1) Interactive flow already in progress for this chat?
            reply = isolation_flow.feed_message(chat_id, text)
            if reply is not None:
                send_message(reply.text)
                return True

            # Parse the command token; strip "@botname" suffix if present.
            parts = text.split()
            if not parts:
                return False

            command = parts[0].split("@", 1)[0].lower()
            args = parts[1:]

            # 2) /isolate
            if command == "/isolate":
                reply = isolation_flow.start_isolate(chat_id, args)
                send_message(reply.text)
                return True

            # 3) /unisolate
            if command == "/unisolate":
                reply = isolation_flow.start_unisolate(chat_id, args)
                send_message(reply.text)
                return True

            # 4) /cancel — try isolation flow first, fall through if not
            #    active so the existing token-save /cancel still works.
            if command == "/cancel":
                reply = isolation_flow.cancel_flow(chat_id)
                if reply is not None:
                    send_message(reply.text)
                    return True
                return False

            return False

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "bot::_maybe_handle_isolation failed | step=dispatch | reason=%s",
                f"{type(exc).__name__}: {str(exc)[:150]}",
            )
            # Do not consume the message; let the handler attempt it.
            return False

    # ---- update handling -----------------------------------------------
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

            # ---- Isolation flow intercept --------------------------------
            # Runs BEFORE the user handler so that:
            #   * an in-progress interactive flow consumes the next message
            #     (e.g. "NIFTY") without the handler misinterpreting it
            #   * /isolate, /unisolate, and /cancel are routed to the
            #     isolation flow when relevant
            # If the flow is not active and the command is not one of
            # those three, this returns False and we fall through to the
            # normal handler dispatch.
            try:
                chat_id_int = int(chat_id)
            except (TypeError, ValueError):
                chat_id_int = 0

            if self._maybe_handle_isolation(text, chat_id_int):
                return
            # -------------------------------------------------------------

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