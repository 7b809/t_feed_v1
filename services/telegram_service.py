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
        self.bot_token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.enabled = config.TELEGRAM_ENABLED

        self.timeout_seconds = int(
            getattr(
                config,
                "TELEGRAM_TIMEOUT_SECONDS",
                10,
            )
        )

        self.market_timezone = self._load_market_timezone()

        self.market_time_format = getattr(
            config,
            "MARKET_TIME_FORMAT",
            "%Y-%m-%d %H:%M:%S %Z",
        )

        self.api_url = (
            f"https://api.telegram.org/" f"bot{self.bot_token}/sendMessage"
            if self.bot_token
            else None
        )

    # ============================================================
    # Time and Configuration
    # ============================================================

    def _load_market_timezone(self) -> ZoneInfo:
        timezone_name = getattr(
            config,
            "MARKET_TIMEZONE",
            "Asia/Kolkata",
        )

        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            logger.error(
                "Invalid MARKET_TIMEZONE configured: %s. "
                "Falling back to Asia/Kolkata.",
                timezone_name,
            )

            return ZoneInfo("Asia/Kolkata")

    def _now_market_time(self) -> str:
        return datetime.now(self.market_timezone).strftime(self.market_time_format)

    def is_configured(self) -> bool:
        return bool(self.enabled and self.bot_token and self.chat_id and self.api_url)

    # ============================================================
    # Formatting Helpers
    # ============================================================

    def _escape(
        self,
        value: Any,
    ) -> str:
        return html.escape(
            str(value),
            quote=False,
        )

    def _safe_float(
        self,
        value: Any,
    ) -> float | None:
        try:
            if value is None:
                return None

            return float(value)
        except (
            TypeError,
            ValueError,
            OverflowError,
        ):
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

    def _normalize_option_type(
        self,
        option_type: Any,
    ) -> str | None:
        if option_type is None:
            return None

        normalized = str(option_type).strip().upper()

        if normalized in {
            "CE",
            "CALL",
        }:
            return "CE"

        if normalized in {
            "PE",
            "PUT",
        }:
            return "PE"

        return None

    def _get_live_ema_calculation_mode(
        self,
    ) -> str:
        return (
            "tick_ltp"
            if bool(
                getattr(
                    config,
                    "LIVE_EMA_CALCULATION_MODE",
                    False,
                )
            )
            else "candle_close"
        )

    def _get_live_ema_calculation_mode_description(
        self,
        mode: str | None = None,
    ) -> str:
        selected_mode = mode or self._get_live_ema_calculation_mode()

        if selected_mode == "tick_ltp":
            return "Live tick/LTP based EMA cross " "detection"

        return "Completed candle close based EMA " "cross detection"

    # ============================================================
    # Raw Telegram Delivery
    # ============================================================

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
                "Telegram notification skipped. "
                "service_configured=False, "
                "title=%s, level=%s, context=%s",
                notification_title,
                notification_level,
                (notification_context or "not_available"),
            )

            return False

        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        logger.info(
            "Sending Telegram notification. " "title=%s, level=%s, context=%s",
            notification_title,
            notification_level,
            (notification_context or "not_available"),
        )

        try:
            response = requests.post(
                self.api_url,
                json=payload,
                timeout=self.timeout_seconds,
            )

            if response.status_code != 200:
                logger.error(
                    "Telegram notification failed. "
                    "title=%s, level=%s, "
                    "context=%s, status_code=%s, "
                    "response=%s",
                    notification_title,
                    notification_level,
                    (notification_context or "not_available"),
                    response.status_code,
                    response.text,
                )

                return False

            logger.info(
                "Telegram notification sent. "
                "title=%s, level=%s, "
                "context=%s, status_code=%s",
                notification_title,
                notification_level,
                (notification_context or "not_available"),
                response.status_code,
            )

            return True

        except Exception as ex:
            logger.error(
                "Telegram notification exception. "
                "title=%s, level=%s, "
                "context=%s, exception_type=%s, "
                "error=%s",
                notification_title,
                notification_level,
                (notification_context or "not_available"),
                type(ex).__name__,
                ex,
            )

            return False

    # ============================================================
    # Generic Message
    # ============================================================

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

        emoji = emoji_map.get(
            level_upper,
            "ℹ️",
        )

        safe_title = self._escape(title)
        safe_message = self._escape(message)

        market_time = self._escape(self._now_market_time())

        formatted_message = (
            f"{emoji} <b>{safe_title}</b>\n\n"
            f"{safe_message}\n\n"
            f"<b>Level:</b> "
            f"{self._escape(level_upper)}\n"
            f"<b>Time:</b> {market_time}"
        )

        return self._send_raw_message(
            formatted_message,
            notification_title=title,
            notification_level=level_upper,
            notification_context=(notification_context),
        )

    # ============================================================
    # Application Lifecycle Messages
    # ============================================================

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
            notification_context=("application_startup"),
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
            notification_context=("application_shutdown"),
        )

    # ============================================================
    # Token Messages
    # ============================================================

    def send_token_refresh_message(
        self,
        success: bool,
        updated_at: Any = None,
        error: str = "",
    ) -> bool:
        if success:
            message = "Access token document refreshed " "successfully from MongoDB."

            if updated_at:
                message += f"\nToken Updated At: {updated_at}"

            return self.send_message(
                title="Token Refresh Successful",
                message=message,
                level="TOKEN",
                notification_context=("token_refresh_success"),
            )

        message = "Access token refresh failed."

        if error:
            message += f"\nError: {error}"

        return self.send_message(
            title="Token Refresh Failed",
            message=message,
            level="ERROR",
            notification_context=("token_refresh_failed"),
        )

    # ============================================================
    # Instrument Messages
    # ============================================================

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
                "Option instruments fetched and cache "
                "updated successfully.\n\n"
                f"Nearest Expiry: {nearest_expiry}\n"
                f"Total Contracts: {total_contracts}\n"
                f"Subscribed Keys: "
                f"{subscribed_keys_count}\n"
                f"Strike Range: "
                f"{strike_from} to {strike_to}"
            )

            return self.send_message(
                title="Instruments Fetch Successful",
                message=message,
                level="INSTRUMENTS",
                notification_context=("instruments_fetch_success"),
            )

        message = "Option instruments fetch failed."

        if error:
            message += f"\nError: {error}"

        return self.send_message(
            title="Instruments Fetch Failed",
            message=message,
            level="ERROR",
            notification_context=("instruments_fetch_failed"),
        )

    # ============================================================
    # Subscription Messages
    # ============================================================

    def send_subscription_message(
        self,
        success: bool,
        subscribed_keys_count: int = 0,
        feed_mode: Any = None,
        error: str = "",
    ) -> bool:
        if success:
            message = (
                "Upstox streamer subscription is "
                "active.\n\n"
                f"Subscribed Instruments: "
                f"{subscribed_keys_count}\n"
                f"Feed Mode: {feed_mode}"
            )

            return self.send_message(
                title="Feed Subscription Successful",
                message=message,
                level="SUBSCRIPTION",
                notification_context=("feed_subscription_success"),
            )

        message = "Upstox feed subscription failed."

        if error:
            message += f"\nError: {error}"

        return self.send_message(
            title="Feed Subscription Failed",
            message=message,
            level="ERROR",
            notification_context=("feed_subscription_failed"),
        )

    # ============================================================
    # Refresh Messages
    # ============================================================

    def send_daily_refresh_message(
        self,
        success: bool,
        subscribed_keys_count: int = 0,
        nearest_expiry: Any = None,
        error: str = "",
    ) -> bool:
        if success:
            message = (
                "Daily market hard refresh completed "
                "successfully.\n\n"
                f"Nearest Expiry: {nearest_expiry}\n"
                f"Subscribed Instruments: "
                f"{subscribed_keys_count}"
            )

            return self.send_message(
                title=("Daily Market Hard Refresh " "Successful"),
                message=message,
                level="REFRESH",
                notification_context=("daily_refresh_success"),
            )

        message = "Daily market hard refresh failed."

        if error:
            message += f"\nError: {error}"

        return self.send_message(
            title="Daily Market Hard Refresh Failed",
            message=message,
            level="ERROR",
            notification_context=("daily_refresh_failed"),
        )

    # ============================================================
    # Isolated Instrument Messages
    # ============================================================

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
                "Isolated instrument Telegram alert " "skipped. instrument_key=%s",
                instrument_key,
            )

            return False

        strike_text = strike_price if strike_price is not None else "N/A"

        type_text = self._normalize_option_type(instrument_type) or "N/A"

        window_text = "not_available"

        if isinstance(average_window, dict):
            window_text = (
                f"{average_window.get('final_lower')} "
                f"to "
                f"{average_window.get('final_upper')}"
            )

        ema_mode = self._get_live_ema_calculation_mode()

        message = (
            "Opening Range instrument isolated for "
            "EMA alerts.\n\n"
            f"Instrument: {strike_text} "
            f"{type_text}\n"
            f"Symbol: {symbol}\n"
            f"Instrument Key: {instrument_key}\n"
            f"Selected Level: {level}\n"
            f"Level Value: {level_value}\n"
            f"Trigger {trigger_field}: "
            f"{trigger_price}\n"
            f"Touch Time: {touch_time}\n"
            f"Touch Source: {source}\n"
            f"Reference Average: "
            f"{reference_average}\n"
            f"Average Window: {window_text}\n"
            f"NIFTY LTP: "
            f"{nifty_ltp if nifty_ltp is not None else 'not_available'}\n"
            f"EMA Calculation Mode: {ema_mode}"
        )

        result = self.send_message(
            title=("Opening Range Instrument Isolated"),
            message=message,
            level="OPENING_RANGE",
            notification_context=(
                f"isolated_instrument"
                f"|instrument_key={instrument_key}"
                f"|strike={strike_text}"
                f"|type={type_text}"
                f"|level={level}"
            ),
        )

        if result:
            logger.info(
                "Isolated instrument Telegram alert " "sent. instrument_key=%s",
                instrument_key,
            )
        else:
            logger.error(
                "Isolated instrument Telegram alert " "failed. instrument_key=%s",
                instrument_key,
            )

        return result

    # ============================================================
    # EMA Instrument Formatting
    # ============================================================

    def _format_suggested_order_instruments(
        self,
        suggested_order_instruments: list,
    ) -> str:
        if not suggested_order_instruments:
            return "Nearest Option Chain Instruments:\n" "- not_available"

        decimal_places = int(
            getattr(
                config,
                "EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES",
                2,
            )
        )

        lines = ["Nearest Option Chain Instruments:"]

        for item in suggested_order_instruments:
            if not isinstance(item, dict):
                continue

            strike = self._format_numeric_value(
                item.get("strike_price"),
                unavailable_text="N/A",
            )

            instrument_type = (
                self._normalize_option_type(
                    item.get("instrument_type") or item.get("option_type")
                )
                or "N/A"
            )

            market_data = item.get(
                "market_data",
                {},
            )

            if not isinstance(
                market_data,
                dict,
            ):
                market_data = {}

            ltp = self._format_numeric_value(
                item.get("ltp"),
                unavailable_text="N/A",
                decimal_places=decimal_places,
            )

            bid_price = self._format_numeric_value(
                market_data.get("bid_price"),
                unavailable_text="N/A",
                decimal_places=decimal_places,
            )

            ask_price = self._format_numeric_value(
                market_data.get("ask_price"),
                unavailable_text="N/A",
                decimal_places=decimal_places,
            )

            oi = self._format_numeric_value(
                market_data.get("oi"),
                unavailable_text="N/A",
            )

            volume = self._format_numeric_value(
                market_data.get("volume"),
                unavailable_text="N/A",
            )

            isolated_text = " [ISOLATED]" if item.get("is_isolated_instrument") else ""

            lines.extend(
                [
                    f"- {strike}{instrument_type}{isolated_text}",
                    f"  LTP: {ltp}rs",
                    f"  Bid/Ask: {bid_price}/{ask_price}",
                    f"  OI: {oi}",
                    f"  Volume: {volume}",
                ]
            )

        return "\n".join(lines)

    def _format_budget_range_instruments(
        self,
        budget_range_instruments: list,
        suggested_order_side: str | None = None,
    ) -> str:
        decimal_places = int(
            getattr(
                config,
                "EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES",
                2,
            )
        )

        minimum_price = self._format_numeric_value(
            getattr(
                config,
                "EMA_ALERT_BUDGET_MIN_PRICE",
                20.0,
            ),
            decimal_places=decimal_places,
        )

        maximum_price = self._format_numeric_value(
            getattr(
                config,
                "EMA_ALERT_BUDGET_MAX_PRICE",
                30.0,
            ),
            decimal_places=decimal_places,
        )

        option_type = self._normalize_option_type(suggested_order_side) or "option"

        lines = [
            (
                "Budget Range Option Chain Instruments "
                f"({minimum_price}rs to "
                f"{maximum_price}rs):"
            )
        ]

        if not budget_range_instruments:
            lines.append(f"- No matching {option_type} instruments")

            return "\n".join(lines)

        for item in budget_range_instruments:
            if not isinstance(item, dict):
                continue

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

            market_data = item.get(
                "market_data",
                {},
            )

            if not isinstance(
                market_data,
                dict,
            ):
                market_data = {}

            ltp = self._format_numeric_value(
                item.get("ltp"),
                unavailable_text="N/A",
                decimal_places=decimal_places,
            )

            bid_price = self._format_numeric_value(
                market_data.get("bid_price"),
                unavailable_text="N/A",
                decimal_places=decimal_places,
            )

            ask_price = self._format_numeric_value(
                market_data.get("ask_price"),
                unavailable_text="N/A",
                decimal_places=decimal_places,
            )

            oi = self._format_numeric_value(
                market_data.get("oi"),
                unavailable_text="N/A",
            )

            isolated_text = " [ISOLATED]" if item.get("is_isolated_instrument") else ""

            lines.extend(
                [
                    f"- {strike}{instrument_type}{isolated_text}",
                    f"  LTP: {ltp}rs",
                    f"  Bid/Ask: {bid_price}/{ask_price}",
                    f"  OI: {oi}",
                ]
            )

        return "\n".join(lines)

    # ============================================================
    # Isolated EMA Alert
    # ============================================================

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

        selected_state = selected_state or {}
        ema_event = ema_event or {}

        suggested_order_instruments = suggested_order_instruments or []

        budget_range_instruments = budget_range_instruments or []

        contract_info = (
            selected_state.get("contract_info")
            or ema_event.get("contract_info")
            or ema_event.get("info")
            or {}
        )

        strike = contract_info.get(
            "strike_price",
            "N/A",
        )

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

        candle_time = (
            candle.get("timestamp") or ema_event.get("timestamp") or "not_available"
        )

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

        if not suggested_order_option_type:
            cross_text = str(cross_type).lower()

            if "bullish" in cross_text:
                suggested_order_option_type = isolated_instrument_type

            elif "bearish" in cross_text:
                if isolated_instrument_type == "CE":
                    suggested_order_option_type = "PE"

                elif isolated_instrument_type == "PE":
                    suggested_order_option_type = "CE"

        suggested_order_side = suggested_order_option_type or "not_available"

        decimal_places = int(
            getattr(
                config,
                "EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES",
                2,
            )
        )

        strike_text = self._format_numeric_value(
            strike,
            unavailable_text="N/A",
        )

        nifty_text = self._format_numeric_value(
            nifty_ltp,
            unavailable_text="N/A",
            decimal_places=decimal_places,
        )

        close_text = self._format_numeric_value(
            candle_close,
            unavailable_text="N/A",
            decimal_places=decimal_places,
        )

        low_text = self._format_numeric_value(
            candle_low,
            unavailable_text="N/A",
            decimal_places=decimal_places,
        )

        movement_text = self._format_numeric_value(
            close_low_movement,
            unavailable_text="N/A",
            decimal_places=decimal_places,
        )

        # ==========================================
        # Option Chain Metadata
        # ==========================================

        option_chain_reference = None

        if suggested_order_instruments:
            option_chain_reference = suggested_order_instruments[0]
        elif budget_range_instruments:
            option_chain_reference = budget_range_instruments[0]

        if not isinstance(
            option_chain_reference,
            dict,
        ):
            option_chain_reference = {}

        underlying_spot_price = self._format_numeric_value(
            option_chain_reference.get("underlying_spot_price"),
            unavailable_text="N/A",
            decimal_places=decimal_places,
        )

        option_chain_expiry = option_chain_reference.get("expiry") or "N/A"

        option_chain_source = option_chain_reference.get("data_source") or "N/A"

        nearest_text = self._format_suggested_order_instruments(
            suggested_order_instruments
        )

        budget_text = self._format_budget_range_instruments(
            budget_range_instruments,
            suggested_order_side,
        )

        message_lines = [
            (
                f"{strike_text} "
                f"{isolated_instrument_type} "
                f"- crosses {selected_level} "
                f"- At {nifty_text}"
            ),
            "",
            "EMA Cross Details:",
            f"Cross Type: {cross_type}",
            f"Signal: {current_signal}",
            ("Isolated Instrument Type: " f"{isolated_instrument_type}"),
            ("Suggested Order Side: " f"{suggested_order_side}"),
            ("EMA Calculation Mode: " f"{ema_mode}"),
            ("Underlying Spot: " f"{underlying_spot_price}"),
            ("Expiry: " f"{option_chain_expiry}"),
            ("Option Source: " f"{option_chain_source}"),
        ]

        if bool(
            getattr(
                config,
                "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_CLOSE",
                True,
            )
        ):
            message_lines.append(f"EMA Candle Close: {close_text}")

        if bool(
            getattr(
                config,
                "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_LOW",
                True,
            )
        ):
            message_lines.append(f"EMA Candle Low: {low_text}")

        if bool(
            getattr(
                config,
                "EMA_ISOLATED_ALERT_INCLUDE_CLOSE_LOW_DIFFERENCE",
                True,
            )
        ):
            message_lines.append(f"EMA Close-Low Movement: " f"{movement_text} points")

        if bool(
            getattr(
                config,
                "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_TIME",
                True,
            )
        ):
            message_lines.extend(
                [
                    "",
                    f"EMA Candle Time: {candle_time}",
                ]
            )

        message_lines.extend(
            [
                "",
                f"Instrument Key: {instrument_key}",
            ]
        )

        if bool(
            getattr(
                config,
                "EMA_ISOLATED_ALERT_INCLUDE_NEAREST_ORDER_INSTRUMENTS",
                True,
            )
        ):
            message_lines.extend(
                [
                    "",
                    nearest_text,
                ]
            )

        if bool(
            getattr(
                config,
                "EMA_ISOLATED_ALERT_INCLUDE_BUDGET_INSTRUMENTS",
                True,
            )
        ):
            message_lines.extend(
                [
                    "",
                    budget_text,
                ]
            )

        message = "\n".join(message_lines)

        logger.info(
            "Sending isolated EMA alert. "
            "instrument_key=%s, cross_type=%s, "
            "order_side=%s, nearest_count=%s, "
            "budget_count=%s",
            instrument_key,
            cross_type,
            suggested_order_side,
            len(suggested_order_instruments),
            len(budget_range_instruments),
        )

        result = self.send_message(
            title="Isolated Instrument EMA Alert",
            message=message,
            level="EMA",
            notification_context=(
                f"isolated_ema"
                f"|instrument_key={instrument_key}"
                f"|cross_type={cross_type}"
            ),
        )

        if result:
            logger.info(
                "Isolated EMA Telegram alert sent. " "instrument_key=%s",
                instrument_key,
            )
        else:
            logger.error(
                "Isolated EMA Telegram alert failed. " "instrument_key=%s",
                instrument_key,
            )

        return result

    def send_isolated_ema_payload(
        self,
        payload: dict,
    ) -> bool:
        if not isinstance(payload, dict):
            return False

        instrument = payload.get("instrument") or {}

        opening_range = payload.get("opening_range") or {}

        market_snapshot = payload.get("market_snapshot") or {}

        ema_data = payload.get("ema") or {}

        order_suggestion = payload.get("order_suggestion") or {}

        candle = ema_data.get("candle") or {}

        selected_state = {
            "instrument_key": (instrument.get("instrument_key")),
            "selected_level": (opening_range.get("selected_level")),
            "contract_info": {
                **instrument,
                "instrument_type": (instrument.get("instrument_type")),
            },
        }

        ema_event = {
            **ema_data,
            "instrument_key": (instrument.get("instrument_key")),
            "cross_type": (ema_data.get("cross_type")),
            "current_signal": (
                ema_data.get("current_signal") or ema_data.get("signal")
            ),
            "ema_calculation_mode": (ema_data.get("calculation_mode")),
            "close": candle.get("close"),
            "timestamp": (candle.get("timestamp") or ema_data.get("timestamp")),
            "candle": candle,
        }

        budget_filter = order_suggestion.get("budget_filter") or {}

        return self.send_selected_or_ema_cross_message(
            selected_state=selected_state,
            ema_event=ema_event,
            nifty_ltp=(market_snapshot.get("nifty_ltp")),
            suggested_order_instruments=(
                order_suggestion.get("nearest_instruments") or []
            ),
            budget_range_instruments=(budget_filter.get("instruments") or []),
        )

    # ============================================================
    # Compatibility Wrappers
    # ============================================================

    def send_isolated_instrument_message(
        self,
        isolated_state: dict,
    ) -> bool:
        isolated_state = isolated_state or {}

        contract_info = isolated_state.get("contract_info") or {}

        symbol = (
            contract_info.get("trading_symbol")
            or contract_info.get("instrument_key")
            or isolated_state.get("instrument_key")
            or "N/A"
        )

        return self.send_selected_or_instrument_message(
            instrument_key=(isolated_state.get("instrument_key")),
            symbol=symbol,
            level=(isolated_state.get("selected_level")),
            level_value=(isolated_state.get("level_value")),
            trigger_field=(isolated_state.get("trigger_field")),
            trigger_price=(isolated_state.get("trigger_price")),
            touch_time=(isolated_state.get("touch_time")),
            source=(isolated_state.get("touch_source")),
            nifty_ltp=(isolated_state.get("latest_main_index_ltp")),
            strike_price=(contract_info.get("strike_price")),
            instrument_type=(contract_info.get("instrument_type")),
            reference_average=(isolated_state.get("reference_average")),
            average_window=(isolated_state.get("average_window")),
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
            suggested_order_instruments=(suggested_order_instruments),
            budget_range_instruments=(budget_range_instruments),
        )

    # ============================================================
    # Exception Messages
    # ============================================================

    def send_exception_message(
        self,
        title: str,
        exception: Exception,
        context: str = "",
    ) -> bool:
        message = (
            f"Exception Type: "
            f"{type(exception).__name__}\n"
            f"Exception Message: {exception}"
        )

        if context:
            message = f"Context: {context}\n\n" f"{message}"

        return self.send_message(
            title=title,
            message=message,
            level="ERROR",
            notification_context=(
                f"exception|context={context}" if context else "exception"
            ),
        )


# ============================================================
# Service Instance
# ============================================================

telegram_service = TelegramService()
