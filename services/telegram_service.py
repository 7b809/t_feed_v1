import html
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


class TelegramService:
    def __init__(self):
        self.bot_token = str(getattr(config, "TELEGRAM_BOT_TOKEN", "") or "").strip()
        self.chat_id = str(getattr(config, "TELEGRAM_CHAT_ID", "") or "").strip()
        self.enabled = bool(getattr(config, "TELEGRAM_ENABLED", False))
        self.local_test_mode = bool(getattr(config, "LOCAL_ALERT_TEST_MODE", False))
        self.local_production_delivery_blocked = bool(
            getattr(config, "LOCAL_ALERT_PRODUCTION_DELIVERY_BLOCKED", True)
        )
        self.local_alert_source_name = str(
            getattr(config, "LOCAL_ALERT_SOURCE_NAME", "option_feed_engine_local_test")
            or "option_feed_engine_local_test"
        ).strip()
        self.local_include_original_payload = bool(
            getattr(config, "LOCAL_ALERT_INCLUDE_ORIGINAL_PAYLOAD", True)
        )
        self.timeout_seconds = self._get_effective_timeout_seconds()
        self.market_timezone = self._load_market_timezone()
        self.market_time_format = getattr(
            config, "MARKET_TIME_FORMAT", "%Y-%m-%d %H:%M:%S %Z"
        )
        self.api_url = self._get_effective_api_url()
        logger.info(
            "Telegram service initialized. enabled=%s, local_test_mode=%s, delivery_mode=%s, configured=%s, target_url=%s",
            self.enabled,
            self.local_test_mode,
            self.get_delivery_mode(),
            self.is_configured(),
            self._get_safe_target_url(),
        )

    def _load_market_timezone(self) -> ZoneInfo:
        timezone_name = getattr(config, "MARKET_TIMEZONE", "Asia/Kolkata")
        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            logger.error(
                "Invalid MARKET_TIMEZONE configured: %s. Falling back to Asia/Kolkata.",
                timezone_name,
            )
            return ZoneInfo("Asia/Kolkata")

    def _now_market_time(self) -> str:
        return datetime.now(self.market_timezone).strftime(self.market_time_format)

    def _now_short_market_time(self) -> str:
        return datetime.now(self.market_timezone).strftime("%I:%M:%S %p %Z")

    def _get_effective_api_url(self) -> str | None:
        if self.local_test_mode:
            local_url = (
                str(getattr(config, "LOCAL_TELEGRAM_URL", "") or "").strip().rstrip("/")
            )
            if not local_url:
                logger.error(
                    "LOCAL_TELEGRAM_URL is not configured while LOCAL_ALERT_TEST_MODE=True."
                )
                return None
            return local_url

        if not self.bot_token:
            return None

        return f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    def _get_effective_timeout_seconds(self) -> float:
        configured_helper = getattr(
            config, "get_effective_telegram_timeout_seconds", None
        )
        if callable(configured_helper):
            try:
                return max(0.1, float(configured_helper()))
            except (TypeError, ValueError, OverflowError):
                pass

        if self.local_test_mode:
            try:
                return max(
                    0.1,
                    float(getattr(config, "LOCAL_ALERT_TIMEOUT_SECONDS", 10.0)),
                )
            except (TypeError, ValueError, OverflowError):
                return 10.0

        try:
            return max(
                0.1,
                float(getattr(config, "TELEGRAM_TIMEOUT_SECONDS", 10)),
            )
        except (TypeError, ValueError, OverflowError):
            return 10.0

    def _get_safe_target_url(self) -> str | None:
        if not self.api_url:
            return None

        if self.local_test_mode:
            return self.api_url

        if self.bot_token:
            return self.api_url.replace(self.bot_token, "***")

        return self.api_url

    def get_delivery_mode(self) -> str:
        return "local_test" if self.local_test_mode else "telegram"

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "configured": self.is_configured(),
            "delivery_mode": self.get_delivery_mode(),
            "local_test_mode": self.local_test_mode,
            "target_url_configured": bool(self.api_url),
            "target_url": self._get_safe_target_url() if self.local_test_mode else None,
            "bot_token_configured": bool(self.bot_token),
            "chat_id_configured": bool(self.chat_id),
            "timeout_seconds": self.timeout_seconds,
            "production_delivery_blocked": bool(
                self.local_test_mode and self.local_production_delivery_blocked
            ),
            "secrets_exposed": False,
        }

    def is_configured(self) -> bool:
        if not self.enabled:
            return False

        if self.local_test_mode:
            return bool(self.api_url)

        return bool(self.bot_token and self.chat_id and self.api_url)

    def _escape(self, value: Any) -> str:
        return html.escape(str(value), quote=False)

    def _safe_float(self, value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return None

    def _format_numeric_value(
        self,
        value: Any,
        unavailable_text: str = "not_available",
        decimal_places: int | None = None,
    ) -> str:
        numeric_value = self._safe_float(value)
        if numeric_value is None:
            if value is None:
                return unavailable_text
            text = str(value).strip()
            return text if text else unavailable_text

        if decimal_places is not None:
            return f"{numeric_value:.{decimal_places}f}"

        if numeric_value.is_integer():
            return str(int(numeric_value))

        return f"{numeric_value:.4f}".rstrip("0").rstrip(".")

    def _format_rupee(
        self,
        value: Any,
        unavailable_text: str = "N/A",
        include_sign: bool = False,
    ) -> str:
        numeric_value = self._safe_float(value)
        if numeric_value is None:
            return unavailable_text

        if include_sign and numeric_value > 0:
            return f"+₹{numeric_value:,.2f}"

        if numeric_value < 0:
            return f"-₹{abs(numeric_value):,.2f}"

        return f"₹{numeric_value:,.2f}"

    def _format_price_without_currency(
        self,
        value: Any,
        unavailable_text: str = "N/A",
    ) -> str:
        numeric_value = self._safe_float(value)
        if numeric_value is None:
            return unavailable_text
        return f"{numeric_value:,.2f}"

    def _format_indian_quantity(
        self,
        value: Any,
        unavailable_text: str = "N/A",
    ) -> str:
        numeric_value = self._safe_float(value)
        if numeric_value is None:
            return unavailable_text

        is_negative = numeric_value < 0
        integer_value = abs(int(numeric_value))
        digits = str(integer_value)

        if len(digits) <= 3:
            formatted = digits
        else:
            last_three = digits[-3:]
            remaining = digits[:-3]
            groups = []

            while remaining:
                groups.insert(0, remaining[-2:])
                remaining = remaining[:-2]

            formatted = ",".join(groups) + "," + last_three

        if is_negative:
            return f"-{formatted}"

        return formatted

    def _normalize_option_type(self, option_type: Any) -> str | None:
        if option_type is None:
            return None

        normalized = str(option_type).strip().upper()

        if normalized in {"CE", "CALL", "C"}:
            return "CE"

        if normalized in {"PE", "PUT", "P"}:
            return "PE"

        return None

    def _format_cross_type_display(self, cross_type: Any) -> str:
        normalized = str(cross_type or "").strip().lower()

        if normalized == "bullish_cross":
            return "Bullish Cross"

        if normalized == "bearish_cross":
            return "Bearish Cross"

        if not normalized:
            return "N/A"

        return normalized.replace("_", " ").title()

    def _format_signal_display(self, signal: Any) -> str:
        normalized = str(signal or "").strip().lower()

        if normalized == "bullish":
            return "Bullish"

        if normalized == "bearish":
            return "Bearish"

        if not normalized:
            return "N/A"

        return normalized.replace("_", " ").title()

    def _format_ema_mode_display(self, mode: Any) -> str:
        normalized = str(mode or "").strip().lower()

        if normalized == "candle_close":
            return "Candle Close"

        if normalized == "tick_ltp":
            return "Live Tick / LTP"

        if not normalized:
            return "N/A"

        return normalized.replace("_", " ").title()

    def _format_short_market_time(
        self,
        value: Any,
        unavailable_text: str = "N/A",
        include_seconds: bool = False,
    ) -> str:
        if value is None:
            return unavailable_text

        parsed_datetime = None

        if isinstance(value, datetime):
            parsed_datetime = value
        else:
            text = str(value).strip()

            if not text:
                return unavailable_text

            normalized_text = text.replace("Z", "+00:00")

            try:
                parsed_datetime = datetime.fromisoformat(normalized_text)
            except ValueError:
                supported_formats = (
                    "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M",
                    "%Y-%m-%dT%H:%M",
                )

                for date_format in supported_formats:
                    try:
                        parsed_datetime = datetime.strptime(text, date_format)
                        break
                    except ValueError:
                        continue

        if parsed_datetime is None:
            return str(value)

        if parsed_datetime.tzinfo is None:
            parsed_datetime = parsed_datetime.replace(tzinfo=self.market_timezone)
        else:
            parsed_datetime = parsed_datetime.astimezone(self.market_timezone)

        time_format = "%I:%M:%S %p" if include_seconds else "%I:%M %p"

        return parsed_datetime.strftime(time_format)

    def _get_number_badge(self, index: int) -> str:
        badges = {
            1: "1️⃣",
            2: "2️⃣",
            3: "3️⃣",
            4: "4️⃣",
            5: "5️⃣",
            6: "6️⃣",
            7: "7️⃣",
            8: "8️⃣",
            9: "9️⃣",
            10: "🔟",
        }

        return badges.get(index, f"{index}.")

    def _get_live_ema_calculation_mode(self) -> str:
        return (
            "tick_ltp"
            if bool(getattr(config, "LIVE_EMA_CALCULATION_MODE", False))
            else "candle_close"
        )

    def _get_live_ema_calculation_mode_description(
        self,
        mode: str | None = None,
    ) -> str:
        selected_mode = mode or self._get_live_ema_calculation_mode()

        if selected_mode == "tick_ltp":
            return "Live tick/LTP based EMA cross detection"

        return "Completed candle close based EMA cross detection"

    def _build_production_payload(self, message: str) -> dict:
        return {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

    def _build_local_payload(
        self,
        message: str,
        notification_title: str,
        notification_level: str,
        notification_context: str,
    ) -> dict:
        payload = {
            "channel": "telegram",
            "delivery_mode": "local_test",
            "source": self.local_alert_source_name,
            "title": notification_title,
            "level": notification_level,
            "context": notification_context or "not_available",
            "message": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "market_time": self._now_market_time(),
            "metadata": {
                "local_test_mode": True,
                "production_delivery_blocked": bool(
                    self.local_production_delivery_blocked
                ),
            },
        }

        if self.local_include_original_payload:
            payload["telegram_payload"] = {
                "chat_id": self.chat_id or "local_test_chat",
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }

        return payload

    def _send_raw_message(
        self,
        message: str,
        *,
        notification_title: str = "Unknown",
        notification_level: str = "INFO",
        notification_context: str = "",
    ) -> bool:
        if not self.is_configured():
            logger.warning(
                "Telegram notification skipped. service_configured=False, enabled=%s, local_test_mode=%s, delivery_mode=%s, title=%s, level=%s, context=%s",
                self.enabled,
                self.local_test_mode,
                self.get_delivery_mode(),
                notification_title,
                notification_level,
                notification_context or "not_available",
            )
            return False

        if self.local_test_mode:
            payload = self._build_local_payload(
                message=message,
                notification_title=notification_title,
                notification_level=notification_level,
                notification_context=notification_context,
            )
        else:
            payload = self._build_production_payload(message)

        logger.info(
            "Sending Telegram notification. delivery_mode=%s, title=%s, level=%s, context=%s, target=%s",
            self.get_delivery_mode(),
            notification_title,
            notification_level,
            notification_context or "not_available",
            self._get_safe_target_url(),
        )

        try:
            response = requests.post(
                self.api_url,
                json=payload,
                timeout=self.timeout_seconds,
            )

            response_text = str(response.text or "")

            if not 200 <= response.status_code < 300:
                logger.error(
                    "Telegram notification failed. delivery_mode=%s, title=%s, level=%s, context=%s, status_code=%s, response=%s",
                    self.get_delivery_mode(),
                    notification_title,
                    notification_level,
                    notification_context or "not_available",
                    response.status_code,
                    response_text[:2000],
                )
                return False

            try:
                response_payload = response.json()
            except ValueError:
                response_payload = None

            if (
                not self.local_test_mode
                and isinstance(response_payload, dict)
                and response_payload.get("ok") is False
            ):
                logger.error(
                    "Telegram API rejected notification. title=%s, level=%s, context=%s, response=%s",
                    notification_title,
                    notification_level,
                    notification_context or "not_available",
                    response_text[:2000],
                )
                return False

            if self.local_test_mode and isinstance(response_payload, dict):
                local_success = response_payload.get("success")
                local_ok = response_payload.get("ok")
                local_accepted = response_payload.get("accepted")

                if local_success is False:
                    logger.error(
                        "Local Telegram simulator rejected notification. title=%s, level=%s, context=%s, response=%s",
                        notification_title,
                        notification_level,
                        notification_context or "not_available",
                        response_text[:2000],
                    )
                    return False

                if local_ok is False:
                    logger.error(
                        "Local Telegram simulator returned ok=false. title=%s, level=%s, context=%s, response=%s",
                        notification_title,
                        notification_level,
                        notification_context or "not_available",
                        response_text[:2000],
                    )
                    return False

                if local_accepted is False:
                    logger.error(
                        "Local Telegram simulator returned accepted=false. title=%s, level=%s, context=%s, response=%s",
                        notification_title,
                        notification_level,
                        notification_context or "not_available",
                        response_text[:2000],
                    )
                    return False

            logger.info(
                "Telegram notification sent. delivery_mode=%s, title=%s, level=%s, context=%s, status_code=%s",
                self.get_delivery_mode(),
                notification_title,
                notification_level,
                notification_context or "not_available",
                response.status_code,
            )

            return True

        except requests.Timeout as ex:
            logger.error(
                "Telegram notification timed out. delivery_mode=%s, title=%s, level=%s, context=%s, timeout_seconds=%s, error=%s",
                self.get_delivery_mode(),
                notification_title,
                notification_level,
                notification_context or "not_available",
                self.timeout_seconds,
                ex,
            )
            return False

        except requests.ConnectionError as ex:
            logger.error(
                "Telegram notification connection failed. delivery_mode=%s, title=%s, level=%s, context=%s, target=%s, error=%s",
                self.get_delivery_mode(),
                notification_title,
                notification_level,
                notification_context or "not_available",
                self._get_safe_target_url(),
                ex,
            )
            return False

        except requests.RequestException as ex:
            logger.error(
                "Telegram notification request failed. delivery_mode=%s, title=%s, level=%s, context=%s, exception_type=%s, error=%s",
                self.get_delivery_mode(),
                notification_title,
                notification_level,
                notification_context or "not_available",
                type(ex).__name__,
                ex,
            )
            return False

        except Exception as ex:
            logger.error(
                "Telegram notification exception. delivery_mode=%s, title=%s, level=%s, context=%s, exception_type=%s, error=%s",
                self.get_delivery_mode(),
                notification_title,
                notification_level,
                notification_context or "not_available",
                type(ex).__name__,
                ex,
            )
            return False

    def send_message(
        self,
        title: str,
        message: str,
        level: str = "INFO",
        notification_context: str = "",
    ) -> bool:
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
            "EMA": "📈",
            "OPENING_RANGE": "🎯",
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

        return self._send_raw_message(
            formatted_message,
            notification_title=title,
            notification_level=level_upper,
            notification_context=notification_context,
        )

    def send_startup_message(
        self,
        status: str,
        details: str = "",
    ) -> bool:
        message = f"Application startup status: {status}"

        if details:
            message += f"\n\n{details}"

        return self.send_message(
            title="Option Feed Engine Startup",
            message=message,
            level="STARTUP",
            notification_context="application_startup",
        )

    def send_shutdown_message(
        self,
        details: str = "",
    ) -> bool:
        message = "Application shutdown sequence executed."

        if details:
            message += f"\n\n{details}"

        return self.send_message(
            title="Option Feed Engine Shutdown",
            message=message,
            level="SHUTDOWN",
            notification_context="application_shutdown",
        )

    def send_token_refresh_message(
        self,
        success: bool,
        updated_at: Any = None,
        error: str = "",
    ) -> bool:
        if success:
            message = "Access token document refreshed successfully from MongoDB."

            if updated_at:
                message += f"\nToken Updated At: {updated_at}"

            return self.send_message(
                title="Token Refresh Successful",
                message=message,
                level="TOKEN",
                notification_context="token_refresh_success",
            )

        message = "Access token refresh failed."

        if error:
            message += f"\nError: {error}"

        return self.send_message(
            title="Token Refresh Failed",
            message=message,
            level="ERROR",
            notification_context="token_refresh_failed",
        )

    def send_instruments_fetched_message(
        self,
        success: bool,
        nearest_expiry: Any = None,
        total_contracts: int = 0,
        subscribed_keys_count: int = 0,
        strike_from: Any = None,
        strike_to: Any = None,
        error: str = "",
    ) -> bool:
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
                notification_context="instruments_fetch_success",
            )

        message = "Option instruments fetch failed."

        if error:
            message += f"\nError: {error}"

        return self.send_message(
            title="Instruments Fetch Failed",
            message=message,
            level="ERROR",
            notification_context="instruments_fetch_failed",
        )

    def send_subscription_message(
        self,
        success: bool,
        subscribed_keys_count: int = 0,
        feed_mode: Any = None,
        error: str = "",
    ) -> bool:
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
                notification_context="feed_subscription_success",
            )

        message = "Upstox feed subscription failed."

        if error:
            message += f"\nError: {error}"

        return self.send_message(
            title="Feed Subscription Failed",
            message=message,
            level="ERROR",
            notification_context="feed_subscription_failed",
        )

    def send_daily_refresh_message(
        self,
        success: bool,
        subscribed_keys_count: int = 0,
        nearest_expiry: Any = None,
        error: str = "",
    ) -> bool:
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
                notification_context="daily_refresh_success",
            )

        message = "Daily market hard refresh failed."

        if error:
            message += f"\nError: {error}"

        return self.send_message(
            title="Daily Market Hard Refresh Failed",
            message=message,
            level="ERROR",
            notification_context="daily_refresh_failed",
        )

    def send_selected_or_instrument_message(
        self,
        instrument_key: str,
        symbol: str,
        level: str,
        level_value: Any,
        trigger_field: str,
        trigger_price: Any,
        touch_time: Any,
        source: str,
        nifty_ltp: Any = None,
        strike_price: Any = None,
        instrument_type: Any = None,
        reference_average: Any = None,
        average_window: dict | None = None,
    ) -> bool:
        if not bool(
            getattr(
                config,
                "OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED",
                True,
            )
        ):
            logger.info(
                "Isolated instrument Telegram alert skipped. instrument_key=%s",
                instrument_key,
            )
            return False

        strike_text = strike_price if strike_price is not None else "N/A"
        type_text = self._normalize_option_type(instrument_type) or "N/A"
        window_text = "not_available"

        if isinstance(average_window, dict):
            window_text = (
                f"{average_window.get('final_lower')} "
                f"to {average_window.get('final_upper')}"
            )

        ema_mode = self._get_live_ema_calculation_mode()

        message = (
            "Opening Range instrument isolated for EMA alerts.\n\n"
            f"Instrument: {strike_text} {type_text}\n"
            f"Symbol: {symbol}\n"
            f"Instrument Key: {instrument_key}\n"
            f"Selected Level: {level}\n"
            f"Level Value: {level_value}\n"
            f"Trigger {trigger_field}: {trigger_price}\n"
            f"Touch Time: {touch_time}\n"
            f"Touch Source: {source}\n"
            f"Reference Average: {reference_average}\n"
            f"Average Window: {window_text}\n"
            f"NIFTY LTP: {nifty_ltp if nifty_ltp is not None else 'not_available'}\n"
            f"EMA Calculation Mode: {ema_mode}"
        )

        result = self.send_message(
            title="Opening Range Instrument Isolated",
            message=message,
            level="OPENING_RANGE",
            notification_context=(
                f"isolated_instrument|instrument_key={instrument_key}"
                f"|strike={strike_text}|type={type_text}|level={level}"
            ),
        )

        if result:
            logger.info(
                "Isolated instrument Telegram alert sent. instrument_key=%s",
                instrument_key,
            )
        else:
            logger.error(
                "Isolated instrument Telegram alert failed. instrument_key=%s",
                instrument_key,
            )

        return result

    def _format_suggested_order_instruments(
        self,
        suggested_order_instruments: list,
        suggested_order_side: str | None = None,
    ) -> str:
        option_type = self._normalize_option_type(suggested_order_side) or "OPTION"

        valid_instruments = [
            item for item in suggested_order_instruments if isinstance(item, dict)
        ]

        lines = [f"📍 <b>NEAREST {self._escape(option_type)} CONTRACTS</b>"]

        if not valid_instruments:
            lines.extend(["", "No matching instruments available."])
            return "\n".join(lines)

        for index, item in enumerate(valid_instruments, start=1):
            strike = self._format_numeric_value(
                item.get("strike_price"),
                unavailable_text="N/A",
            )

            instrument_type = (
                self._normalize_option_type(
                    item.get("instrument_type") or item.get("option_type")
                )
                or option_type
            )

            market_data = item.get("market_data", {})

            if not isinstance(market_data, dict):
                market_data = {}

            ltp_value = (
                item.get("ltp") if item.get("ltp") is not None else item.get("live_ltp")
            )

            if ltp_value is None:
                ltp_value = market_data.get("ltp")

            volume_value = market_data.get("volume")

            if volume_value is None:
                volume_value = item.get("volume")

            badge = self._get_number_badge(index)

            lines.extend(
                [
                    "",
                    f"{badge} <b>{self._escape(strike)} {self._escape(instrument_type)}</b>",
                    f"   💰 LTP     : {self._format_rupee(ltp_value)}",
                    f"   📊 Volume  : {self._format_indian_quantity(volume_value)}",
                ]
            )

        return "\n".join(lines)

    def _format_budget_range_instruments(
        self,
        budget_range_instruments: list,
        suggested_order_side: str | None = None,
    ) -> str:
        minimum_price = getattr(
            config,
            "EMA_ALERT_BUDGET_MIN_PRICE",
            50.0,
        )

        maximum_price = getattr(
            config,
            "EMA_ALERT_BUDGET_MAX_PRICE",
            120.0,
        )

        option_type = self._normalize_option_type(suggested_order_side) or "OPTION"

        valid_instruments = [
            item for item in budget_range_instruments if isinstance(item, dict)
        ]

        lines = [
            "💵 <b>BUDGET MATCHES</b>",
            f"Range: {self._format_rupee(minimum_price)} to {self._format_rupee(maximum_price)}",
        ]

        if not valid_instruments:
            lines.extend(
                [
                    "",
                    f"No matching {self._escape(option_type)} instruments.",
                ]
            )
            return "\n".join(lines)

        for item in valid_instruments:
            strike = self._format_numeric_value(
                item.get("strike_price"),
                unavailable_text="N/A",
            )

            instrument_type = (
                self._normalize_option_type(
                    item.get("instrument_type") or item.get("option_type")
                )
                or option_type
            )

            market_data = item.get("market_data", {})

            if not isinstance(market_data, dict):
                market_data = {}

            ltp_value = (
                item.get("ltp") if item.get("ltp") is not None else item.get("live_ltp")
            )

            if ltp_value is None:
                ltp_value = market_data.get("ltp")

            lines.extend(
                [
                    "",
                    f"✅ <b>{self._escape(strike)} {self._escape(instrument_type)}</b>  •  <b>{self._format_rupee(ltp_value)}</b>",
                ]
            )

        return "\n".join(lines)

    def send_selected_or_ema_cross_message(
        self,
        selected_state: dict,
        ema_event: dict,
        nifty_ltp: Any = None,
        suggested_order_instruments: list | None = None,
        budget_range_instruments: list | None = None,
    ) -> bool:
        if not bool(
            getattr(
                config,
                "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED",
                True,
            )
        ):
            logger.info("Isolated EMA Telegram alert skipped.")
            return False

        selected_state = selected_state if isinstance(selected_state, dict) else {}

        ema_event = ema_event if isinstance(ema_event, dict) else {}

        suggested_order_instruments = (
            suggested_order_instruments
            if isinstance(suggested_order_instruments, list)
            else []
        )

        budget_range_instruments = (
            budget_range_instruments
            if isinstance(budget_range_instruments, list)
            else []
        )

        contract_info = (
            selected_state.get("contract_info")
            or ema_event.get("contract_info")
            or ema_event.get("info")
            or {}
        )

        if not isinstance(contract_info, dict):
            contract_info = {}

        strike = contract_info.get("strike_price", "N/A")

        isolated_instrument_type = (
            self._normalize_option_type(
                contract_info.get("instrument_type") or contract_info.get("option_type")
            )
            or "N/A"
        )

        selected_level = selected_state.get("selected_level") or "N/A"

        instrument_key = (
            ema_event.get("instrument_key")
            or selected_state.get("instrument_key")
            or "not_available"
        )

        cross_type = ema_event.get("cross_type") or "N/A"

        current_signal = (
            ema_event.get("current_signal") or ema_event.get("signal") or "N/A"
        )

        ema_mode = (
            ema_event.get("ema_calculation_mode")
            or ema_event.get("calculation_mode")
            or self._get_live_ema_calculation_mode()
        )

        candle = ema_event.get("candle") or {}

        if not isinstance(candle, dict):
            candle = {}

        candle_close = (
            candle.get("close")
            if candle.get("close") is not None
            else ema_event.get("close")
        )

        candle_low = candle.get("low")
        candle_time = candle.get("timestamp") or ema_event.get("timestamp")

        close_value = self._safe_float(candle_close)
        low_value = self._safe_float(candle_low)

        close_low_movement = None

        if close_value is not None and low_value is not None:
            close_low_movement = close_value - low_value

        suggested_order_option_type = None

        for item in suggested_order_instruments:
            if not isinstance(item, dict):
                continue

            suggested_order_option_type = self._normalize_option_type(
                item.get("instrument_type") or item.get("option_type")
            )

            if suggested_order_option_type:
                break

        cross_text = str(cross_type).strip().lower()

        if not suggested_order_option_type:
            if "bullish" in cross_text:
                suggested_order_option_type = isolated_instrument_type
            elif "bearish" in cross_text:
                if isolated_instrument_type == "CE":
                    suggested_order_option_type = "PE"
                elif isolated_instrument_type == "PE":
                    suggested_order_option_type = "CE"

        suggested_order_side = suggested_order_option_type or "N/A"

        strike_text = self._format_numeric_value(
            strike,
            unavailable_text="N/A",
        )

        nifty_text = self._format_price_without_currency(
            nifty_ltp,
            unavailable_text="N/A",
        )

        cross_type_display = self._format_cross_type_display(cross_type)
        signal_display = self._format_signal_display(current_signal)
        ema_mode_display = self._format_ema_mode_display(ema_mode)

        direction_text = (
            "BULLISH"
            if "bullish" in cross_text
            else ("BEARISH" if "bearish" in cross_text else signal_display.upper())
        )

        direction_icon = (
            "📈"
            if direction_text == "BULLISH"
            else ("📉" if direction_text == "BEARISH" else "📊")
        )

        nearest_text = self._format_suggested_order_instruments(
            suggested_order_instruments,
            suggested_order_side,
        )

        budget_text = self._format_budget_range_instruments(
            budget_range_instruments,
            suggested_order_side,
        )

        message_lines = [
            "🚨 <b>ISOLATED EMA SIGNAL</b>",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "",
            f"📌 <b>{self._escape(strike_text)} {self._escape(isolated_instrument_type)}  •  {self._escape(direction_text)}</b>",
            f"{direction_icon} Crossed <b>{self._escape(selected_level)}</b>",
            f"💹 NIFTY SPOT: <b>{self._escape(nifty_text)}</b>",
            "",
            "🎯 <b>TRADE DIRECTION</b>",
            f"├ Isolated Side : <b>{self._escape(isolated_instrument_type)}</b>",
            f"├ Suggested Side: <b>{self._escape(suggested_order_side)}</b>",
            f"├ Cross Type    : {self._escape(cross_type_display)}",
            f"└ Signal        : {self._escape(signal_display)}",
            "",
            "📊 <b>EMA CANDLE</b>",
            f"├ Mode      : {self._escape(ema_mode_display)}",
        ]

        if bool(
            getattr(
                config,
                "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_CLOSE",
                True,
            )
        ):
            message_lines.append(f"├ Close     : {self._format_rupee(candle_close)}")

        if bool(
            getattr(
                config,
                "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_LOW",
                True,
            )
        ):
            message_lines.append(f"├ Low       : {self._format_rupee(candle_low)}")

        if bool(
            getattr(
                config,
                "EMA_ISOLATED_ALERT_INCLUDE_CLOSE_LOW_DIFFERENCE",
                True,
            )
        ):
            message_lines.append(
                f"├ Movement  : {self._format_rupee(close_low_movement, include_sign=True)}"
            )

        if bool(
            getattr(
                config,
                "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_TIME",
                True,
            )
        ):
            message_lines.append(
                f"└ Time      : {self._escape(self._format_short_market_time(candle_time))}"
            )
        else:
            message_lines.append("└ Time      : N/A")

        message_lines.extend(
            [
                "",
                "🔑 <b>INSTRUMENT</b>",
                self._escape(instrument_key),
            ]
        )

        if bool(
            getattr(
                config,
                "EMA_ISOLATED_ALERT_INCLUDE_NEAREST_ORDER_INSTRUMENTS",
                True,
            )
        ):
            message_lines.extend(["", nearest_text])

        if bool(
            getattr(
                config,
                "EMA_ISOLATED_ALERT_INCLUDE_BUDGET_INSTRUMENTS",
                True,
            )
        ):
            message_lines.extend(["", budget_text])

        message_lines.extend(
            [
                "",
                "━━━━━━━━━━━━━━━━━━━━━━",
                f"🕒 Alert Time: <b>{self._escape(self._now_short_market_time())}</b>",
            ]
        )

        message = "\n".join(message_lines)

        logger.info(
            "Sending isolated EMA alert. delivery_mode=%s, instrument_key=%s, cross_type=%s, order_side=%s, nearest_count=%s, budget_count=%s",
            self.get_delivery_mode(),
            instrument_key,
            cross_type,
            suggested_order_side,
            len(suggested_order_instruments),
            len(budget_range_instruments),
        )

        result = self._send_raw_message(
            message,
            notification_title="Isolated EMA Signal",
            notification_level="EMA",
            notification_context=(
                f"isolated_ema|instrument_key={instrument_key}"
                f"|cross_type={cross_type}"
            ),
        )

        if result:
            logger.info(
                "Isolated EMA Telegram alert sent. delivery_mode=%s, instrument_key=%s",
                self.get_delivery_mode(),
                instrument_key,
            )
        else:
            logger.error(
                "Isolated EMA Telegram alert failed. delivery_mode=%s, instrument_key=%s",
                self.get_delivery_mode(),
                instrument_key,
            )

        return result

    def send_isolated_ema_payload(self, payload: dict) -> bool:
        if not isinstance(payload, dict):
            return False

        instrument = payload.get("instrument") or {}
        opening_range = payload.get("opening_range") or {}
        market_snapshot = payload.get("market_snapshot") or {}
        ema_data = payload.get("ema") or {}
        order_suggestion = payload.get("order_suggestion") or {}
        candle = ema_data.get("candle") or {}

        selected_state = {
            "instrument_key": instrument.get("instrument_key"),
            "selected_level": opening_range.get("selected_level"),
            "contract_info": {
                **instrument,
                "instrument_type": instrument.get("instrument_type"),
            },
        }

        ema_event = {
            **ema_data,
            "instrument_key": instrument.get("instrument_key"),
            "cross_type": ema_data.get("cross_type"),
            "current_signal": (
                ema_data.get("current_signal") or ema_data.get("signal")
            ),
            "ema_calculation_mode": ema_data.get("calculation_mode"),
            "close": candle.get("close"),
            "timestamp": (candle.get("timestamp") or ema_data.get("timestamp")),
            "candle": candle,
        }

        budget_filter = order_suggestion.get("budget_filter") or {}

        return self.send_selected_or_ema_cross_message(
            selected_state=selected_state,
            ema_event=ema_event,
            nifty_ltp=market_snapshot.get("nifty_ltp"),
            suggested_order_instruments=(
                order_suggestion.get("nearest_instruments") or []
            ),
            budget_range_instruments=(budget_filter.get("instruments") or []),
        )

    def send_isolated_instrument_message(
        self,
        isolated_state: dict,
    ) -> bool:
        isolated_state = isolated_state if isinstance(isolated_state, dict) else {}

        contract_info = isolated_state.get("contract_info") or {}

        symbol = (
            contract_info.get("trading_symbol")
            or contract_info.get("instrument_key")
            or isolated_state.get("instrument_key")
            or "N/A"
        )

        return self.send_selected_or_instrument_message(
            instrument_key=isolated_state.get("instrument_key"),
            symbol=symbol,
            level=isolated_state.get("selected_level"),
            level_value=isolated_state.get("level_value"),
            trigger_field=isolated_state.get("trigger_field"),
            trigger_price=isolated_state.get("trigger_price"),
            touch_time=isolated_state.get("touch_time"),
            source=isolated_state.get("touch_source"),
            nifty_ltp=isolated_state.get("latest_main_index_ltp"),
            strike_price=contract_info.get("strike_price"),
            instrument_type=contract_info.get("instrument_type"),
            reference_average=isolated_state.get("reference_average"),
            average_window=isolated_state.get("average_window"),
        )

    def send_isolated_ema_cross_message(
        self,
        isolated_state: dict,
        ema_event: dict,
        nifty_ltp: Any = None,
        suggested_order_instruments: list | None = None,
        budget_range_instruments: list | None = None,
    ) -> bool:
        return self.send_selected_or_ema_cross_message(
            selected_state=isolated_state,
            ema_event=ema_event,
            nifty_ltp=nifty_ltp,
            suggested_order_instruments=suggested_order_instruments,
            budget_range_instruments=budget_range_instruments,
        )

    def send_exception_message(
        self,
        title: str,
        exception: Exception,
        context: str = "",
    ) -> bool:
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
            notification_context=(
                f"exception|context={context}" if context else "exception"
            ),
        )


telegram_service = TelegramService()
