"""Telegram long-polling bot for Upstox token management."""

import json
import time
from threading import Event, Lock, Thread

import requests

from core import config
from core.logger import get_logger
from token_tasks.token_monitor import check_upstox_token_validity
from token_tasks.token_store import token_store
from token_tasks.upstox_token_client import upstox_token_client

logger = get_logger(__file__)


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
            self._send("Available commands:\n/save_token — save a token\n/token_status — show masked token status\n/token_check — validate the stored token\n/cancel — cancel token entry", chat_id=chat_id)

    def run_loop(self):
        logger.info("Telegram token management polling started.")
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
        logger.info("Telegram token management polling stopped.")

    def start(self) -> bool:
        if not self.enabled:
            logger.info("Telegram token bot is disabled or not configured.")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop_event.clear()
        self._thread = Thread(target=self.run_loop, name="telegram-token-bot", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.long_poll_timeout + 5)


telegram_token_bot = TelegramTokenBot()
