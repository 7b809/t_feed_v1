"""
services/notification_service.py

Centralized notification service for EMA events.

Responsibilities:
    - EMA crossover notifications
    - Live processing lifecycle notifications
    - Hard refresh notifications
    - Error notifications
    - Duplicate notification prevention
    - Safe Telegram delivery
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Optional

from core import config

logger = logging.getLogger(__name__)


class NotificationService:
    """
    Centralized notification service.

    The service is intentionally defensive:
        - Notification failures do not stop EMA processing.
        - Configuration flags can disable individual notification types.
        - Duplicate events can be suppressed using event identifiers.
    """

    def __init__(self, telegram_service: Optional[Any] = None) -> None:
        self.enabled = self._get_bool_setting(
            "TELEGRAM_ENABLED",
            default=False,
        )

        self.crossover_enabled = self._get_bool_setting(
            "EMA_TELEGRAM_CROSSOVER_ENABLED",
            default=True,
        )

        self.lifecycle_enabled = self._get_bool_setting(
            "EMA_TELEGRAM_LIFECYCLE_ENABLED",
            default=True,
        )

        self.error_enabled = self._get_bool_setting(
            "EMA_TELEGRAM_ERROR_ENABLED",
            default=True,
        )

        self.notify_live_start = self._get_bool_setting(
            "EMA_NOTIFY_LIVE_START",
            default=True,
        )

        self.notify_hard_refresh = self._get_bool_setting(
            "EMA_NOTIFY_HARD_REFRESH",
            default=True,
        )

        self.notify_hard_refresh_errors = self._get_bool_setting(
            "EMA_NOTIFY_HARD_REFRESH_ERRORS",
            default=True,
        )

        self._telegram_service = telegram_service
        self._sent_event_ids: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_bool_setting(name: str, default: bool) -> bool:
        """
        Safely read a boolean configuration value.

        Supports configuration values that may not exist yet.
        """
        return bool(getattr(config, name, default))

    # ------------------------------------------------------------------
    # Telegram service resolution
    # ------------------------------------------------------------------

    def _get_telegram_service(self) -> Optional[Any]:
        """
        Resolve the Telegram service lazily.

        A service can also be injected through the constructor, which
        makes testing easier and avoids hard dependencies.
        """
        if self._telegram_service is not None:
            return self._telegram_service

        try:
            from services.telegram_service import TelegramService

            self._telegram_service = TelegramService()
            return self._telegram_service

        except ImportError:
            logger.warning(
                "TelegramService import failed. "
                "Telegram notifications are unavailable."
            )
            return None

        except Exception:
            logger.exception("Failed to initialize TelegramService.")
            return None

    # ------------------------------------------------------------------
    # Generic sending
    # ------------------------------------------------------------------

    def is_enabled(self) -> bool:
        """Return whether Telegram notifications are enabled."""
        return self.enabled

    def send(
        self,
        message: str,
        *,
        silent: bool = False,
    ) -> bool:
        """
        Send a Telegram message safely.

        Returns:
            True: message was sent successfully.
            False: sending was disabled or failed.
        """
        if not self.enabled:
            logger.debug("Telegram notifications are disabled.")
            return False

        if not message or not message.strip():
            logger.warning("Skipped empty Telegram notification.")
            return False

        telegram_service = self._get_telegram_service()

        if telegram_service is None:
            return False

        try:
            result = telegram_service.send(
                message=message,
                silent=silent,
            )

            if result is False:
                logger.warning("TelegramService returned failure.")
                return False

            return True

        except TypeError:
            """
            Compatibility fallback for TelegramService implementations
            that accept positional arguments only or do not support
            the silent keyword.
            """
            try:
                result = telegram_service.send(message)

                if result is False:
                    logger.warning("TelegramService returned failure.")
                    return False

                return True

            except Exception:
                logger.exception("Telegram notification failed.")
                return False

        except Exception:
            logger.exception("Unexpected Telegram notification error.")
            return False

    # ------------------------------------------------------------------
    # Duplicate prevention
    # ------------------------------------------------------------------

    def _is_duplicate(self, event_id: Optional[str]) -> bool:
        """
        Check and register an event identifier.

        Returns:
            True if the event was already processed.
            False if this is a new event.
        """
        if not event_id:
            return False

        with self._lock:
            if event_id in self._sent_event_ids:
                return True

            self._sent_event_ids.add(event_id)

        return False

    def clear_event_cache(self) -> None:
        """Clear the in-memory duplicate event cache."""
        with self._lock:
            self._sent_event_ids.clear()

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_value(
        value: Any,
        default: str = "-",
    ) -> str:
        """Convert a value into a display-safe string."""
        if value is None:
            return default

        if isinstance(value, float):
            return f"{value:.4f}"

        return str(value)

    @staticmethod
    def _format_timestamp(value: Any) -> str:
        """
        Format a timestamp for Telegram.

        Supports datetime and string values.
        """
        if value is None:
            return "-"

        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")

        return str(value)

    @classmethod
    def _get_event_value(
        cls,
        event: Any,
        name: str,
        default: Any = None,
    ) -> Any:
        """Read a field from a dataclass or dictionary event."""
        if event is None:
            return default

        if isinstance(event, dict):
            return event.get(name, default)

        return getattr(event, name, default)

    @classmethod
    def _instrument_label(cls, event: Any) -> str:
        """
        Build a readable instrument label.

        Preference:
            trading_symbol -> instrument_key -> underlying.
        """
        trading_symbol = cls._get_event_value(
            event,
            "trading_symbol",
        )

        instrument_key = cls._get_event_value(
            event,
            "instrument_key",
        )

        underlying = cls._get_event_value(
            event,
            "underlying",
        )

        return str(trading_symbol or instrument_key or underlying or "UNKNOWN")

    # ------------------------------------------------------------------
    # EMA crossover notification
    # ------------------------------------------------------------------

    def notify_crossover(
        self,
        event: Any,
        *,
        event_id: Optional[str] = None,
        silent: bool = False,
    ) -> bool:
        """
        Send an EMA crossover notification.

        Expected event fields:
            instrument_key
            trading_symbol
            timestamp
            close
            ema_9
            ema_21
            ema_difference
            previous_ema_difference
            cross_type
            candle_timestamp
            source
        """
        if not self.crossover_enabled:
            return False

        if event_id and self._is_duplicate(event_id):
            logger.debug(
                "Duplicate crossover notification skipped: %s",
                event_id,
            )
            return False

        cross_type = self._get_event_value(
            event,
            "cross_type",
            "UNKNOWN",
        )

        instrument = self._instrument_label(event)

        timestamp = self._format_timestamp(
            self._get_event_value(
                event,
                "candle_timestamp",
                self._get_event_value(event, "timestamp"),
            )
        )

        close = self._format_value(self._get_event_value(event, "close"))

        ema_9 = self._format_value(self._get_event_value(event, "ema_9"))

        ema_21 = self._format_value(self._get_event_value(event, "ema_21"))

        difference = self._format_value(self._get_event_value(event, "ema_difference"))

        previous_difference = self._format_value(
            self._get_event_value(
                event,
                "previous_ema_difference",
            )
        )

        source = self._get_event_value(
            event,
            "source",
            "unknown",
        )

        message = (
            "📊 *EMA CROSSOVER DETECTED*\n"
            "\n"
            f"📌 *Instrument:* `{instrument}`\n"
            f"🔔 *Signal:* `{cross_type}`\n"
            f"🕒 *Candle Time:* `{timestamp}`\n"
            f"💰 *Close:* `{close}`\n"
            "\n"
            f"📈 *EMA 9:* `{ema_9}`\n"
            f"📉 *EMA 21:* `{ema_21}`\n"
            f"➗ *Difference:* `{difference}`\n"
            f"↩️ *Previous Difference:* `{previous_difference}`\n"
            f"⚙️ *Source:* `{source}`"
        )

        logger.info(
            "Sending EMA crossover notification: " "instrument=%s cross_type=%s",
            instrument,
            cross_type,
        )

        return self.send(
            message,
            silent=silent,
        )

    # ------------------------------------------------------------------
    # Lifecycle notifications
    # ------------------------------------------------------------------

    def notify_live_started(
        self,
        *,
        instrument_count: Optional[int] = None,
        message: Optional[str] = None,
    ) -> bool:
        """Notify that live EMA processing has started."""
        if not self.lifecycle_enabled:
            return False

        if not self.notify_live_start:
            return False

        if message:
            notification = message
        else:
            count_text = str(instrument_count) if instrument_count is not None else "-"

            notification = (
                "🟢 *EMA LIVE PROCESSING STARTED*\n"
                "\n"
                f"📌 *Instruments:* `{count_text}`\n"
                "⏱️ *Mode:* Completed one-minute candles"
            )

        logger.info("Sending live processing started notification.")

        return self.send(notification)

    def notify_live_stopped(
        self,
        *,
        instrument_count: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> bool:
        """Notify that live EMA processing has stopped."""
        if not self.lifecycle_enabled:
            return False

        count_text = str(instrument_count) if instrument_count is not None else "-"

        reason_text = reason or "Not specified"

        message = (
            "🔴 *EMA LIVE PROCESSING STOPPED*\n"
            "\n"
            f"📌 *Instruments:* `{count_text}`\n"
            f"📝 *Reason:* `{reason_text}`"
        )

        logger.info("Sending live processing stopped notification.")

        return self.send(message)

    def notify_lifecycle(
        self,
        event_name: str,
        *,
        message: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Send a generic lifecycle notification."""
        if not self.lifecycle_enabled:
            return False

        if message:
            notification = message
        else:
            lines = [
                "ℹ️ *EMA LIFECYCLE EVENT*",
                "",
                f"📌 *Event:* `{event_name}`",
            ]

            if details:
                for key, value in details.items():
                    label = str(key).replace("_", " ").title()
                    lines.append(f"• *{label}:* `{self._format_value(value)}`")

            notification = "\n".join(lines)

        logger.info(
            "Sending lifecycle notification: %s",
            event_name,
        )

        return self.send(notification)

    # ------------------------------------------------------------------
    # Hard refresh notifications
    # ------------------------------------------------------------------

    def notify_hard_refresh_started(
        self,
        *,
        job_id: Optional[str] = None,
    ) -> bool:
        """Notify that a hard refresh has started."""
        if not self.lifecycle_enabled:
            return False

        if not self.notify_hard_refresh:
            return False

        job_text = job_id or "-"

        message = (
            "🔄 *EMA HARD REFRESH STARTED*\n"
            "\n"
            f"🆔 *Job ID:* `{job_text}`\n"
            "⏳ Historical data refresh is in progress."
        )

        return self.send(message)

    def notify_hard_refresh_completed(
        self,
        *,
        job_id: Optional[str] = None,
        instrument_count: Optional[int] = None,
        duration_seconds: Optional[float] = None,
    ) -> bool:
        """Notify that a hard refresh completed successfully."""
        if not self.lifecycle_enabled:
            return False

        if not self.notify_hard_refresh:
            return False

        job_text = job_id or "-"
        count_text = str(instrument_count) if instrument_count is not None else "-"

        duration_text = (
            f"{duration_seconds:.2f}" if duration_seconds is not None else "-"
        )

        message = (
            "✅ *EMA HARD REFRESH COMPLETED*\n"
            "\n"
            f"🆔 *Job ID:* `{job_text}`\n"
            f"📌 *Instruments:* `{count_text}`\n"
            f"⏱️ *Duration:* `{duration_text} seconds`"
        )

        return self.send(message)

    def notify_hard_refresh_failed(
        self,
        *,
        job_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> bool:
        """Notify that a hard refresh failed."""
        if not self.error_enabled:
            return False

        if not self.notify_hard_refresh_errors:
            return False

        job_text = job_id or "-"
        error_text = error or "Unknown error"

        message = (
            "❌ *EMA HARD REFRESH FAILED*\n"
            "\n"
            f"🆔 *Job ID:* `{job_text}`\n"
            f"⚠️ *Error:* `{error_text}`"
        )

        logger.error(
            "Hard refresh failed. job_id=%s error=%s",
            job_text,
            error_text,
        )

        return self.send(message)

    # ------------------------------------------------------------------
    # Error notifications
    # ------------------------------------------------------------------

    def notify_error(
        self,
        error: Any,
        *,
        context: Optional[str] = None,
        instrument_key: Optional[str] = None,
        event_id: Optional[str] = None,
        silent: bool = False,
    ) -> bool:
        """
        Send an error notification.

        Error messages are truncated to prevent excessively large
        Telegram messages.
        """
        if not self.error_enabled:
            return False

        if event_id and self._is_duplicate(event_id):
            logger.debug(
                "Duplicate error notification skipped: %s",
                event_id,
            )
            return False

        error_text = str(error or "Unknown error")

        if len(error_text) > 2000:
            error_text = error_text[:2000] + "..."

        context_text = context or "EMA service"
        instrument_text = instrument_key or "-"

        message = (
            "🚨 *EMA SERVICE ERROR*\n"
            "\n"
            f"📌 *Context:* `{context_text}`\n"
            f"📊 *Instrument:* `{instrument_text}`\n"
            f"⚠️ *Error:* `{error_text}`"
        )

        logger.error(
            "Sending EMA error notification. " "context=%s instrument=%s error=%s",
            context_text,
            instrument_text,
            error_text,
        )

        return self.send(
            message,
            silent=silent,
        )

    # ------------------------------------------------------------------
    # Event dispatcher
    # ------------------------------------------------------------------

    def notify_event(
        self,
        event: Any,
        *,
        event_id: Optional[str] = None,
    ) -> bool:
        """
        Dispatch an EmaEvent to the appropriate notification method.

        Supported event values:
            crossover
            live_started
            live_stopped
            lifecycle
            error
            hard_refresh_started
            hard_refresh_completed
            hard_refresh_failed
        """
        event_name = self._get_event_value(
            event,
            "event",
            "",
        )

        normalized_event = str(event_name).strip().lower()

        if normalized_event in {
            "crossover",
            "ema_crossover",
            "cross",
        }:
            return self.notify_crossover(
                event,
                event_id=event_id,
            )

        if normalized_event in {
            "live_started",
            "live_start",
        }:
            return self.notify_live_started(
                instrument_count=self._get_event_value(
                    event,
                    "instrument_count",
                ),
                message=self._get_event_value(
                    event,
                    "message",
                ),
            )

        if normalized_event in {
            "live_stopped",
            "live_stop",
        }:
            return self.notify_live_stopped(
                instrument_count=self._get_event_value(
                    event,
                    "instrument_count",
                ),
                reason=self._get_event_value(
                    event,
                    "message",
                ),
            )

        if normalized_event in {
            "lifecycle",
            "service_lifecycle",
        }:
            return self.notify_lifecycle(
                event_name=normalized_event,
                message=self._get_event_value(
                    event,
                    "message",
                ),
            )

        if normalized_event in {
            "error",
            "service_error",
        }:
            return self.notify_error(
                error=self._get_event_value(
                    event,
                    "error",
                    self._get_event_value(event, "message"),
                ),
                context=self._get_event_value(
                    event,
                    "context",
                ),
                instrument_key=self._get_event_value(
                    event,
                    "instrument_key",
                ),
                event_id=event_id,
            )

        if normalized_event == "hard_refresh_started":
            return self.notify_hard_refresh_started(
                job_id=self._get_event_value(
                    event,
                    "job_id",
                ),
            )

        if normalized_event == "hard_refresh_completed":
            return self.notify_hard_refresh_completed(
                job_id=self._get_event_value(
                    event,
                    "job_id",
                ),
                instrument_count=self._get_event_value(
                    event,
                    "instrument_count",
                ),
                duration_seconds=self._get_event_value(
                    event,
                    "duration_seconds",
                ),
            )

        if normalized_event == "hard_refresh_failed":
            return self.notify_hard_refresh_failed(
                job_id=self._get_event_value(
                    event,
                    "job_id",
                ),
                error=self._get_event_value(
                    event,
                    "error",
                    self._get_event_value(event, "message"),
                ),
            )

        logger.debug(
            "No notification handler for event: %s",
            normalized_event,
        )

        return False


# Shared service instance.
notification_service = NotificationService()
