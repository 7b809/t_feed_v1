# token_tasks/telegram_token_bot.py
"""Telegram long-polling bot for Upstox token management.

Commands
--------
Token management:
    /save_token       save a new Upstox access token
    /token_status     show masked token status
    /token_check      validate the stored token
    /cancel           cancel pending token entry
    /help, /start     show available commands

Order runtime toggle:
    /orders_status    show current order enable/disable state
    /orders_enable    enable live order placement
    /orders_disable   disable live order placement

The order toggle commands are handled by
``token_tasks.order_toggle_commands.handle_order_toggle_command`` so
the actual state lives in ``services.order_runtime_service`` and is
shared with the admin API.

Runtime state flag
------------------
``self.is_running`` reflects **whether polling is actually active**
(set to True only once ``run_loop()`` has begun, cleared in ``stop()``
and when ``start()`` declines / is disabled). This is the source of
truth read by ``routes/admin_routes.py`` — the older behaviour of
treating "start() did not raise" as success produced a false
``telegram_polling=True`` when the Windows prompt was declined.

Windows start confirmation
--------------------------
When running on Windows (``os.name == "nt"``) with an interactive
console, and the bot is enabled, ``start()`` asks the operator to
confirm before beginning Telegram polling::

    Start Telegram bot polling on Windows? [y/N] (5s timeout):

Behaviour:

  * press ``y``  -> start polling.
  * press ``n``  -> do not start.
  * press any other key, press Enter, or let the 5-second timeout
    expire -> do not start (matching the ``[y/N]`` convention).

Headless Windows (no console / no TTY — e.g. a Windows Service or a
scheduled task):

  * the prompt would be invisible, so the bot is NOT started, and a
    WARNING is logged;
  * set ``TELEGRAM_BOT_SKIP_WINDOWS_PROMPT=true`` (in ``.env``, after
    adding the option to ``core/config.py``) to bypass the prompt and
    start immediately — useful for headless deployments that still
    want the bot working.

Non-Windows (Linux / macOS):

  * no prompt; the bot starts as before.

The prompt is a pure ``start()`` gate. ``run_loop()`` is unchanged, so
a bot that has already been started by any means (interactive prompt,
skip-prompt override, or non-Windows) keeps exactly the same
runtime behaviour it had before this change.
"""

import json
import os
import sys
import time
from threading import Event, Lock, Thread

import requests

from core import config
from core.logger import get_logger
from token_tasks.order_toggle_commands import handle_order_toggle_command
from token_tasks.token_monitor import check_upstox_token_validity
from token_tasks.token_store import token_store
from token_tasks.upstox_token_client import upstox_token_client

logger = get_logger(__file__)


# --------------------------------------------------------------------------- #
# Windows start-confirmation helpers                                          #
# --------------------------------------------------------------------------- #
def _console_is_interactive() -> bool:
    """Return True when both stdin and stdout are attached to a TTY.

    Runs inside a try/except because ``sys.stdin`` / ``sys.stdout`` can
    be ``None`` in some embedded interpreters, and ``isatty()`` can
    raise on unusual file objects.
    """
    try:
        return bool(
            sys.stdin is not None
            and sys.stdin.isatty()
            and sys.stdout is not None
            and sys.stdout.isatty()
        )
    except Exception:
        return False


def _prompt_yes_no_windows(question: str, timeout_seconds: float) -> bool:
    """Prompt the operator for a Y/N answer on Windows.

    Returns True **only** when the operator presses ``y`` before the
    timeout. Any other key, Enter (empty input), or the timeout all
    return False, matching the ``[y/N]`` default-No convention.

    Uses ``msvcrt.kbhit()`` + ``msvcrt.getwch()`` so the operator does
    not need to press Enter — a single ``y`` starts the bot, a single
    ``n`` (or any other key) skips it. Arrow keys and other
    special-function keys are consumed and ignored so an accidental
    arrow press does not count as an answer.

    Safe to call from any thread, though it is intended to be called
    from the startup thread only.
    """
    try:
        import msvcrt  # type: ignore  # Windows-only stdlib module
    except ImportError:
        # Should not happen on Windows, but fall back to "No" so a
        # broken environment cannot silently start the bot.
        return False

    sys.stdout.write(question)
    sys.stdout.flush()

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if msvcrt.kbhit():
            ch = msvcrt.getwch()

            # Special keys (arrows, F1-F12, Home/End, ...) arrive as a
            # two-byte sequence whose first byte is \x00 or \xe0.
            # Consume the second byte and keep waiting.
            if ch in ("\x00", "\xe0"):
                try:
                    msvcrt.getwch()
                except Exception:
                    pass
                continue

            # Enter without any character typed — respect the default.
            if ch in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                return False

            sys.stdout.write(ch + "\n")
            sys.stdout.flush()
            return ch.lower() == "y"

        time.sleep(0.05)

    sys.stdout.write(" (timeout — default No)\n")
    sys.stdout.flush()
    return False


# --------------------------------------------------------------------------- #
# Bot                                                                         #
# --------------------------------------------------------------------------- #
class TelegramTokenBot:
    def __init__(self):
        self.bot_token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = str(config.TELEGRAM_CHAT_ID)
        self.enabled = bool(
            config.TELEGRAM_TOKEN_BOT_ENABLED
            and config.TELEGRAM_ENABLED
            and self.bot_token
            and self.chat_id
        )
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else None
        self.poll_seconds = config.TELEGRAM_TOKEN_BOT_POLL_SECONDS
        self.long_poll_timeout = config.TELEGRAM_TOKEN_BOT_LONG_POLL_TIMEOUT
        self.restrict_to_chat = config.TELEGRAM_TOKEN_BOT_RESTRICT_TO_CHAT
        self._stop_event = Event()
        self._thread = None
        self._offset = None
        self._lock = Lock()
        self._pending_save_token_chats = set()

        # ---- Runtime state (single source of truth) ------------------ #
        # True only while the polling loop is actually running. Read by
        # routes/admin_routes.py to report the correct telegram state
        # (previously the refresh reported True just because start()
        # did not raise, even when the operator declined the Windows
        # prompt).
        self._running = False
        self._state_lock = Lock()

    # ------------------------------------------------------------------ #
    # Runtime state accessors                                            #
    # ------------------------------------------------------------------ #
    @property
    def is_running(self) -> bool:
        """True while the polling loop is actually running."""
        with self._state_lock:
            return bool(self._running)

    @property
    def polling(self) -> bool:
        """Alias for ``is_running`` (compat with admin_routes probe)."""
        return self.is_running

    def _set_running(self, value: bool) -> None:
        with self._state_lock:
            self._running = bool(value)

    # ------------------------------------------------------------------ #
    # Windows start confirmation                                          #
    # ------------------------------------------------------------------ #
    def _confirm_start_on_windows(self) -> bool:
        """Ask the operator to confirm bot start on Windows.

        Fires only when:

          * the process is running on Windows (``os.name == "nt"``), AND
          * ``TELEGRAM_BOT_SKIP_WINDOWS_PROMPT`` is not True, AND
          * both stdin and stdout are attached to a console.

        Every other case is a no-op:

          * Linux / macOS                    -> return True (start).
          * Windows + skip-prompt override   -> return True (start).
          * Windows + no console             -> return False (skip;
                                                warning logged).
          * Windows + interactive console    -> prompt (y / 5 s /
                                                default No).

        Returns True to proceed with polling, False to skip.
        """
        # Not Windows — the operator's concern does not apply here.
        if os.name != "nt":
            return True

        # Operator has opted out of the prompt (headless deployments,
        # services, scheduled tasks that still want the bot running).
        if getattr(config, "TELEGRAM_BOT_SKIP_WINDOWS_PROMPT", False):
            logger.info(
                "TELEGRAM_BOT_SKIP_WINDOWS_PROMPT=true — starting "
                "Telegram bot on Windows without confirmation prompt."
            )
            return True

        # Windows headless: we cannot prompt, so do not start (the
        # [y/N] default is No).
        if not _console_is_interactive():
            logger.warning(
                "Windows: no interactive console detected — skipping "
                "Telegram bot start. Set "
                "TELEGRAM_BOT_SKIP_WINDOWS_PROMPT=true in .env to start "
                "without confirmation."
            )
            return False

        # Interactive Windows: prompt for 5 s, default No.
        timeout_seconds = float(
            getattr(config, "TELEGRAM_BOT_WINDOWS_PROMPT_TIMEOUT_SECONDS", 5.0)
        )
        try:
            started = _prompt_yes_no_windows(
                "Start Telegram bot polling on Windows? "
                f"[y/N] ({int(timeout_seconds)}s timeout): ",
                timeout_seconds=timeout_seconds,
            )
        except Exception:
            logger.exception(
                "Windows start-confirmation prompt failed; "
                "skipping bot start."
            )
            return False

        if started:
            logger.info("Operator confirmed — starting Telegram bot polling.")
        else:
            logger.info(
                "Operator declined or timed out — Telegram bot polling "
                "will not start. Re-launch and press 'y' at the prompt "
                "to enable the bot, or set "
                "TELEGRAM_BOT_SKIP_WINDOWS_PROMPT=true in .env."
            )
        return started

    @staticmethod
    def _command(text: str) -> str:
        return str(text or "").strip().split("@", 1)[0].split(maxsplit=1)[0].lower()

    def _request(self, method: str, *, payload=None, timeout=None):
        if not self.api_url:
            return None
        response = requests.post(
            f"{self.api_url}/{method}",
            json=payload or {},
            timeout=timeout or config.TELEGRAM_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError("Telegram API returned an unsuccessful response")
        return body.get("result")

    def _send(self, text: str, *, chat_id=None):
        target = str(chat_id or self.chat_id)
        if not self.enabled or not target:
            return False
        try:
            self._request("sendMessage", payload={"chat_id": target, "text": str(text)})
            return True
        except Exception as exc:
            logger.warning("Telegram message send failed (%s).", type(exc).__name__)
            return False

    def _authorized(self, chat_id) -> bool:
        if not chat_id:
            return False
        return not self.restrict_to_chat or str(chat_id) == self.chat_id

    def set_commands(self):
        try:
            self._request(
                "setMyCommands",
                payload={"commands": [
                    {"command": "save_token", "description": "Save a new Upstox access token"},
                    {"command": "token_status", "description": "Show masked token status"},
                    {"command": "token_check", "description": "Validate the stored token"},
                    {"command": "orders_status", "description": "Show order runtime toggle state"},
                    {"command": "orders_enable", "description": "Enable live order placement"},
                    {"command": "orders_disable", "description": "Disable live order placement"},
                    {"command": "cancel", "description": "Cancel token entry"},
                    {"command": "help", "description": "Show available commands"},
                ]},
            )
            return True
        except Exception as exc:
            logger.warning("Telegram command registration failed (%s).", type(exc).__name__)
            return False

    def get_updates(self):
        payload = {"timeout": self.long_poll_timeout, "allowed_updates": ["message"]}
        if self._offset is not None:
            payload["offset"] = self._offset
        return self._request("getUpdates", payload=payload, timeout=self.long_poll_timeout + 5) or []

    def delete_message(self, chat_id, message_id):
        try:
            self._request("deleteMessage", payload={"chat_id": chat_id, "message_id": message_id})
            return True
        except Exception as exc:
            logger.warning("Telegram token message deletion failed (%s).", type(exc).__name__)
            return False

    def _status(self, chat_id):
        safe_info = token_store.get_masked_token_info()
        self._send("Token document status:\n" + json.dumps(safe_info, indent=2, default=str), chat_id=chat_id)

    def _check(self, chat_id):
        result = check_upstox_token_validity(send_success_message=False)
        safe = {key: value for key, value in result.items() if key not in {"profile", "token", "access_token"}}
        self._send("Token validation result:\n" + json.dumps(safe, indent=2, default=str), chat_id=chat_id)

    def _save_token_message(self, message):
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        raw_token = str(message.get("text") or "").strip()
        with self._lock:
            self._pending_save_token_chats.discard(str(chat_id))
        self.delete_message(chat_id, message_id)

        if len(raw_token) < 50:
            self._send("Token was not saved because it appears incomplete. Use /save_token and send the complete token.", chat_id=chat_id)
            return

        valid, profile, _error = upstox_token_client.validate_token(raw_token)
        if not valid:
            self._send("Token was not saved because Upstox profile validation failed. Please generate a new token and try again.", chat_id=chat_id)
            return

        token_store.save_access_token(access_token=raw_token, source="telegram")
        token_store.update_validation_status(
            is_valid=True,
            status="saved_and_profile_validated",
            error=None,
            profile=profile,
        )
        self._send("New Upstox access token saved and validated successfully.", chat_id=chat_id)

    def handle_message(self, message: dict):
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not self._authorized(chat_id):
            if chat_id:
                logger.warning("Unauthorized Telegram chat attempted token bot use.")
            return
        text = str(message.get("text") or "").strip()
        command = self._command(text)

        # ---- Order runtime toggle commands ---------------------------- #
        if command.startswith("/orders"):
            handle_order_toggle_command(
                bot=self,
                chat_id=chat_id,
                command=command,
            )
            return

        with self._lock:
            pending = str(chat_id) in self._pending_save_token_chats

        if command == "/cancel":
            with self._lock:
                self._pending_save_token_chats.discard(str(chat_id))
            self._send("Pending token entry cancelled.", chat_id=chat_id)
        elif pending and text:
            self._save_token_message(message)
        elif command == "/save_token":
            with self._lock:
                self._pending_save_token_chats.add(str(chat_id))
            self._send("Paste the latest Upstox access token in your next private message. The message will be deleted after processing.", chat_id=chat_id)
        elif command in {"/token_status", "/token-status"}:
            self._status(chat_id)
        elif command in {"/token_check", "/token-check"}:
            self._check(chat_id)
        elif command in {"/help", "/start"}:
            self._send(
                "Available commands:\n"
                "/save_token — save a token\n"
                "/token_status — show masked token status\n"
                "/token_check — validate the stored token\n"
                "/orders_status — show order runtime toggle state\n"
                "/orders_enable — enable live order placement\n"
                "/orders_disable — disable live order placement\n"
                "/cancel — cancel token entry",
                chat_id=chat_id,
            )

    def run_loop(self):
        # Mark running only once the polling loop has truly begun, so
        # that "start() returned True but the thread has not entered
        # run_loop yet" is not reported as running by admin_routes.
        self._set_running(True)
        logger.info("Telegram token management polling started.")
        try:
            self.set_commands()
            self._send("Token management bot started. Use /help to see commands.")
            while not self._stop_event.is_set():
                try:
                    for update in self.get_updates():
                        self._offset = int(update.get("update_id", 0)) + 1
                        message = update.get("message")
                        if message:
                            self.handle_message(message)
                except Exception as exc:
                    logger.warning("Telegram polling failed (%s); retrying.", type(exc).__name__)
                    self._stop_event.wait(self.poll_seconds)
        finally:
            self._set_running(False)
            logger.info("Telegram token management polling stopped.")

    def start(self) -> bool:
        """Start polling, subject to the Windows confirmation gate.

        Order of checks:

          1. ``self.enabled`` — if False, return early (no prompt).
          2. Thread already alive — return True (idempotent).
          3. Windows confirmation — see ``_confirm_start_on_windows``.
             Skipped entirely on Linux / macOS.
          4. Spawn the polling thread.

        The Windows confirmation step is deliberately AFTER the
        "already running" check, so a re-entrant ``start()`` call
        (from the admin refresh or a hot reload) never re-prompts.

        Returns True when polling was started (or was already running),
        False otherwise. ``self.is_running`` is the authoritative
        runtime state and may lag by a few milliseconds while the
        spawned thread enters ``run_loop()``.
        """
        if not self.enabled:
            logger.info("Telegram token bot is disabled or not configured.")
            self._set_running(False)
            return False
        if self._thread and self._thread.is_alive():
            # Already running — refresh the state flag defensively.
            return True
        if not self._confirm_start_on_windows():
            self._set_running(False)
            return False
        self._stop_event.clear()
        self._thread = Thread(target=self.run_loop, name="telegram-token-bot", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.long_poll_timeout + 5)
        self._set_running(False)


telegram_token_bot = TelegramTokenBot()