"""
telegram_app/manager.py

Single orchestrator for every telegram interaction.
"""
import asyncio
import threading
from typing import Any, Callable, Coroutine, Dict, Optional

from core.logger import get_logger
from telegram_app import notify
from telegram_app.bot import telegram_bot
from telegram_app.config import telegram_config
from telegram_app.telegram_msg import delete_message, send_message

logger = get_logger(__name__)

RefreshTriggerFn = Callable[[str], None]
SaveTokenFn = Callable[[str], Dict[str, Any]]
StatusFn = Callable[[], Dict[str, Any]]


class TelegramManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._refresh_fn: Optional[RefreshTriggerFn] = None
        self._save_token_fn: Optional[SaveTokenFn] = None
        self._status_fn: Optional[StatusFn] = None

        self._awaiting_token: Dict[str, bool] = {}
        self._prompt_message_id: Dict[str, int] = {}
        self._token_healthy: bool = True
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def enabled(self) -> bool:
        return telegram_config.enabled

    # ---- event loop bridge --------------------------------------------
    def set_main_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._main_loop = loop
        logger.info("manager::set_main_loop | captured main event loop")

    def schedule(self, coro: Coroutine) -> bool:
        with self._lock:
            loop = self._main_loop
        if loop is None:
            logger.error(
                "manager::schedule failed | step=submit | reason=main loop not captured"
            )
            return False
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "manager::schedule failed | step=submit | reason=%s",
                f"{type(exc).__name__}: {str(exc)[:150]}",
            )
            return False

    # ---- lifecycle -----------------------------------------------------
    def notify_project_starting(self, version: str) -> None:
        self._safe(notify.notify_project_starting, version)

    def notify_project_started(self, steps_done, steps_skipped, token_valid: bool) -> None:
        self._safe(notify.notify_project_started, steps_done, steps_skipped, token_valid)

    def notify_project_stopping(self) -> None:
        self._safe(notify.notify_project_stopping)

    def notify_token_health(self, health: Dict[str, Any]) -> None:
        self._safe(notify.notify_token_health, health)

    def notify_token_invalid(self, health: Dict[str, Any]) -> None:
        self._safe(notify.notify_token_invalid, health)

    def notify_token_expired(
        self,
        reason: str,
        invalid_since: Optional[str] = None,
        reminder_number: int = 0,
        is_reminder: bool = False,
    ) -> None:
        self._safe(
            notify.notify_token_expired,
            reason, invalid_since, reminder_number, is_reminder,
        )

    def notify_token_recovered(self) -> None:
        self._safe(notify.notify_token_recovered)

    def notify_token_saved(self, source: str) -> None:
        self._safe(notify.notify_token_saved, source)

    def notify_hard_refresh_started(self, trigger: str) -> None:
        self._safe(notify.notify_hard_refresh_started, trigger)

    def notify_hard_refresh_done(self, summary: Dict[str, Any]) -> None:
        self._safe(notify.notify_hard_refresh_done, summary)

    def notify_hard_refresh_failed(self, summary: Dict[str, Any]) -> None:
        self._safe(notify.notify_hard_refresh_failed, summary)

    def notify_daily_refresh_done(self, summary: Dict[str, Any]) -> None:
        self._safe(notify.notify_daily_refresh_done, summary)

    def notify_error(self, where: str, what_stopped: str, short_error: str) -> None:
        self._safe(notify.notify_error, where, what_stopped, short_error)

    def send(self, text: str) -> Optional[int]:
        return send_message(text)

    # ---- token health state -------------------------------------------
    def mark_token_healthy(self, value: bool) -> None:
        with self._lock:
            self._token_healthy = bool(value)

    def is_token_healthy(self) -> bool:
        with self._lock:
            return self._token_healthy

    # ---- bot wiring ----------------------------------------------------
    def start_bot(
        self,
        save_token_fn: Optional[SaveTokenFn] = None,
        refresh_fn: Optional[RefreshTriggerFn] = None,
        status_fn: Optional[StatusFn] = None,
    ) -> None:
        with self._lock:
            if save_token_fn is not None:
                self._save_token_fn = save_token_fn
            if refresh_fn is not None:
                self._refresh_fn = refresh_fn
            if status_fn is not None:
                self._status_fn = status_fn
        telegram_bot.start(self._dispatch)

    def stop_bot(self) -> None:
        telegram_bot.stop()

    # ---- dispatcher ----------------------------------------------------
    def _dispatch(self, text: str, chat_id: str, message_id: int) -> None:
        try:
            stripped = (text or "").strip()
            if not stripped:
                return

            if self._is_awaiting_token(chat_id):
                if stripped.startswith("/"):
                    self._clear_awaiting(chat_id, delete_prompt=True)
                    self._dispatch_command(stripped, chat_id, message_id)
                    return
                self._handle_token_submission(stripped, chat_id, message_id)
                return

            self._dispatch_command(stripped, chat_id, message_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "manager::_dispatch failed | step=handle message | reason=%s",
                f"{type(exc).__name__}: {str(exc)[:150]}",
            )

    def _dispatch_command(self, text: str, chat_id: str, message_id: int) -> None:
        cmd, _, rest = text.partition(" ")
        cmd = cmd.strip().lower()
        rest = rest.strip()

        if cmd in ("/help", "/start"):
            notify.notify_help()
            return
        if cmd == "/status":
            self._cmd_status()
            return
        if cmd == "/save_token":
            self._cmd_save_token(rest, chat_id, message_id)
            return
        if cmd == "/cancel":
            self._cmd_cancel(chat_id)
            return
        if cmd == "/refresh":
            self._cmd_refresh(trigger="telegram")
            return

        send_message(f"❓ Unknown command: <code>{cmd}</code>\nSend /help.")

    # ---- pending-state helpers -----------------------------------------
    def _is_awaiting_token(self, chat_id: str) -> bool:
        with self._lock:
            return bool(self._awaiting_token.get(chat_id))

    def _mark_awaiting(self, chat_id: str, prompt_message_id: Optional[int]) -> None:
        with self._lock:
            self._awaiting_token[chat_id] = True
            if prompt_message_id is not None:
                self._prompt_message_id[chat_id] = int(prompt_message_id)

    def _clear_awaiting(self, chat_id: str, delete_prompt: bool) -> None:
        with self._lock:
            self._awaiting_token.pop(chat_id, None)
            prompt_id = self._prompt_message_id.pop(chat_id, None)
        if delete_prompt and prompt_id:
            delete_message(prompt_id, chat_id=chat_id)

    # ---- commands ------------------------------------------------------
    def _cmd_status(self) -> None:
        if self._status_fn is None:
            send_message("⚠️ Status not available yet.")
            return
        try:
            data = dict(self._status_fn() or {})
        except Exception as exc:  # noqa: BLE001
            send_message(f"⚠️ Status failed: {type(exc).__name__}")
            return

        with self._lock:
            data.setdefault("token_healthy", self._token_healthy)

        lines = ["📊 <b>Status</b>", "─" * 28]
        for k, v in data.items():
            lines.append(f"  {k:<18}: {v}")
        send_message("\n".join(lines))

    def _cmd_save_token(self, rest: str, chat_id: str, message_id: int) -> None:
        if rest:
            delete_message(message_id, chat_id=chat_id)
            self._handle_token_submission(rest, chat_id, message_id)
            return

        self._clear_awaiting(chat_id, delete_prompt=True)
        prompt_id = notify.notify_save_token_prompt()
        self._mark_awaiting(chat_id, prompt_id)

    def _cmd_cancel(self, chat_id: str) -> None:
        if not self._is_awaiting_token(chat_id):
            send_message("Nothing to cancel.")
            return
        self._clear_awaiting(chat_id, delete_prompt=True)
        notify.notify_save_token_cancelled()

    def _handle_token_submission(
        self, token: str, chat_id: str, user_message_id: int
    ) -> None:
        try:
            delete_message(user_message_id, chat_id=chat_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "manager::_handle_token_submission | delete user msg failed | %s",
                f"{type(exc).__name__}: {str(exc)[:120]}",
            )

        self._clear_awaiting(chat_id, delete_prompt=True)
        notify.notify_save_token_accepted()

        if self._save_token_fn is None:
            notify.notify_save_token_result(False, "Save handler not registered")
            return
        try:
            summary = self._save_token_fn(token) or {}
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "manager::_handle_token_submission failed | step=save | reason=%s",
                f"{type(exc).__name__}: {str(exc)[:150]}",
            )
            notify.notify_save_token_result(False, f"internal error: {type(exc).__name__}")
            return

        if summary.get("saved"):
            notify.notify_save_token_result(True, "validated & stored", source="telegram")
            self.mark_token_healthy(True)
            self._cmd_refresh(trigger="telegram_after_save")
        else:
            reason = summary.get("error", "unknown")
            notify.notify_save_token_result(False, str(reason), source="telegram")

    def _cmd_refresh(self, trigger: str) -> None:
        if self._refresh_fn is None:
            send_message("⚠️ Refresh handler not registered yet.")
            return
        try:
            self._refresh_fn(trigger)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "manager::_cmd_refresh failed | step=trigger | reason=%s",
                f"{type(exc).__name__}: {str(exc)[:150]}",
            )
            send_message(f"❌ Refresh failed: {type(exc).__name__}")

    # ---- safe wrappers -------------------------------------------------
    @staticmethod
    def _safe(fn, *args, **kwargs) -> None:
        try:
            fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "manager::_safe failed | step=%s | reason=%s",
                getattr(fn, "__name__", "unknown"),
                f"{type(exc).__name__}: {str(exc)[:150]}",
            )


telegram_manager = TelegramManager()