from __future__ import annotations

import logging
import threading
from datetime import date, datetime
from typing import Any, Optional

from core import config

logger = logging.getLogger(__name__)


class NotificationService:
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

    @staticmethod
    def _get_bool_setting(name: str, default: bool) -> bool:
        value = getattr(config, name, default)

        if isinstance(value, bool):
            return value

        if isinstance(value, str):
            return value.strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
                "enabled",
            }

        return bool(value)

    def _get_telegram_service(self):
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
            logger.exception("Failed to initialize TelegramService")
            return None

    def is_enabled(self) -> bool:
        return self.enabled

    def send(
        self,
        message: str,
        *,
        silent: bool = False,
    ) -> bool:
        if not self.enabled:
            logger.debug("Telegram notifications are disabled")
            return False

        if not message or not message.strip():
            logger.warning("Skipped empty Telegram notification")
            return False

        telegram_service = self._get_telegram_service()

        if telegram_service is None:
            logger.warning("Telegram notification skipped because service is unavailable")
            return False

        try:
            result = telegram_service.send(
                message=message,
                silent=silent,
            )

            if result is False:
                logger.warning("TelegramService returned failure")
                return False

            return True

        except TypeError:
            try:
                result = telegram_service.send(message)

                if result is False:
                    logger.warning("TelegramService returned failure")
                    return False

                return True

            except Exception:
                logger.exception("Telegram notification failed")
                return False

        except Exception:
            logger.exception("Unexpected Telegram notification error")
            return False

    def _is_duplicate(self, event_id: Optional[str]) -> bool:
        if not event_id:
            return False

        with self._lock:
            if event_id in self._sent_event_ids:
                return True

            self._sent_event_ids.add(event_id)

        return False

    def clear_event_cache(self) -> None:
        with self._lock:
            self._sent_event_ids.clear()

    @staticmethod
    def _format_value(
        value: Any,
        default: str = "-",
    ) -> str:
        if value is None:
            return default

        if isinstance(value, float):
            return f"{value:.4f}"

        return str(value)

    @staticmethod
    def _format_timestamp(value: Any) -> str:
        if value is None:
            return "-"

        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")

        return str(value)

    @staticmethod
    def _merge_event_data(
        event: Any = None,
        *,
        contract: Optional[dict] = None,
        candle: Optional[dict] = None,
        trading_date: Optional[date] = None,
        source: Optional[str] = None,
    ) -> dict[str, Any]:
        merged: dict[str, Any] = {}

        if isinstance(event, dict):
            merged.update(event)
        elif event is not None:
            try:
                merged.update(vars(event))
            except TypeError:
                logger.warning(
                    "Unable to convert notification event to dictionary "
                    "event_type=%s",
                    type(event).__name__,
                )

        if isinstance(contract, dict):
            merged.update(contract)

        if isinstance(candle, dict):
            merged.update(candle)

        if trading_date is not None:
            merged["trading_date"] = trading_date.isoformat()

        if source:
            merged["source"] = source

        if "candle_timestamp" not in merged and "timestamp" in merged:
            merged["candle_timestamp"] = merged.get("timestamp")

        return merged

    @classmethod
    def _get_event_value(
        cls,
        event: Any,
        name: str,
        default: Any = None,
    ) -> Any:
        if event is None:
            return default

        if isinstance(event, dict):
            return event.get(name, default)

        return getattr(event, name, default)

    @classmethod
    def _instrument_label(cls, event: Any) -> str:
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

        return str(
            trading_symbol
            or instrument_key
            or underlying
            or "UNKNOWN"
        )

    @classmethod
    def _build_crossover_event_id(cls, event: Any):
        instrument_key = cls._get_event_value(
            event,
            "instrument_key",
        )
        timestamp = cls._get_event_value(
            event,
            "candle_timestamp",
            cls._get_event_value(event, "timestamp"),
        )
        cross_type = cls._get_event_value(
            event,
            "cross_type",
        )

        if not instrument_key or not timestamp or not cross_type:
            return None

        return (
            f"crossover:"
            f"{instrument_key}:"
            f"{timestamp}:"
            f"{str(cross_type).lower()}"
        )

    def notify_crossover(
        self,
        event: Any = None,
        *,
        contract: Optional[dict] = None,
        candle: Optional[dict] = None,
        trading_date: Optional[date] = None,
        source: Optional[str] = None,
        event_id: Optional[str] = None,
        silent: bool = False,
    ) -> bool:
        if not self.crossover_enabled:
            return False

        normalized_event = self._merge_event_data(
            event,
            contract=contract,
            candle=candle,
            trading_date=trading_date,
            source=source or "ema_runtime.process_candle",
        )

        resolved_event_id = (
            event_id
            or self._build_crossover_event_id(normalized_event)
        )

        if resolved_event_id and self._is_duplicate(resolved_event_id):
            logger.debug(
                "Duplicate crossover notification skipped event_id=%s",
                resolved_event_id,
            )
            return False

        cross_type = self._get_event_value(
            normalized_event,
            "cross_type",
            "UNKNOWN",
        )
        instrument = self._instrument_label(normalized_event)
        timestamp = self._format_timestamp(
            self._get_event_value(
                normalized_event,
                "candle_timestamp",
                self._get_event_value(
                    normalized_event,
                    "timestamp",
                ),
            )
        )
        trading_date_value = self._format_value(
            self._get_event_value(
                normalized_event,
                "trading_date",
            )
        )
        close = self._format_value(
            self._get_event_value(
                normalized_event,
                "close",
            )
        )
        ema_9 = self._format_value(
            self._get_event_value(
                normalized_event,
                "ema_9",
            )
        )
        ema_21 = self._format_value(
            self._get_event_value(
                normalized_event,
                "ema_21",
            )
        )
        difference = self._format_value(
            self._get_event_value(
                normalized_event,
                "ema_difference",
            )
        )
        previous_difference = self._format_value(
            self._get_event_value(
                normalized_event,
                "previous_ema_difference",
            )
        )
        event_source = self._get_event_value(
            normalized_event,
            "source",
            "unknown",
        )

        message = (
            "📊 *EMA CROSSOVER DETECTED*\n"
            "\n"
            f"📌 *Instrument:* `{instrument}`\n"
            f"🔔 *Signal:* `{str(cross_type).upper()}`\n"
            f"📅 *Trading Date:* `{trading_date_value}`\n"
            f"🕒 *Candle Time:* `{timestamp}`\n"
            f"💰 *Close:* `{close}`\n"
            "\n"
            f"📈 *EMA 9:* `{ema_9}`\n"
            f"📉 *EMA 21:* `{ema_21}`\n"
            f"➗ *Difference:* `{difference}`\n"
            f"↩️ *Previous Difference:* `{previous_difference}`\n"
            f"⚙️ *Source:* `{event_source}`"
        )

        logger.info(
            "Sending EMA crossover notification "
            "instrument=%s cross_type=%s event_id=%s",
            instrument,
            cross_type,
            resolved_event_id,
        )

        return self.send(
            message,
            silent=silent,
        )

    def notify_live_started(
        self,
        *,
        instrument_count: Optional[int] = None,
        message: Optional[str] = None,
    ) -> bool:
        if not self.lifecycle_enabled:
            return False

        if not self.notify_live_start:
            return False

        if message:
            notification = message
        else:
            count_text = (
                str(instrument_count)
                if instrument_count is not None
                else "-"
            )

            notification = (
                "🟢 *EMA LIVE PROCESSING STARTED*\n"
                "\n"
                f"📌 *Instruments:* `{count_text}`\n"
                "⏱️ *Mode:* Completed one-minute candles"
            )

        logger.info("Sending live processing started notification")

        return self.send(notification)

    def notify_live_stopped(
        self,
        *,
        instrument_count: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> bool:
        if not self.lifecycle_enabled:
            return False

        count_text = (
            str(instrument_count)
            if instrument_count is not None
            else "-"
        )
        reason_text = reason or "Not specified"

        message = (
            "🔴 *EMA LIVE PROCESSING STOPPED*\n"
            "\n"
            f"📌 *Instruments:* `{count_text}`\n"
            f"📝 *Reason:* `{reason_text}`"
        )

        logger.info("Sending live processing stopped notification")

        return self.send(message)

    def notify_lifecycle(
        self,
        event_name: str,
        *,
        message: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> bool:
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
                    formatted_value = self._format_value(value)
                    lines.append(
                        f"• *{label}:* `{formatted_value}`"
                    )

            notification = "\n".join(lines)

        logger.info(
            "Sending lifecycle notification event=%s",
            event_name,
        )

        return self.send(notification)

    def notify_hard_refresh_started(
        self,
        *,
        job_id: Optional[str] = None,
    ) -> bool:
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
        if not self.lifecycle_enabled:
            return False

        if not self.notify_hard_refresh:
            return False

        job_text = job_id or "-"
        count_text = (
            str(instrument_count)
            if instrument_count is not None
            else "-"
        )
        duration_text = (
            f"{duration_seconds:.2f}"
            if duration_seconds is not None
            else "-"
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
            "Hard refresh failed job_id=%s error=%s",
            job_text,
            error_text,
        )

        return self.send(message)

    def notify_error(
        self,
        error: Any = None,
        *,
        title: Optional[str] = None,
        message: Optional[str] = None,
        source: Optional[str] = None,
        context: Optional[str] = None,
        instrument_key: Optional[str] = None,
        event_id: Optional[str] = None,
        silent: bool = False,
    ) -> bool:
        if not self.error_enabled:
            return False

        if event_id and self._is_duplicate(event_id):
            logger.debug(
                "Duplicate error notification skipped event_id=%s",
                event_id,
            )
            return False

        resolved_error = error

        if resolved_error is None:
            resolved_error = message or "Unknown error"

        error_text = str(resolved_error)

        if len(error_text) > 2000:
            error_text = error_text[:2000] + "..."

        title_text = title or "EMA SERVICE ERROR"
        context_text = context or source or "EMA service"
        instrument_text = instrument_key or "-"

        if message and error is not None:
            detail_text = str(message)

            if len(detail_text) > 2000:
                detail_text = detail_text[:2000] + "..."
        else:
            detail_text = None

        lines = [
            f"🚨 *{title_text.upper()}*",
            "",
            f"📌 *Context:* `{context_text}`",
            f"📊 *Instrument:* `{instrument_text}`",
            f"⚠️ *Error:* `{error_text}`",
        ]

        if detail_text and detail_text != error_text:
            lines.append(f"📝 *Details:* `{detail_text}`")

        notification = "\n".join(lines)

        logger.error(
            "Sending EMA error notification "
            "title=%s context=%s instrument=%s error=%s",
            title_text,
            context_text,
            instrument_text,
            error_text,
        )

        return self.send(
            notification,
            silent=silent,
        )

    def notify_event(
        self,
        event: Any,
        *,
        event_id: Optional[str] = None,
    ) -> bool:
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
                event_name=self._get_event_value(
                    event,
                    "event_name",
                    normalized_event,
                ),
                message=self._get_event_value(
                    event,
                    "message",
                ),
                details=self._get_event_value(
                    event,
                    "details",
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
                    self._get_event_value(
                        event,
                        "message",
                    ),
                ),
                title=self._get_event_value(
                    event,
                    "title",
                ),
                context=self._get_event_value(
                    event,
                    "context",
                ),
                source=self._get_event_value(
                    event,
                    "source",
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
                    self._get_event_value(
                        event,
                        "message",
                    ),
                ),
            )

        logger.debug(
            "No notification handler for event=%s",
            normalized_event,
        )

        return False


notification_service = NotificationService()