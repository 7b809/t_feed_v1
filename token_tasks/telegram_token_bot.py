import html
import json
import time
from threading import Event, RLock, Thread

import requests

from core import config
from core.logger import get_logger
from services.runtime_config_service import (
    RuntimeConfigError,
    RuntimeConfigValidationError,
    get_runtime_config_categories,
    get_runtime_config_metadata,
    get_runtime_config_registry,
    initialize_runtime_config,
    refresh_runtime_config,
    reset_runtime_config,
    runtime_config_service,
    set_runtime_config,
)
from services.telegram_service import telegram_service
from token_tasks.token_monitor import check_upstox_token_validity
from token_tasks.token_store import token_store
from token_tasks.upstox_token_client import upstox_token_client

logger = get_logger(__file__)

CATEGORY_LABELS = {"opening_range_isolation": "Opening Range Isolation", "opening_range_touch": "Opening Range Touch", "ema": "EMA", "ema_budget": "EMA Budget", "ema_order": "EMA Order"}
CATEGORY_SHORT_CODES = {"opening_range_isolation": "ori", "opening_range_touch": "ort", "ema": "ema", "ema_budget": "emb", "ema_order": "emo"}
SHORT_CODE_CATEGORIES = {value: key for key, value in CATEGORY_SHORT_CODES.items()}


class TelegramTokenBot:
    def __init__(self):
        self.bot_token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = str(config.TELEGRAM_CHAT_ID)
        self.enabled = bool(getattr(config, "TELEGRAM_TOKEN_BOT_ENABLED", True) and getattr(config, "TELEGRAM_ENABLED", False) and self.bot_token and self.chat_id)
        self.runtime_config_enabled = bool(getattr(config, "TELEGRAM_RUNTIME_CONFIG_ENABLED", True))
        self.config_confirmation_required = bool(getattr(config, "TELEGRAM_RUNTIME_CONFIG_CONFIRMATION_REQUIRED", True))
        self.custom_value_enabled = bool(getattr(config, "TELEGRAM_RUNTIME_CONFIG_CUSTOM_VALUE_ENABLED", True))
        self.config_session_timeout = max(30, int(getattr(config, "TELEGRAM_RUNTIME_CONFIG_TIMEOUT_SECONDS", 120)))
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else None
        self.poll_seconds = max(1, int(getattr(config, "TELEGRAM_TOKEN_BOT_POLL_SECONDS", 3)))
        self.long_poll_timeout = max(1, int(getattr(config, "TELEGRAM_TOKEN_BOT_LONG_POLL_TIMEOUT", 20)))
        self.restrict_to_chat = bool(getattr(config, "TELEGRAM_TOKEN_BOT_RESTRICT_TO_CHAT", True))
        self._stop_event = Event()
        self._thread = None
        self._offset = None
        self._state_lock = RLock()
        self._pending_save_token_chats = set()
        self._pending_config_inputs = {}
        self._pending_config_confirmations = {}
        self._config_name_to_code = {}
        self._config_code_to_name = {}
        self._build_config_code_map()

    def _build_config_code_map(self):
        try:
            registry = get_runtime_config_registry(telegram_only=True)
        except Exception:
            registry = {}
        with self._state_lock:
            self._config_name_to_code.clear()
            self._config_code_to_name.clear()
            for index, name in enumerate(sorted(registry), start=1):
                code = f"c{index}"
                self._config_name_to_code[name] = code
                self._config_code_to_name[code] = name

    @staticmethod
    def _escape(value) -> str:
        return html.escape(str(value), quote=False)

    def _is_authorized_chat(self, chat_id) -> bool:
        if not self.restrict_to_chat:
            return True
        return str(chat_id) == self.chat_id

    def _request(self, method: str, payload: dict | None = None, timeout: int | None = None) -> dict:
        if not self.api_url:
            return {"ok": False, "description": "Telegram API URL is not configured."}
        url = f"{self.api_url}/{method}"
        try:
            response = requests.post(url, json=payload or {}, timeout=timeout or self.long_poll_timeout + 10)
            try:
                return response.json()
            except Exception:
                return {"ok": False, "description": response.text, "status_code": response.status_code}
        except Exception as ex:
            return {"ok": False, "description": f"{type(ex).__name__}: {ex}"}

    def _send_message(self, text: str, level: str = "TOKEN") -> bool:
        try:
            return bool(telegram_service.send_message(title="Token Bot", message=text, level=level))
        except Exception as ex:
            logger.error("Telegram service send failed. error=%s: %s", type(ex).__name__, ex)
            return False

    def _send_bot_message(self, chat_id, text: str, reply_markup: dict | None = None) -> dict:
        payload = {"chat_id": chat_id, "text": str(text), "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result = self._request("sendMessage", payload, timeout=15)
        if not result.get("ok"):
            logger.warning("Telegram sendMessage failed. chat_id=%s, result=%s", chat_id, result)
        return result

    def _edit_bot_message(self, chat_id, message_id, text: str, reply_markup: dict | None = None) -> dict:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": str(text), "disable_web_page_preview": True}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = self._request("editMessageText", payload, timeout=15)
        if not result.get("ok"):
            description = str(result.get("description") or "").lower()
            if "message is not modified" not in description:
                logger.warning("Telegram editMessageText failed. chat_id=%s, message_id=%s, result=%s", chat_id, message_id, result)
        return result

    def _answer_callback_query(self, callback_query_id, text: str | None = None, show_alert: bool = False):
        payload = {"callback_query_id": callback_query_id, "show_alert": bool(show_alert)}
        if text:
            payload["text"] = str(text)[:200]
        self._request("answerCallbackQuery", payload, timeout=10)

    def _format_json_for_telegram(self, payload, max_chars: int = 3200) -> str:
        try:
            text = json.dumps(payload, indent=2, default=str, ensure_ascii=False)
        except Exception:
            text = str(payload)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...trimmed..."
        return text

    @staticmethod
    def _normalize_command(text: str) -> str:
        command = str(text or "").strip().split()[0].lower()
        if "@" in command:
            command = command.split("@", 1)[0]
        return command

    @staticmethod
    def _format_config_value(value) -> str:
        if isinstance(value, bool):
            return "Enabled" if value else "Disabled"
        if isinstance(value, (list, tuple, set)):
            return ", ".join(str(item) for item in value)
        if isinstance(value, float):
            if value.is_integer():
                return str(int(value))
            return f"{value:.4f}".rstrip("0").rstrip(".")
        if value is None:
            return "Not available"
        return str(value)

    @staticmethod
    def _serialize_callback_value(value) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (list, tuple, set)):
            return ",".join(str(item) for item in value)
        return str(value)

    @staticmethod
    def _config_actor(message_or_callback: dict) -> str:
        user = message_or_callback.get("from") or {}
        username = str(user.get("username") or "").strip()
        first_name = str(user.get("first_name") or "").strip()
        user_id = user.get("id")
        if username:
            return f"@{username}"
        if first_name and user_id:
            return f"{first_name} ({user_id})"
        if user_id:
            return str(user_id)
        return "telegram_user"

    def _cleanup_expired_config_sessions(self):
        now = time.time()
        with self._state_lock:
            expired_inputs = [chat_id for chat_id, state in self._pending_config_inputs.items() if now - float(state.get("created_at", 0)) > self.config_session_timeout]
            for chat_id in expired_inputs:
                self._pending_config_inputs.pop(chat_id, None)
            expired_confirmations = [key for key, state in self._pending_config_confirmations.items() if now - float(state.get("created_at", 0)) > self.config_session_timeout]
            for key in expired_confirmations:
                self._pending_config_confirmations.pop(key, None)

    def delete_message(self, chat_id, message_id) -> bool:
        result = self._request("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, timeout=10)
        if not result.get("ok"):
            logger.warning("Telegram deleteMessage failed. chat_id=%s, message_id=%s, result=%s", chat_id, message_id, result)
        return bool(result.get("ok"))

    def set_commands(self) -> bool:
        if not self.enabled:
            return False
        commands = [
            {"command": "save_token", "description": "Save new Upstox access token"},
            {"command": "token_status", "description": "Check current token status"},
            {"command": "token_check", "description": "Validate current Upstox token"},
            {"command": "profile", "description": "Get current Upstox profile"},
            {"command": "funds", "description": "Get Upstox funds and margin"},
        ]
        if self.runtime_config_enabled:
            commands.extend([
                {"command": "config", "description": "Open runtime configuration"},
                {"command": "config_status", "description": "Show runtime config status"},
                {"command": "config_refresh", "description": "Reload runtime config from MongoDB"},
            ])
        commands.append({"command": "help", "description": "Show available commands"})
        result = self._request("setMyCommands", {"commands": commands}, timeout=10)
        if result.get("ok"):
            logger.info("Telegram bot commands registered.")
        else:
            logger.warning("Telegram bot command registration failed: %s", result)
        return bool(result.get("ok"))

    def get_updates(self) -> list:
        payload = {"timeout": self.long_poll_timeout, "allowed_updates": ["message", "callback_query"]}
        if self._offset is not None:
            payload["offset"] = self._offset
        result = self._request("getUpdates", payload, timeout=self.long_poll_timeout + 10)
        if not result.get("ok"):
            logger.warning("Telegram getUpdates failed. result=%s", result)
            return []
        updates = result.get("result", [])
        if updates:
            self._offset = max(int(item.get("update_id", 0)) for item in updates) + 1
        return updates

    def _handle_save_token_command(self, chat_id):
        with self._state_lock:
            self._pending_save_token_chats.add(str(chat_id))
            self._pending_config_inputs.pop(str(chat_id), None)
        self._send_message("Please paste the latest raw Upstox access token in the next message.\n\nFor safety, the token message will be deleted after processing.\n\nDo not send the token in a group chat.", level="TOKEN")

    def _handle_token_status_command(self):
        info = token_store.get_masked_token_info()
        self._send_message("Current token document status:\n\n" + self._format_json_for_telegram(info), level="TOKEN")

    def _handle_token_status_validate_command(self):
        result = check_upstox_token_validity(send_success_message=True)
        safe_result = dict(result)
        safe_result.pop("profile", None)
        self._send_message("Token validation result:\n\n" + self._format_json_for_telegram(safe_result), level="TOKEN")

    def _handle_profile_command(self):
        token = token_store.get_access_token()
        if not token:
            self._send_message("No Upstox token found in MongoDB.\n\nUse /save_token first.", level="WARNING")
            return
        profile_result = upstox_token_client.get_profile_safe(token)
        response = profile_result.get("response")
        if isinstance(response, dict):
            is_valid = str(response.get("status", "")).lower() == "success"
            token_store.update_validation_status(is_valid=is_valid, status="profile_manual", error=None if is_valid else "manual profile returned non-success", profile=response)
        self._send_message("Upstox profile raw response:\n\n" + self._format_json_for_telegram(profile_result), level="TOKEN")

    def _handle_funds_command(self):
        token = token_store.get_access_token()
        if not token:
            self._send_message("No Upstox token found in MongoDB.\n\nUse /save_token first.", level="WARNING")
            return
        funds_result = upstox_token_client.get_funds_safe(token)
        self._send_message("Upstox funds raw response:\n\n" + self._format_json_for_telegram(funds_result), level="TOKEN")

    def _config_main_keyboard(self) -> dict:
        try:
            categories = get_runtime_config_categories(telegram_only=True)
        except RuntimeConfigError:
            categories = []
        rows = []
        for category in categories:
            code = CATEGORY_SHORT_CODES.get(category)
            if not code:
                continue
            rows.append([{"text": CATEGORY_LABELS.get(category, category.replace("_", " ").title()), "callback_data": f"cfg:cat:{code}"}])
        rows.append([{"text": "Refresh", "callback_data": "cfg:refresh"}, {"text": "Status", "callback_data": "cfg:status"}])
        rows.append([{"text": "Close", "callback_data": "cfg:close"}])
        return {"inline_keyboard": rows}

    def _handle_config_command(self, chat_id):
        if not self.runtime_config_enabled:
            self._send_bot_message(chat_id, "Runtime configuration from Telegram is disabled.")
            return
        try:
            initialize_runtime_config()
            self._build_config_code_map()
        except Exception as ex:
            logger.error("Runtime config initialization failed. error=%s: %s", type(ex).__name__, ex)
            self._send_bot_message(chat_id, "Runtime configuration could not be initialized.")
            return
        self._send_bot_message(chat_id, "Runtime Configuration\n\nSelect a configuration category.", reply_markup=self._config_main_keyboard())

    def _show_config_categories(self, chat_id, message_id=None):
        text = "Runtime Configuration\n\nSelect a configuration category."
        keyboard = self._config_main_keyboard()
        if message_id:
            self._edit_bot_message(chat_id, message_id, text, reply_markup=keyboard)
        else:
            self._send_bot_message(chat_id, text, reply_markup=keyboard)

    def _show_config_category(self, chat_id, message_id, category_code):
        category = SHORT_CODE_CATEGORIES.get(category_code)
        if not category:
            self._answer_invalid_config(chat_id, "Unknown configuration category.")
            return
        try:
            registry = get_runtime_config_registry(telegram_only=True, category=category)
        except RuntimeConfigError as ex:
            self._send_bot_message(chat_id, f"Unable to load configuration: {ex}")
            return
        rows = []
        for name, metadata in registry.items():
            code = self._config_name_to_code.get(name)
            if not code:
                continue
            value_text = self._format_config_value(metadata.get("effective_value"))
            override_marker = " *" if metadata.get("has_runtime_override") else ""
            button_text = f"{metadata.get('label', name)}: {value_text}{override_marker}"
            if len(button_text) > 60:
                button_text = button_text[:57] + "..."
            rows.append([{"text": button_text, "callback_data": f"cfg:item:{code}"}])
        rows.append([{"text": "Back", "callback_data": "cfg:home"}, {"text": "Close", "callback_data": "cfg:close"}])
        category_label = CATEGORY_LABELS.get(category, category.replace("_", " ").title())
        text = f"{category_label}\n\nSelect a configuration.\n* indicates a runtime override."
        self._edit_bot_message(chat_id, message_id, text, reply_markup={"inline_keyboard": rows})

    def _show_config_item(self, chat_id, message_id, config_code):
        name = self._config_code_to_name.get(config_code)
        if not name:
            self._send_bot_message(chat_id, "Configuration mapping expired. Open /config again.")
            return
        try:
            metadata = get_runtime_config_metadata(name)
        except RuntimeConfigError as ex:
            self._send_bot_message(chat_id, f"Unable to load configuration: {ex}")
            return
        category = metadata.get("category")
        category_code = CATEGORY_SHORT_CODES.get(category, "ori")
        current_value = self._format_config_value(metadata.get("effective_value"))
        default_value = self._format_config_value(metadata.get("default"))
        source_text = "Runtime override" if metadata.get("has_runtime_override") else "Environment/default"
        text = f"{metadata.get('label', name)}\n\nCurrent: {current_value}\nDefault: {default_value}\nSource: {source_text}\nType: {metadata.get('value_type')}\nRestart required: {'Yes' if metadata.get('restart_required') else 'No'}\nRecalculation required: {'Yes' if metadata.get('recalculation_required') else 'No'}"
        description = str(metadata.get("description") or "").strip()
        if description:
            text += f"\n\n{description}"
        rows = []
        suggested_values = metadata.get("suggested_values") or []
        value_buttons = []
        for index, suggested_value in enumerate(suggested_values):
            display_value = self._format_config_value(suggested_value)
            value_buttons.append({"text": display_value, "callback_data": f"cfg:val:{config_code}:{index}"})
            if len(value_buttons) == 2:
                rows.append(value_buttons)
                value_buttons = []
        if value_buttons:
            rows.append(value_buttons)
        value_type = metadata.get("value_type")
        if self.custom_value_enabled and value_type in {"int", "float", "string", "string_list", "int_list"}:
            rows.append([{"text": "Custom Value", "callback_data": f"cfg:custom:{config_code}"}])
        if metadata.get("has_runtime_override"):
            rows.append([{"text": "Reset to Default", "callback_data": f"cfg:reset:{config_code}"}])
        rows.append([{"text": "Back", "callback_data": f"cfg:cat:{category_code}"}, {"text": "Close", "callback_data": "cfg:close"}])
        self._edit_bot_message(chat_id, message_id, text, reply_markup={"inline_keyboard": rows})

    def _prepare_config_value(self, chat_id, message_id, callback_query, config_code, raw_value):
        name = self._config_code_to_name.get(config_code)
        if not name:
            self._send_bot_message(chat_id, "Configuration mapping expired. Open /config again.")
            return
        try:
            metadata = get_runtime_config_metadata(name)
            validated_value = runtime_config_service.validate(name, raw_value)
        except RuntimeConfigError as ex:
            self._send_bot_message(chat_id, f"Invalid configuration value: {ex}")
            return
        actor = self._config_actor(callback_query)
        confirmation_key = f"{chat_id}:{config_code}"
        confirmation = {"name": name, "value": deepcopy_value(validated_value), "actor": actor, "chat_id": str(chat_id), "created_at": time.time()}
        if not self.config_confirmation_required:
            self._apply_config_change(chat_id=chat_id, message_id=message_id, config_code=config_code, confirmation=confirmation)
            return
        with self._state_lock:
            self._pending_config_confirmations[confirmation_key] = confirmation
        old_value = self._format_config_value(metadata.get("effective_value"))
        new_value = self._format_config_value(validated_value)
        text = f"Confirm Configuration Change\n\nConfiguration: {metadata.get('label', name)}\nOld value: {old_value}\nNew value: {new_value}"
        self._edit_bot_message(chat_id, message_id, text, reply_markup={"inline_keyboard": [[{"text": "Confirm", "callback_data": f"cfg:confirm:{config_code}"}, {"text": "Cancel", "callback_data": f"cfg:item:{config_code}"}]]})

    def _apply_config_change(self, chat_id, message_id, config_code, confirmation):
        name = confirmation.get("name")
        value = confirmation.get("value")
        actor = confirmation.get("actor", "telegram_user")
        try:
            result = set_runtime_config(name=name, value=value, changed_by=actor, source="telegram", chat_id=chat_id, reason="Changed from Telegram runtime configuration menu.")
        except RuntimeConfigValidationError as ex:
            self._send_bot_message(chat_id, f"Validation failed: {ex}")
            return
        except RuntimeConfigError as ex:
            self._send_bot_message(chat_id, f"Configuration update failed: {ex}")
            return
        except Exception as ex:
            logger.exception("Unexpected runtime configuration update failure.")
            self._send_bot_message(chat_id, f"Configuration update failed due to {type(ex).__name__}.")
            return
        old_value = self._format_config_value(result.get("old_value"))
        new_value = self._format_config_value(result.get("new_value"))
        text = f"Configuration Updated\n\nConfiguration: {result.get('label', name)}\nOld value: {old_value}\nNew value: {new_value}\nChanged: {'Yes' if result.get('changed') else 'No'}\nSaved to MongoDB: {'Yes' if result.get('persisted') else 'No'}\nRestart required: {'Yes' if result.get('restart_required') else 'No'}\nRecalculation required: {'Yes' if result.get('recalculation_required') else 'No'}"
        self._edit_bot_message(chat_id, message_id, text, reply_markup={"inline_keyboard": [[{"text": "View Setting", "callback_data": f"cfg:item:{config_code}"}, {"text": "Config Home", "callback_data": "cfg:home"}]]})

    def _handle_custom_config_request(self, chat_id, message_id, config_code, callback_query):
        name = self._config_code_to_name.get(config_code)
        if not name:
            self._send_bot_message(chat_id, "Configuration mapping expired. Open /config again.")
            return
        try:
            metadata = get_runtime_config_metadata(name)
        except RuntimeConfigError as ex:
            self._send_bot_message(chat_id, f"Unable to load configuration: {ex}")
            return
        with self._state_lock:
            self._pending_config_inputs[str(chat_id)] = {"name": name, "config_code": config_code, "actor": self._config_actor(callback_query), "created_at": time.time()}
        value_type = metadata.get("value_type")
        guidance = {"int": "Send a whole number.", "float": "Send a numeric value.", "string": "Send the new text value.", "string_list": "Send comma-separated values.", "int_list": "Send comma-separated whole numbers."}.get(value_type, "Send the new value.")
        text = f"{metadata.get('label', name)}\n\nCurrent value: {self._format_config_value(metadata.get('effective_value'))}\n\n{guidance}\nSend /cancel to cancel."
        self._edit_bot_message(chat_id, message_id, text, reply_markup={"inline_keyboard": [[{"text": "Cancel", "callback_data": f"cfg:item:{config_code}"}]]})

    def _handle_custom_config_message(self, message: dict, pending_state: dict):
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = str(message.get("text") or "").strip()
        config_code = pending_state.get("config_code")
        name = pending_state.get("name")
        with self._state_lock:
            self._pending_config_inputs.pop(str(chat_id), None)
        if text.lower() == "/cancel":
            self._send_bot_message(chat_id, "Configuration change cancelled.")
            return
        try:
            metadata = get_runtime_config_metadata(name)
            validated_value = runtime_config_service.validate(name, text)
        except RuntimeConfigError as ex:
            self._send_bot_message(chat_id, f"Invalid value: {ex}\n\nOpen /config and try again.")
            return
        confirmation = {"name": name, "value": deepcopy_value(validated_value), "actor": pending_state.get("actor", self._config_actor(message)), "chat_id": str(chat_id), "created_at": time.time()}
        if not self.config_confirmation_required:
            try:
                result = set_runtime_config(name=name, value=validated_value, changed_by=confirmation["actor"], source="telegram", chat_id=chat_id, reason="Custom value changed from Telegram.")
            except RuntimeConfigError as ex:
                self._send_bot_message(chat_id, f"Configuration update failed: {ex}")
                return
            self._send_bot_message(chat_id, f"Configuration Updated\n\nConfiguration: {result.get('label', name)}\nOld value: {self._format_config_value(result.get('old_value'))}\nNew value: {self._format_config_value(result.get('new_value'))}", reply_markup={"inline_keyboard": [[{"text": "Open Config", "callback_data": "cfg:home"}]]})
            return
        confirmation_key = f"{chat_id}:{config_code}"
        with self._state_lock:
            self._pending_config_confirmations[confirmation_key] = confirmation
        self._send_bot_message(chat_id, f"Confirm Configuration Change\n\nConfiguration: {metadata.get('label', name)}\nOld value: {self._format_config_value(metadata.get('effective_value'))}\nNew value: {self._format_config_value(validated_value)}", reply_markup={"inline_keyboard": [[{"text": "Confirm", "callback_data": f"cfg:confirm:{config_code}"}, {"text": "Cancel", "callback_data": f"cfg:item:{config_code}"}]]})

    def _handle_config_reset(self, chat_id, message_id, config_code, callback_query):
        name = self._config_code_to_name.get(config_code)
        if not name:
            self._send_bot_message(chat_id, "Configuration mapping expired. Open /config again.")
            return
        try:
            result = reset_runtime_config(name=name, changed_by=self._config_actor(callback_query), source="telegram", chat_id=chat_id, reason="Reset from Telegram runtime configuration menu.")
        except RuntimeConfigError as ex:
            self._send_bot_message(chat_id, f"Configuration reset failed: {ex}")
            return
        self._edit_bot_message(chat_id, message_id, f"Configuration Reset\n\nConfiguration: {result.get('label', name)}\nPrevious value: {self._format_config_value(result.get('old_value'))}\nCurrent value: {self._format_config_value(result.get('effective_value'))}", reply_markup={"inline_keyboard": [[{"text": "View Setting", "callback_data": f"cfg:item:{config_code}"}, {"text": "Config Home", "callback_data": "cfg:home"}]]})

    def _handle_config_status(self, chat_id, message_id=None):
        try:
            status = runtime_config_service.get_status()
        except Exception as ex:
            status = {"enabled": False, "initialized": False, "error": f"{type(ex).__name__}: {ex}"}
        text = f"Runtime Configuration Status\n\nEnabled: {'Yes' if status.get('enabled') else 'No'}\nInitialized: {'Yes' if status.get('initialized') else 'No'}\nMongoDB connected: {'Yes' if status.get('database_connected') else 'No'}\nCache enabled: {'Yes' if status.get('cache_enabled') else 'No'}\nRuntime overrides: {status.get('override_count', 0)}\nRegistered settings: {status.get('registry_count', 0)}"
        if status.get("error"):
            text += f"\nError: {status.get('error')}"
        keyboard = {"inline_keyboard": [[{"text": "Refresh", "callback_data": "cfg:refresh"}, {"text": "Config Home", "callback_data": "cfg:home"}]]}
        if message_id:
            self._edit_bot_message(chat_id, message_id, text, reply_markup=keyboard)
        else:
            self._send_bot_message(chat_id, text, reply_markup=keyboard)

    def _handle_config_refresh(self, chat_id, message_id=None):
        try:
            status = refresh_runtime_config()
            self._build_config_code_map()
            text = f"Runtime configuration refreshed from MongoDB.\n\nRuntime overrides: {status.get('override_count', 0)}"
        except Exception as ex:
            logger.exception("Runtime configuration refresh failed.")
            text = f"Runtime configuration refresh failed.\n\nError: {type(ex).__name__}: {ex}"
        keyboard = {"inline_keyboard": [[{"text": "Config Home", "callback_data": "cfg:home"}]]}
        if message_id:
            self._edit_bot_message(chat_id, message_id, text, reply_markup=keyboard)
        else:
            self._send_bot_message(chat_id, text, reply_markup=keyboard)

    def _answer_invalid_config(self, chat_id, message):
        self._send_bot_message(chat_id, message)

    def _handle_help_command(self):
        text = "Available commands:\n\n/save_token\nSave a new Upstox access token.\n\n/token_status\nShow masked token document status.\n\n/token_check\nValidate the current token.\n\n/profile\nGet the current Upstox profile response.\n\n/funds\nGet Upstox funds and margin."
        if self.runtime_config_enabled:
            text += "\n\n/config\nOpen runtime configuration.\n\n/config_status\nShow runtime configuration status.\n\n/config_refresh\nReload runtime values from MongoDB."
        self._send_message(text, level="INFO")

    def _handle_raw_token_message(self, chat_id, message_id, text: str):
        raw_token = str(text or "").strip()
        with self._state_lock:
            self._pending_save_token_chats.discard(str(chat_id))
        deleted = self.delete_message(chat_id=chat_id, message_id=message_id)
        if not raw_token:
            self._send_message("Token message was empty. Use /save_token and try again.", level="WARNING")
            return
        if len(raw_token) < 50:
            self._send_message("Token looks too short and was not saved.\n\nUse /save_token and send the complete Upstox access token.", level="WARNING")
            return
        is_valid, profile, error = upstox_token_client.validate_token(raw_token)
        if not is_valid:
            self._send_message(f"The token was not saved because Upstox profile validation failed.\n\nError: {error}\n\nToken message deleted: {deleted}", level="ERROR")
            return
        token_store.save_access_token(access_token=raw_token, source="telegram")
        token_store.update_validation_status(is_valid=True, status="saved_and_profile_validated", error=None, profile=profile)
        self._send_message("New Upstox access token saved successfully.", level="SUCCESS")

    def handle_message(self, message: dict):
        self._cleanup_expired_config_sessions()
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        text = str(message.get("text") or "").strip()
        if not chat_id or not message_id:
            return
        if not self._is_authorized_chat(chat_id):
            logger.warning("Unauthorized Telegram chat attempted bot use: %s", chat_id)
            return
        if not text:
            return
        with self._state_lock:
            pending_token = str(chat_id) in self._pending_save_token_chats
            pending_config = deepcopy_value(self._pending_config_inputs.get(str(chat_id)))
        if pending_token:
            self._handle_raw_token_message(chat_id=chat_id, message_id=message_id, text=text)
            return
        if pending_config:
            self._handle_custom_config_message(message, pending_config)
            return
        command = self._normalize_command(text)
        if command == "/save_token":
            self._handle_save_token_command(chat_id)
            return
        if command in {"/token-status", "/token_status"}:
            self._handle_token_status_command()
            return
        if command in {"/token-check", "/token_check"}:
            self._handle_token_status_validate_command()
            return
        if command == "/profile":
            self._handle_profile_command()
            return
        if command == "/funds":
            self._handle_funds_command()
            return
        if command == "/config":
            self._handle_config_command(chat_id)
            return
        if command in {"/config-status", "/config_status"}:
            self._handle_config_status(chat_id)
            return
        if command in {"/config-refresh", "/config_refresh"}:
            self._handle_config_refresh(chat_id)
            return
        if command == "/cancel":
            with self._state_lock:
                self._pending_save_token_chats.discard(str(chat_id))
                self._pending_config_inputs.pop(str(chat_id), None)
            self._send_bot_message(chat_id, "Pending operation cancelled.")
            return
        if command in {"/start", "/help"}:
            self._handle_help_command()

    def handle_callback_query(self, callback_query: dict):
        self._cleanup_expired_config_sessions()
        callback_query_id = callback_query.get("id")
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        data = str(callback_query.get("data") or "").strip()
        if not callback_query_id:
            return
        if not chat_id or not message_id:
            self._answer_callback_query(callback_query_id, "Invalid callback message.", show_alert=True)
            return
        if not self._is_authorized_chat(chat_id):
            logger.warning("Unauthorized Telegram callback. chat_id=%s", chat_id)
            self._answer_callback_query(callback_query_id, "Unauthorized.", show_alert=True)
            return
        if not self.runtime_config_enabled:
            self._answer_callback_query(callback_query_id, "Runtime configuration is disabled.", show_alert=True)
            return
        self._answer_callback_query(callback_query_id)
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if data == "cfg:home":
            self._show_config_categories(chat_id, message_id)
            return
        if data == "cfg:close":
            with self._state_lock:
                self._pending_config_inputs.pop(str(chat_id), None)
            self._edit_bot_message(chat_id, message_id, "Runtime configuration menu closed.", reply_markup={"inline_keyboard": []})
            return
        if data == "cfg:status":
            self._handle_config_status(chat_id, message_id)
            return
        if data == "cfg:refresh":
            self._handle_config_refresh(chat_id, message_id)
            return
        if action == "cat" and len(parts) >= 3:
            self._show_config_category(chat_id, message_id, parts[2])
            return
        if action == "item" and len(parts) >= 3:
            with self._state_lock:
                self._pending_config_inputs.pop(str(chat_id), None)
            self._show_config_item(chat_id, message_id, parts[2])
            return
        if action == "val" and len(parts) >= 4:
            config_code = parts[2]
            try:
                value_index = int(parts[3])
            except ValueError:
                self._send_bot_message(chat_id, "Invalid value selection.")
                return
            name = self._config_code_to_name.get(config_code)
            if not name:
                self._send_bot_message(chat_id, "Configuration mapping expired. Open /config again.")
                return
            try:
                metadata = get_runtime_config_metadata(name)
                suggested_values = metadata.get("suggested_values") or []
                selected_value = suggested_values[value_index]
            except (RuntimeConfigError, IndexError) as ex:
                self._send_bot_message(chat_id, f"Unable to select value: {ex}")
                return
            self._prepare_config_value(chat_id=chat_id, message_id=message_id, callback_query=callback_query, config_code=config_code, raw_value=selected_value)
            return
        if action == "custom" and len(parts) >= 3:
            self._handle_custom_config_request(chat_id=chat_id, message_id=message_id, config_code=parts[2], callback_query=callback_query)
            return
        if action == "confirm" and len(parts) >= 3:
            config_code = parts[2]
            confirmation_key = f"{chat_id}:{config_code}"
            with self._state_lock:
                confirmation = deepcopy_value(self._pending_config_confirmations.pop(confirmation_key, None))
            if not confirmation:
                self._send_bot_message(chat_id, "Confirmation expired. Open /config and try again.")
                return
            self._apply_config_change(chat_id=chat_id, message_id=message_id, config_code=config_code, confirmation=confirmation)
            return
        if action == "reset" and len(parts) >= 3:
            self._handle_config_reset(chat_id=chat_id, message_id=message_id, config_code=parts[2], callback_query=callback_query)
            return
        self._send_bot_message(chat_id, "Unsupported configuration action.")

    def run_loop(self):
        logger.info("Telegram token bot polling loop started.")
        if self.runtime_config_enabled:
            try:
                initialize_runtime_config()
                self._build_config_code_map()
            except Exception as ex:
                logger.exception("Runtime configuration startup initialization failed: %s", ex)
        self.set_commands()
        startup_commands = "Telegram bot started.\n\nCommands:\n/save_token\n/token_status\n/profile\n/funds\n/token_check"
        if self.runtime_config_enabled:
            startup_commands += "\n/config\n/config_status\n/config_refresh"
        self._send_message(startup_commands, level="INFO")
        while not self._stop_event.is_set():
            try:
                updates = self.get_updates()
                for update in updates:
                    callback_query = update.get("callback_query")
                    if callback_query:
                        self.handle_callback_query(callback_query)
                        continue
                    message = update.get("message")
                    if message:
                        self.handle_message(message)
            except Exception as ex:
                logger.exception("Telegram bot loop error: %s: %s", type(ex).__name__, ex)
                self._stop_event.wait(self.poll_seconds)
        logger.info("Telegram token bot polling loop stopped.")

    def start(self) -> bool:
        if not self.enabled:
            logger.info("Telegram token bot is disabled or not configured. Check TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and TELEGRAM_TOKEN_BOT_ENABLED.")
            return False
        if self._thread and self._thread.is_alive():
            logger.info("Telegram token bot is already running.")
            return True
        self._stop_event.clear()
        self._thread = Thread(target=self.run_loop, name="telegram-token-bot", daemon=True)
        self._thread.start()
        logger.info("Telegram token bot started.")
        return True

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.long_poll_timeout + 5)
        logger.info("Telegram token bot stop requested.")


def deepcopy_value(value):
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return value


telegram_token_bot = TelegramTokenBot()