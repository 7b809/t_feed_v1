import html
import json
import time
from threading import Event, Thread

import requests

from core import config
from core.logger import get_logger
from services.telegram_service import telegram_service
from token_tasks.token_store import token_store
from token_tasks.token_monitor import check_upstox_token_validity
from token_tasks.upstox_token_client import upstox_token_client

logger = get_logger(__file__)


class TelegramTokenBot:
    """
    Telegram command listener for Upstox token management.

    Supported typed commands:
        /save-token
        /token-status
        /profile
        /funds

    Telegram command menu compatible commands:
        /save_token
        /token_status
        /profile
        /funds

    Security behavior:
        - Only configured TELEGRAM_CHAT_ID is allowed by default.
        - Raw token is never logged.
        - Raw token is never sent back to Telegram.
        - Raw token message is deleted after processing when Telegram permits it.
        - Token is validated using Upstox get_profile before saving.
    """

    def __init__(self):
        self.bot_token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = str(config.TELEGRAM_CHAT_ID)

        self.enabled = bool(
            getattr(config, "TELEGRAM_TOKEN_BOT_ENABLED", True)
            and getattr(config, "TELEGRAM_ENABLED", False)
            and self.bot_token
            and self.chat_id
        )

        self.api_url = (
            f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else None
        )

        self.poll_seconds = int(getattr(config, "TELEGRAM_TOKEN_BOT_POLL_SECONDS", 3))

        self.long_poll_timeout = int(
            getattr(config, "TELEGRAM_TOKEN_BOT_LONG_POLL_TIMEOUT", 20)
        )

        self.restrict_to_chat = bool(
            getattr(config, "TELEGRAM_TOKEN_BOT_RESTRICT_TO_CHAT", True)
        )

        self._stop_event = Event()
        self._thread = None
        self._offset = None

        # chat_id strings waiting for next message to contain raw token.
        self._pending_save_token_chats = set()

    # ========================================================
    # Basic Helpers
    # ========================================================

    def _escape(self, value) -> str:
        """
        Escapes text for Telegram HTML parse mode.
        """

        return html.escape(str(value), quote=False)

    def _is_authorized_chat(self, chat_id) -> bool:
        """
        Restricts token commands to configured TELEGRAM_CHAT_ID.
        """

        if not self.restrict_to_chat:
            return True

        return str(chat_id) == self.chat_id

    def _request(
        self,
        method: str,
        payload: dict | None = None,
        timeout: int | None = None,
    ) -> dict:
        """
        Calls Telegram Bot API method.
        """

        if not self.api_url:
            return {
                "ok": False,
                "description": "Telegram API URL is not configured.",
            }

        url = f"{self.api_url}/{method}"

        try:
            response = requests.post(
                url,
                json=payload or {},
                timeout=timeout or self.long_poll_timeout + 10,
            )

            try:
                return response.json()
            except Exception:
                return {
                    "ok": False,
                    "description": response.text,
                    "status_code": response.status_code,
                }

        except Exception as ex:
            return {
                "ok": False,
                "description": f"{type(ex).__name__}: {ex}",
            }

    def _send_message(
        self,
        text: str,
        level: str = "TOKEN",
    ) -> bool:
        """
        Sends a Telegram message using existing project TelegramService.
        """

        return telegram_service.send_message(
            title="Token Bot",
            message=text,
            level=level,
        )

    def _format_json_for_telegram(
        self,
        payload,
        max_chars: int = 3200,
    ) -> str:
        """
        Formats JSON payload for Telegram.

        Telegram messages have a length limit. This keeps response safe.
        """

        try:
            text = json.dumps(
                payload,
                indent=2,
                default=str,
                ensure_ascii=False,
            )
        except Exception:
            text = str(payload)

        if len(text) > max_chars:
            text = text[:max_chars] + "\n...trimmed..."

        return text

    def _normalize_command(self, text: str) -> str:
        """
        Normalizes command.

        Supports:
            /save-token
            /save_token
            /token-status
            /token_status
        """

        command = str(text or "").strip().split()[0].lower()

        # Remove bot username suffix if message is like /profile@MyBot
        if "@" in command:
            command = command.split("@", 1)[0]

        return command

    # ========================================================
    # Telegram API Helpers
    # ========================================================

    def delete_message(
        self,
        chat_id,
        message_id,
    ) -> bool:
        """
        Attempts to delete a Telegram message.

        Used for deleting raw token message after user sends it.
        """

        result = self._request(
            "deleteMessage",
            {
                "chat_id": chat_id,
                "message_id": message_id,
            },
            timeout=10,
        )

        if not result.get("ok"):
            logger.warning(
                f"Telegram deleteMessage failed. "
                f"chat_id={chat_id}, message_id={message_id}, result={result}"
            )

        return bool(result.get("ok"))

    def set_commands(self) -> bool:
        """
        Registers command menu in Telegram.

        Telegram command menu does not reliably support hyphen commands,
        so menu commands use underscores. Typed hyphen commands are still handled.
        """

        if not self.enabled:
            return False

        result = self._request(
            "setMyCommands",
            {
                "commands": [
                    {
                        "command": "save_token",
                        "description": "Save new Upstox access token",
                    },
                    {
                        "command": "token_status",
                        "description": "Check current token status",
                    },
                    {
                        "command": "profile",
                        "description": "Get Upstox profile using current token",
                    },
                    {
                        "command": "funds",
                        "description": "Get Upstox funds and margin using current token",
                    },
                ]
            },
            timeout=10,
        )

        if result.get("ok"):
            logger.info("Telegram token bot commands registered successfully.")
        else:
            logger.warning(f"Telegram token bot command registration failed: {result}")

        return bool(result.get("ok"))

    def get_updates(self) -> list:
        """
        Long-polls Telegram updates.
        """

        payload = {
            "timeout": self.long_poll_timeout,
            "allowed_updates": ["message"],
        }

        if self._offset is not None:
            payload["offset"] = self._offset

        result = self._request(
            "getUpdates",
            payload,
            timeout=self.long_poll_timeout + 10,
        )

        if not result.get("ok"):
            logger.warning(f"Telegram getUpdates failed. result={result}")
            return []

        updates = result.get("result", [])

        if updates:
            self._offset = max(int(item.get("update_id", 0)) for item in updates) + 1

        return updates

    # ========================================================
    # Command Handlers
    # ========================================================

    def _handle_save_token_command(self, chat_id):
        """
        Starts save-token flow.
        """

        self._pending_save_token_chats.add(str(chat_id))

        self._send_message(
            "Please paste the latest raw Upstox access token in the next message.\n\n"
            "For safety, I will delete the token message after saving or validating it.\n\n"
            "Note: Do not send the token in a group chat.",
            level="TOKEN",
        )

    def _handle_token_status_command(self):
        """
        Sends masked token status from MongoDB.
        """

        info = token_store.get_masked_token_info()

        self._send_message(
            "Current token document status:\n\n"
            f"{self._format_json_for_telegram(info)}",
            level="TOKEN",
        )

    def _handle_token_status_validate_command(self):
        """
        Validates token immediately and sends status message.
        """

        result = check_upstox_token_validity(
            send_success_message=True,
        )

        safe_result = dict(result)
        safe_result.pop("profile", None)

        self._send_message(
            "Token validation result:\n\n"
            f"{self._format_json_for_telegram(safe_result)}",
            level="TOKEN",
        )

    def _handle_profile_command(self):
        """
        Calls Upstox profile API and returns raw response to Telegram.
        """

        token = token_store.get_access_token()

        if not token:
            self._send_message(
                "No Upstox token found in MongoDB.\n\n"
                "Use /save-token or /save_token first.",
                level="WARNING",
            )
            return

        profile_result = upstox_token_client.get_profile_safe(token)

        response = profile_result.get("response")

        if isinstance(response, dict):
            is_valid = str(response.get("status", "")).lower() == "success"

            token_store.update_validation_status(
                is_valid=is_valid,
                status="profile_manual",
                error=None if is_valid else "manual profile returned non-success",
                profile=response,
            )

        self._send_message(
            "Upstox profile raw response:\n\n"
            f"{self._format_json_for_telegram(profile_result)}",
            level="TOKEN",
        )

    def _handle_funds_command(self):
        """
        Calls Upstox funds and margin API and returns raw response to Telegram.
        """

        token = token_store.get_access_token()

        if not token:
            self._send_message(
                "No Upstox token found in MongoDB.\n\n"
                "Use /save-token or /save_token first.",
                level="WARNING",
            )
            return

        funds_result = upstox_token_client.get_funds_safe(token)

        self._send_message(
            "Upstox funds raw response:\n\n"
            f"{self._format_json_for_telegram(funds_result)}",
            level="TOKEN",
        )

    def _handle_help_command(self):
        """
        Sends help text.
        """

        self._send_message(
            "Available token commands:\n\n"
            "/save-token or /save_token\n"
            "Save new Upstox access token. Bot will ask you to paste the token next.\n\n"
            "/token-status or /token_status\n"
            "Show masked token document status.\n\n"
            "/profile\n"
            "Get Upstox profile raw response using current token.\n\n"
            "/funds\n"
            "Get Upstox funds and margin raw response using current token.\n\n"
            "/token-check or /token_check\n"
            "Immediately validate token using Upstox profile API.",
            level="INFO",
        )

    def _handle_raw_token_message(
        self,
        chat_id,
        message_id,
        text: str,
    ):
        """
        Handles next message after /save-token.

        Flow:
            1. Delete raw token message immediately.
            2. Validate token using Upstox profile API.
            3. Save token to MongoDB only if valid.
            4. Send success/failure Telegram message.
        """

        raw_token = str(text or "").strip()

        # Remove pending flag first to avoid loops.
        self._pending_save_token_chats.discard(str(chat_id))

        # Delete token message as early as possible.
        deleted = self.delete_message(
            chat_id=chat_id,
            message_id=message_id,
        )

        if not raw_token:
            self._send_message(
                "Token message was empty. Please try /save-token again.",
                level="WARNING",
            )
            return

        if len(raw_token) < 50:
            self._send_message(
                "Token looks too short and was not saved.\n\n"
                "Please try /save-token again with the full Upstox access token.",
                level="WARNING",
            )
            return

        is_valid, profile, error = upstox_token_client.validate_token(raw_token)

        if not is_valid:
            self._send_message(
                "The token was not saved because Upstox profile validation failed.\n\n"
                f"Error: {error}\n\n"
                f"Token message deleted: {deleted}",
                level="ERROR",
            )
            return

        saved_doc = token_store.save_access_token(
            access_token=raw_token,
            source="telegram",
        )

        token_store.update_validation_status(
            is_valid=True,
            status="saved_and_profile_validated",
            error=None,
            profile=profile,
        )

        profile_data = profile.get("data", {}) if isinstance(profile, dict) else {}

        self._send_message(
            "New Upstox access token saved successfully.\n\n"
            f"Document ID: {saved_doc.get('_id')}\n"
            f"Created At: {saved_doc.get('created_at')}\n"
            f"Updated At: {saved_doc.get('updated_at')}\n"
            f"Token: {saved_doc.get('access_token')}\n"
            f"Profile User ID: {profile_data.get('user_id')}\n"
            f"Profile User Name: {profile_data.get('user_name')}\n"
            f"Broker: {profile_data.get('broker')}\n"
            f"Is Active: {profile_data.get('is_active')}\n"
            f"Token message deleted: {deleted}",
            level="SUCCESS",
        )

    # ========================================================
    # Message Dispatcher
    # ========================================================

    def handle_message(self, message: dict):
        """
        Dispatches Telegram message to command handlers.
        """

        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        text = str(message.get("text") or "").strip()

        if not chat_id or not message_id:
            return

        if not self._is_authorized_chat(chat_id):
            logger.warning(
                f"Unauthorized Telegram chat attempted token bot use: {chat_id}"
            )
            return

        if not text:
            return

        if str(chat_id) in self._pending_save_token_chats:
            self._handle_raw_token_message(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
            )
            return

        command = self._normalize_command(text)

        if command in ["/save-token", "/save_token"]:
            self._handle_save_token_command(chat_id)
            return

        if command in ["/token-status", "/token_status"]:
            self._handle_token_status_command()
            return

        if command in ["/token-check", "/token_check"]:
            self._handle_token_status_validate_command()
            return

        if command == "/profile":
            self._handle_profile_command()
            return

        if command == "/funds":
            self._handle_funds_command()
            return

        if command in ["/start", "/help"]:
            self._handle_help_command()
            return

    # ========================================================
    # Lifecycle
    # ========================================================

    def run_loop(self):
        """
        Telegram polling loop.

        Note:
            This uses getUpdates polling.
            Do not use webhook mode with the same bot at the same time.
        """

        logger.info("Telegram token bot polling loop started.")

        self.set_commands()

        self._send_message(
            "Telegram token bot started.\n\n"
            "Commands:\n"
            "/save-token or /save_token\n"
            "/token-status or /token_status\n"
            "/profile\n"
            "/funds\n"
            "/token-check or /token_check",
            level="INFO",
        )

        while not self._stop_event.is_set():
            try:
                updates = self.get_updates()

                for update in updates:
                    message = update.get("message")

                    if message:
                        self.handle_message(message)

            except Exception as ex:
                logger.error(
                    f"Telegram token bot loop error: {type(ex).__name__}: {ex}"
                )
                time.sleep(self.poll_seconds)

        logger.info("Telegram token bot polling loop stopped.")

    def start(self) -> bool:
        """
        Starts Telegram token bot background thread.
        """

        if not self.enabled:
            logger.info(
                "Telegram token bot is disabled or not configured. "
                "Check TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, "
                "and TELEGRAM_TOKEN_BOT_ENABLED."
            )
            return False

        if self._thread and self._thread.is_alive():
            logger.info("Telegram token bot is already running.")
            return True

        self._stop_event.clear()

        self._thread = Thread(
            target=self.run_loop,
            name="telegram-token-bot",
            daemon=True,
        )

        self._thread.start()

        logger.info("Telegram token bot started.")

        return True

    def stop(self):
        """
        Stops Telegram token bot background thread.
        """

        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)


# Singleton instance shared across the application
telegram_token_bot = TelegramTokenBot()