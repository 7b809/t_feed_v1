import html
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


class TelegramService:
    """
    Telegram notification service.

    Used for sending project lifecycle, scheduler, token, instrument,
    subscription, refresh, and error notifications to a Telegram chat.
    """

    def __init__(self):
        self.bot_token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.enabled = config.TELEGRAM_ENABLED

        self.timeout_seconds = int(getattr(config, "TELEGRAM_TIMEOUT_SECONDS", 10))

        self.market_timezone = self._load_market_timezone()
        self.market_time_format = getattr(
            config,
            "MARKET_TIME_FORMAT",
            "%Y-%m-%d %H:%M:%S %Z",
        )

        self.api_url = (
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            if self.bot_token
            else None
        )

    def _load_market_timezone(self):
        """Loads market timezone from config, defaulting to Asia/Kolkata."""
        timezone_name = getattr(config, "MARKET_TIMEZONE", "Asia/Kolkata")

        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            logger.error(
                f"Invalid MARKET_TIMEZONE configured: {timezone_name}. "
                "Falling back to Asia/Kolkata."
            )
            return ZoneInfo("Asia/Kolkata")

    def _now_market_time(self) -> str:
        """Returns current market time as formatted string."""
        return datetime.now(self.market_timezone).strftime(self.market_time_format)

    def is_configured(self) -> bool:
        """Returns True if Telegram service has required configuration."""
        return bool(self.enabled and self.bot_token and self.chat_id and self.api_url)

    def _escape(self, value) -> str:
        """Escapes text for Telegram HTML parse mode."""
        return html.escape(str(value), quote=False)

    def _send_raw_message(self, message: str) -> bool:
        """
        Sends raw HTML message to Telegram.

        Returns:
            True if message was sent successfully, False otherwise.
        """
        if not self.is_configured():
            logger.warning(
                "Telegram notification skipped. "
                "Service is disabled or bot token/chat id is missing."
            )
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            response = requests.post(
                self.api_url,
                json=payload,
                timeout=self.timeout_seconds,
            )

            if response.status_code != 200:
                logger.error(
                    f"Telegram send failed. "
                    f"status_code={response.status_code}, response={response.text}"
                )
                return False

            logger.info("Telegram notification sent successfully.")
            return True

        except Exception as ex:
            logger.error(f"Telegram send exception: {type(ex).__name__}: {ex}")
            return False

    def send_message(
        self,
        title: str,
        message: str,
        level: str = "INFO",
    ) -> bool:
        """
        Sends a formatted Telegram notification.

        Args:
            title: Notification title.
            message: Notification body.
            level: INFO, SUCCESS, WARNING, ERROR, STARTUP, REFRESH, SUBSCRIPTION.
        """
        level_upper = str(level or "INFO").upper()

        emoji_map = {
            "INFO": "ℹ️",
            "SUCCESS": "✅",
            "WARNING": "⚠️",
            "ERROR": "❌",
            "STARTUP": "🚀",
            "REFRESH": "🔄",
            "SUBSCRIPTION": "📡",
            "TOKEN": "🔐",
            "INSTRUMENTS": "📊",
            "SHUTDOWN": "🛑",
        }

        emoji = emoji_map.get(level_upper, "ℹ️")

        safe_title = self._escape(title)
        safe_message = self._escape(message)
        market_time = self._escape(self._now_market_time())

        formatted_message = (
            f"{emoji} <b>{safe_title}</b>\n\n"
            f"{safe_message}\n\n"
            f"<b>Level:</b> {self._escape(level_upper)}\n"
            f"<b>Time:</b> {market_time}"
        )

        return self._send_raw_message(formatted_message)

    def send_startup_message(self, status: str, details: str = "") -> bool:
        """Sends application startup notification."""
        message = f"Application startup status: {status}"

        if details:
            message += f"\n\n{details}"

        return self.send_message(
            title="Option Feed Engine Startup",
            message=message,
            level="STARTUP",
        )

    def send_shutdown_message(self, details: str = "") -> bool:
        """Sends application shutdown notification."""
        message = "Application shutdown sequence executed."

        if details:
            message += f"\n\n{details}"

        return self.send_message(
            title="Option Feed Engine Shutdown",
            message=message,
            level="SHUTDOWN",
        )

    def send_token_refresh_message(
        self,
        success: bool,
        updated_at=None,
        error: str = "",
    ) -> bool:
        """Sends token refresh notification."""
        if success:
            message = "Access token document refreshed successfully from MongoDB."

            if updated_at:
                message += f"\nToken Updated At: {updated_at}"

            return self.send_message(
                title="Token Refresh Successful",
                message=message,
                level="TOKEN",
            )

        message = "Access token refresh failed."

        if error:
            message += f"\nError: {error}"

        return self.send_message(
            title="Token Refresh Failed",
            message=message,
            level="ERROR",
        )

    def send_instruments_fetched_message(
        self,
        success: bool,
        nearest_expiry=None,
        total_contracts=0,
        subscribed_keys_count=0,
        strike_from=None,
        strike_to=None,
        error: str = "",
    ) -> bool:
        """Sends option instruments fetch notification."""
        if success:
            message = (
                "Option instruments fetched and cache updated successfully.\n\n"
                f"Nearest Expiry: {nearest_expiry}\n"
                f"Total Contracts: {total_contracts}\n"
                f"Subscribed Keys: {subscribed_keys_count}\n"
                f"Strike Range: {strike_from} to {strike_to}"
            )

            return self.send_message(
                title="Instruments Fetch Successful",
                message=message,
                level="INSTRUMENTS",
            )

        message = "Option instruments fetch failed."

        if error:
            message += f"\nError: {error}"

        return self.send_message(
            title="Instruments Fetch Failed",
            message=message,
            level="ERROR",
        )

    def send_subscription_message(
        self,
        success: bool,
        subscribed_keys_count=0,
        feed_mode=None,
        error: str = "",
    ) -> bool:
        """Sends Upstox subscription or streamer restart notification."""
        if success:
            message = (
                "Upstox streamer subscription is active.\n\n"
                f"Subscribed Instruments: {subscribed_keys_count}\n"
                f"Feed Mode: {feed_mode}"
            )

            return self.send_message(
                title="Feed Subscription Successful",
                message=message,
                level="SUBSCRIPTION",
            )

        message = "Upstox feed subscription failed."

        if error:
            message += f"\nError: {error}"

        return self.send_message(
            title="Feed Subscription Failed",
            message=message,
            level="ERROR",
        )

    def send_daily_refresh_message(
        self,
        success: bool,
        subscribed_keys_count=0,
        nearest_expiry=None,
        error: str = "",
    ) -> bool:
        """Sends daily hard refresh notification."""
        if success:
            message = (
                "Daily market hard refresh completed successfully.\n\n"
                f"Nearest Expiry: {nearest_expiry}\n"
                f"Subscribed Instruments: {subscribed_keys_count}"
            )

            return self.send_message(
                title="Daily Market Hard Refresh Successful",
                message=message,
                level="REFRESH",
            )

        message = "Daily market hard refresh failed."

        if error:
            message += f"\nError: {error}"

        return self.send_message(
            title="Daily Market Hard Refresh Failed",
            message=message,
            level="ERROR",
        )

    def send_exception_message(
        self,
        title: str,
        exception: Exception,
        context: str = "",
    ) -> bool:
        """Sends exception notification."""
        message = (
            f"Exception Type: {type(exception).__name__}\n"
            f"Exception Message: {exception}"
        )

        if context:
            message = f"Context: {context}\n\n{message}"

        return self.send_message(
            title=title,
            message=message,
            level="ERROR",
        )


telegram_service = TelegramService()
