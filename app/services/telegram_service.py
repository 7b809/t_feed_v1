# app/services/telegram_service.py

import json
import logging
import os
import traceback
import urllib.parse
import urllib.request
from datetime import datetime
from html import escape

logger = logging.getLogger("uvicorn")


class TelegramService:
    """
    Telegram notification service.

    Used for sending:
    - Success messages
    - Info messages
    - Error messages
    - Exception alerts
    - Market feed connections & first tick verification proofs

    Environment variables required:

    TELEGRAM_BOT_TOKEN=your_bot_token
    TELEGRAM_CHAT_ID=your_chat_id
    ENABLE_TELEGRAM_ALERTS=true
    TELEGRAM_TIMEOUT_SECONDS=10
    """

    def __init__(self):
        self.bot_token = os.getenv(
            "TELEGRAM_BOT_TOKEN",
            "",
        ).strip()

        self.chat_id = os.getenv(
            "TELEGRAM_CHAT_ID",
            "",
        ).strip()

        self.enabled = (
            os.getenv(
                "ENABLE_TELEGRAM_ALERTS",
                "false",
            )
            .strip()
            .lower()
            == "true"
        )

        self.timeout = int(
            os.getenv(
                "TELEGRAM_TIMEOUT_SECONDS",
                "10",
            )
        )

    # --------------------------------------------------
    # Internal Helpers
    # --------------------------------------------------

    def is_configured(self):
        """
        Returns True only when Telegram alerts are enabled
        and bot token/chat id are available.
        """

        return bool(self.enabled and self.bot_token and self.chat_id)

    @staticmethod
    def _now():
        """
        Current timestamp string.
        """

        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _safe_text(value):
        """
        Escape text for Telegram HTML mode.
        """

        if value is None:
            return ""

        return escape(str(value))

    def _build_message(
        self,
        level,
        title,
        message,
        details=None,
    ):
        """
        Build Telegram formatted HTML message.
        """

        icon_map = {
            "success": "✅",
            "info": "ℹ️",
            "warning": "⚠️",
            "error": "❌",
        }

        icon = icon_map.get(
            level,
            "ℹ️",
        )

        text = (
            f"{icon} <b>{self._safe_text(title)}</b>\n\n"
            f"{self._safe_text(message)}\n\n"
            f"<b>Time:</b> {self._safe_text(self._now())}"
        )

        if details:
            text += "\n\n" "<b>Details:</b>\n" f"<pre>{self._safe_text(details)}</pre>"

        return text

    def _send_raw_message(
        self,
        text,
    ):
        """
        Sends raw Telegram message using Telegram Bot API.
        """

        if not self.is_configured():
            logger.debug("Telegram alerts disabled or not configured.")
            return False

        try:
            url = f"https://api.telegram.org/bot" f"{self.bot_token}/sendMessage"

            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }

            data = urllib.parse.urlencode(payload).encode("utf-8")

            request = urllib.request.Request(
                url=url,
                data=data,
                method="POST",
            )

            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
            ) as response:

                response_body = response.read().decode("utf-8")

                result = json.loads(response_body)

                if result.get("ok") is True:
                    return True

                logger.error(f"Telegram API returned error: {result}")

                return False

        except Exception as ex:
            logger.error(f"Failed to send Telegram message: {ex}")

            return False

    # --------------------------------------------------
    # Public Methods
    # --------------------------------------------------

    def send_success(
        self,
        title,
        message,
        details=None,
    ):
        """
        Send success message.
        """

        text = self._build_message(
            level="success",
            title=title,
            message=message,
            details=details,
        )

        return self._send_raw_message(text)

    def send_info(
        self,
        title,
        message,
        details=None,
    ):
        """
        Send info message.
        """

        text = self._build_message(
            level="info",
            title=title,
            message=message,
            details=details,
        )

        return self._send_raw_message(text)

    def send_warning(
        self,
        title,
        message,
        details=None,
    ):
        """
        Send warning message.
        """

        text = self._build_message(
            level="warning",
            title=title,
            message=message,
            details=details,
        )

        return self._send_raw_message(text)

    def send_error(
        self,
        title,
        message,
        details=None,
    ):
        """
        Send error message.
        """

        text = self._build_message(
            level="error",
            title=title,
            message=message,
            details=details,
        )

        return self._send_raw_message(text)

    def send_exception(
        self,
        title,
        exception,
        message="Exception occurred",
    ):
        """
        Send exception traceback.
        """

        details = "".join(
            traceback.format_exception(
                type(exception),
                exception,
                exception.__traceback__,
            )
        )

        return self.send_error(
            title=title,
            message=message,
            details=details,
        )

    # --------------------------------------------------
    # Specialized Market Feed Notification Helpers
    # --------------------------------------------------

    def send_feed_connected(self, total_subscribed_count: int, details: str = None):
        """
        Sends an alert confirming that the Upstox market feed successfully
        connected and subscribed to the target instruments.
        """
        title = "Upstox Feed Connected"
        message = f"Successfully established WebSocket connection and subscribed to <b>{total_subscribed_count}</b> instruments/strikes."
        return self.send_success(title=title, message=message, details=details)

    def send_first_tick_proof(self, instrument_key: str, tick_data: dict):
        """
        Sends proof alert containing the first incoming tick data for the main Nifty/strike feed.
        """
        title = "First Tick Received (Connection Proof)"
        message = f"Successfully received the first market tick for instrument: <b>{self._safe_text(instrument_key)}</b>"

        # Pretty print tick details dictionary
        formatted_details = (
            json.dumps(tick_data, indent=2)
            if isinstance(tick_data, dict)
            else str(tick_data)
        )

        return self.send_info(title=title, message=message, details=formatted_details)


telegram_service = TelegramService()
