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
    subscription, refresh, Opening Range job status, isolated instrument
    selection, isolated EMA crossover alerts, shutdown, and error notifications.

    Current behavior:
    - Normal main-flow Telegram notifications remain unchanged.
    - Sends Telegram notification when one Opening Range instrument is isolated.
    - Sends EMA Telegram alerts only for the isolated instrument.
    - Other instruments can continue live EMA processing and WebSocket broadcast,
      but should not trigger Telegram EMA alerts.

    Live EMA calculation mode:
    - LIVE_EMA_CALCULATION_MODE=False means completed candle close based EMA.
    - LIVE_EMA_CALCULATION_MODE=True means live tick/LTP based EMA.

    Order-side display behavior:
    - Opening Range service and Option service decide suggested order side.
    - Telegram service only displays isolated side, suggested side, and instruments.

    Isolated role display behavior:
    - If selected level is S2 or S3, isolated instrument role is SUPPORT.
    - If selected level is R2 or R3, isolated instrument role is RESISTANCE.
    - Opposite CE/PE instrument at same strike is displayed with opposite role.
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

    def _get_live_ema_calculation_mode(self) -> str:
        """
        Returns configured live EMA calculation mode.

        LIVE_EMA_CALCULATION_MODE=False:
            candle_close

        LIVE_EMA_CALCULATION_MODE=True:
            tick_ltp
        """

        tick_based_mode = bool(getattr(config, "LIVE_EMA_CALCULATION_MODE", False))

        return "tick_ltp" if tick_based_mode else "candle_close"

    def _get_live_ema_calculation_mode_description(
        self,
        mode: str | None = None,
    ) -> str:
        """Returns readable description for EMA calculation mode."""

        mode = mode or self._get_live_ema_calculation_mode()

        if mode == "tick_ltp":
            return "Live tick/LTP based EMA cross detection"

        return "Completed candle close based EMA cross detection"

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
            level: INFO, SUCCESS, WARNING, ERROR, STARTUP, REFRESH,
                   SUBSCRIPTION, TOKEN, INSTRUMENTS, EMA, OPENING_RANGE.
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

        return self._send_raw_message(formatted_message)

    # ========================================================
    # Application Lifecycle Messages
    # ========================================================

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

    # ========================================================
    # Token / Instrument / Subscription Messages
    # ========================================================

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

    # ========================================================
    # Refresh Messages
    # ========================================================

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

    # ========================================================
    # Opening Range Isolated Instrument Messages
    # ========================================================

    def send_selected_or_instrument_message(
        self,
        instrument_key: str,
        symbol: str,
        level: str,
        level_value,
        trigger_field: str,
        trigger_price,
        touch_time,
        source: str,
        nifty_ltp=None,
        strike_price=None,
        instrument_type=None,
        reference_average=None,
        average_window=None,
    ) -> bool:
        """
        Sends Telegram notification when an Opening Range instrument is isolated.

        Role logic:
        - S2/S3 selected level means isolated instrument is SUPPORT.
        - R2/R3 selected level means isolated instrument is RESISTANCE.
        """

        if not bool(
            getattr(config, "OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED", True)
        ):
            logger.info(
                "Isolated instrument Telegram notification skipped because "
                "OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED=False."
            )
            return False

        strike_text = strike_price if strike_price is not None else "N/A"
        type_text = self._normalize_option_type(instrument_type)
        selected_level = self._normalize_level(level)

        isolated_role = self._get_isolated_instrument_role(selected_level)
        opposite_type = self._get_opposite_option_type(type_text)
        opposite_role = self._get_opposite_instrument_role(isolated_role)
        opposite_symbol = self._build_opposite_symbol(strike_text, opposite_type)

        eligible_lower = "not_available"
        eligible_upper = "not_available"
        final_lower = "not_available"
        final_upper = "not_available"

        if isinstance(average_window, dict):
            eligible_lower = average_window.get(
                "eligible_lower",
                average_window.get("raw_lower", "not_available"),
            )
            eligible_upper = average_window.get(
                "eligible_upper",
                average_window.get("raw_upper", "not_available"),
            )
            final_lower = average_window.get("final_lower", "not_available")
            final_upper = average_window.get("final_upper", "not_available")

        instrument_name = self._format_instrument_name(strike_text, type_text, symbol)

        message = (
            f"Date: {self._format_date_from_value(touch_time)}\n"
            f"Instrument: {instrument_name}\n"
            f"Instrument Key: {instrument_key}\n\n"
            f"Isolated Instrument Type: {type_text}\n"
            f"Isolated Instrument Role: {isolated_role}\n"
            f"Role Rule: {self._get_role_rule_text(selected_level)}\n\n"
            f"Opposite Instrument Type: {opposite_type}\n"
            f"Opposite Instrument: {opposite_symbol}\n"
            f"Opposite Instrument Role: {opposite_role}\n\n"
            f"Selected Level: {selected_level}\n"
            f"Touch Source: {source}\n"
            f"Touch Price: {trigger_price}\n"
            f"Level Value: {level_value}\n"
            f"Trigger Field: {trigger_field}\n\n"
            f"Opening Range Average: {reference_average}\n"
            f"Eligible Strike Window: {eligible_lower} to {eligible_upper}\n"
            f"Final Strike Range: {final_lower} to {final_upper}\n\n"
            f"Latest NIFTY LTP: "
            f"{nifty_ltp if nifty_ltp is not None else 'not_available'}\n\n"
            "Status:\n"
            "This instrument is now isolated for EMA Telegram alerts.\n"
            "Live EMA continues for all instruments.\n"
            "Telegram EMA alerts will be sent only for this isolated instrument."
        )

        logger.info(
            f"Sending isolated instrument Telegram message. "
            f"instrument_key={instrument_key}, "
            f"selected_level={selected_level}, "
            f"instrument_type={type_text}, "
            f"isolated_role={isolated_role}, "
            f"opposite_type={opposite_type}, "
            f"opposite_role={opposite_role}"
        )

        return self.send_message(
            title="Opening Range Isolated Instrument Selected",
            message=message,
            level="OPENING_RANGE",
        )

    def send_selected_or_ema_cross_message(
        self,
        selected_state: dict,
        ema_event: dict,
        nifty_ltp=None,
        suggested_order_instruments: list | None = None,
        budget_order_instruments: list | None = None,
    ) -> bool:
        """
        Sends EMA crossover Telegram alert for isolated instrument only.

        This service displays suggested order instruments and budget range
        instruments. The actual order-side and budget filtering should be done
        before calling this method.
        """

        if not bool(getattr(config, "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED", True)):
            logger.info(
                "Isolated EMA Telegram notification skipped because "
                "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED=False."
            )
            return False

        selected_state = selected_state or {}
        ema_event = ema_event or {}
        suggested_order_instruments = suggested_order_instruments or []
        budget_order_instruments = budget_order_instruments or []

        contract_info = (
            selected_state.get("contract_info")
            or ema_event.get("contract_info")
            or ema_event.get("info")
            or {}
        )

        strike = contract_info.get("strike_price", "N/A")
        trading_symbol = (
            contract_info.get("trading_symbol")
            or contract_info.get("symbol")
            or selected_state.get("symbol")
            or ""
        )

        isolated_instrument_type = self._normalize_option_type(
            contract_info.get("instrument_type", "N/A")
        )

        selected_level = self._normalize_level(
            selected_state.get("selected_level", "N/A")
        )

        isolated_role = self._get_isolated_instrument_role(selected_level)
        opposite_type = self._get_opposite_option_type(isolated_instrument_type)
        opposite_role = self._get_opposite_instrument_role(isolated_role)

        instrument_key = ema_event.get("instrument_key") or selected_state.get(
            "instrument_key"
        )

        cross_type = ema_event.get("cross_type", "N/A")
        cross_type_text = str(cross_type or "").strip().lower()

        close_price = ema_event.get("close")
        event_timestamp = ema_event.get("timestamp")

        ema_mode = ema_event.get(
            "ema_calculation_mode",
            self._get_live_ema_calculation_mode(),
        )

        if ema_mode == "tick_ltp":
            price_label = "Live Tick LTP"
            time_label = "Tick Time"
        else:
            price_label = "EMA Candle Close"
            time_label = "EMA Candle Time"

        nifty_text = nifty_ltp if nifty_ltp is not None else "NIFTY_LTP_NOT_AVAILABLE"

        suggested_order_option_type = self._resolve_suggested_order_type(
            suggested_order_instruments=suggested_order_instruments,
            isolated_instrument_type=isolated_instrument_type,
            cross_type=cross_type_text,
        )

        opening_range_context_text = self._format_opening_range_context(
            ema_event=ema_event,
            selected_state=selected_state,
        )

        order_details_text = self._format_suggested_order_instruments(
            suggested_order_instruments
        )

        budget_details_text = self._format_budget_order_instruments(
            budget_order_instruments
        )

        title = self._get_ema_alert_title(cross_type_text)

        message = (
            f"Instrument: "
            f"{self._format_instrument_name(strike, isolated_instrument_type, trading_symbol)}\n"
            f"Instrument Key: {instrument_key}\n"
            f"Isolated Instrument Type: {isolated_instrument_type}\n"
            f"Isolated Instrument Role: {isolated_role}\n"
            f"Opposite Instrument Type: {opposite_type}\n"
            f"Opposite Instrument Role: {opposite_role}\n"
            f"Selected Opening Range Level: {selected_level}\n\n"
            f"EMA Cross: {cross_type}\n\n"
            f"NIFTY LTP: {nifty_text}\n"
            f"{price_label}: {close_price}\n"
            f"{time_label}: {event_timestamp}\n\n"
            f"{opening_range_context_text}\n\n"
            f"Touched Level: {selected_level}\n\n"
            f"Suggested Order Side: {suggested_order_option_type}\n\n"
            f"{order_details_text}\n\n"
            f"{budget_details_text}\n\n"
            "Alert Scope: isolated_instrument_only\n"
            f"EMA Mode: {ema_mode}"
        )

        logger.info(
            f"Sending isolated EMA Telegram message. "
            f"instrument_key={instrument_key}, "
            f"cross_type={cross_type}, "
            f"isolated_instrument_type={isolated_instrument_type}, "
            f"selected_level={selected_level}, "
            f"isolated_role={isolated_role}, "
            f"suggested_order_option_type={suggested_order_option_type}, "
            f"suggested_instruments_count={len(suggested_order_instruments)}, "
            f"budget_instruments_count={len(budget_order_instruments)}"
        )

        return self.send_message(
            title=title,
            message=message,
            level="EMA",
        )

    def send_isolated_instrument_message(
        self,
        isolated_state: dict,
    ) -> bool:
        """
        Convenience wrapper for isolated instrument notification.

        This allows opening_range_service.py to pass the full isolated state
        instead of individual fields.
        """

        isolated_state = isolated_state or {}
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
        nifty_ltp=None,
        suggested_order_instruments: list | None = None,
        budget_order_instruments: list | None = None,
    ) -> bool:
        """
        Convenience wrapper for isolated instrument EMA alert.
        """

        return self.send_selected_or_ema_cross_message(
            selected_state=isolated_state,
            ema_event=ema_event,
            nifty_ltp=nifty_ltp,
            suggested_order_instruments=suggested_order_instruments,
            budget_order_instruments=budget_order_instruments,
        )

    # ========================================================
    # Role / Type Helpers
    # ========================================================

    def _normalize_option_type(self, value) -> str:
        """Normalizes option type to CE/PE."""

        text = str(value or "N/A").strip().upper()

        if text == "CALL":
            return "CE"

        if text == "PUT":
            return "PE"

        if text in ["CE", "PE"]:
            return text

        return text or "N/A"

    def _normalize_level(self, value) -> str:
        """Normalizes Opening Range level text."""

        return str(value or "N/A").strip().upper()

    def _get_opposite_option_type(self, option_type: str) -> str:
        """Returns opposite option type for CE/PE."""

        option_type = self._normalize_option_type(option_type)

        if option_type == "CE":
            return "PE"

        if option_type == "PE":
            return "CE"

        return "not_available"

    def _get_isolated_instrument_role(self, selected_level: str) -> str:
        """
        Returns isolated instrument role based on selected level.

        S2/S3 -> SUPPORT
        R2/R3 -> RESISTANCE
        """

        selected_level = self._normalize_level(selected_level)

        support_levels = [
            str(item).upper()
            for item in getattr(
                config,
                "OPENING_RANGE_ISOLATED_SUPPORT_LEVELS",
                ["S2", "S3"],
            )
        ]

        resistance_levels = [
            str(item).upper()
            for item in getattr(
                config,
                "OPENING_RANGE_ISOLATED_RESISTANCE_LEVELS",
                ["R2", "R3"],
            )
        ]

        if selected_level in support_levels:
            return getattr(
                config,
                "OPENING_RANGE_ISOLATED_SUPPORT_ROLE_TEXT",
                "SUPPORT",
            )

        if selected_level in resistance_levels:
            return getattr(
                config,
                "OPENING_RANGE_ISOLATED_RESISTANCE_ROLE_TEXT",
                "RESISTANCE",
            )

        return "not_available"

    def _get_opposite_instrument_role(self, isolated_role: str) -> str:
        """Returns opposite instrument role text."""

        isolated_role = str(isolated_role or "").strip().upper()

        support_text = str(
            getattr(config, "OPENING_RANGE_ISOLATED_SUPPORT_ROLE_TEXT", "SUPPORT")
        ).upper()

        resistance_text = str(
            getattr(
                config,
                "OPENING_RANGE_ISOLATED_RESISTANCE_ROLE_TEXT",
                "RESISTANCE",
            )
        ).upper()

        if isolated_role == support_text:
            return getattr(
                config,
                "OPENING_RANGE_OPPOSITE_WHEN_ISOLATED_SUPPORT_ROLE_TEXT",
                "RESISTANCE-LIKE",
            )

        if isolated_role == resistance_text:
            return getattr(
                config,
                "OPENING_RANGE_OPPOSITE_WHEN_ISOLATED_RESISTANCE_ROLE_TEXT",
                "SUPPORT-LIKE",
            )

        return "not_available"

    def _get_role_rule_text(self, selected_level: str) -> str:
        """Returns readable role rule text for isolated instrument message."""

        selected_level = self._normalize_level(selected_level)

        support_levels = [
            str(item).upper()
            for item in getattr(
                config,
                "OPENING_RANGE_ISOLATED_SUPPORT_LEVELS",
                ["S2", "S3"],
            )
        ]

        resistance_levels = [
            str(item).upper()
            for item in getattr(
                config,
                "OPENING_RANGE_ISOLATED_RESISTANCE_LEVELS",
                ["R2", "R3"],
            )
        ]

        if selected_level in support_levels:
            return (
                f"This instrument touched {selected_level}, "
                "so it is treated as SUPPORT."
            )

        if selected_level in resistance_levels:
            return (
                f"This instrument touched {selected_level}. "
                "Only S2/S3 touches are treated as SUPPORT, "
                "so this instrument is treated as RESISTANCE."
            )

        return "Role could not be resolved because selected level is unavailable."

    def _build_opposite_symbol(self, strike_price=None, opposite_type=None) -> str:
        """Builds opposite instrument display name."""

        if strike_price in [None, "", "N/A"] or not opposite_type:
            return "not_available"

        return f"NIFTY {strike_price} {opposite_type}"

    def _format_instrument_name(
        self,
        strike,
        instrument_type,
        fallback_symbol=None,
    ) -> str:
        """Formats NIFTY option instrument display name."""

        instrument_type = self._normalize_option_type(instrument_type)

        if strike not in [None, "", "N/A"] and instrument_type in ["CE", "PE"]:
            return f"NIFTY {strike} {instrument_type}"

        if fallback_symbol:
            return str(fallback_symbol)

        return f"{strike} {instrument_type}"

    def _format_date_from_value(self, value) -> str:
        """Extracts date text from touch time when available."""

        if not value:
            return datetime.now(self.market_timezone).date().isoformat()

        text = str(value)

        if "T" in text:
            return text.split("T")[0]

        if " " in text:
            return text.split(" ")[0]

        if len(text) >= 10:
            return text[:10]

        return datetime.now(self.market_timezone).date().isoformat()

    # ========================================================
    # EMA Formatting Helpers
    # ========================================================

    def _get_ema_alert_title(self, cross_type_text: str) -> str:
        """Returns Telegram title for EMA alert."""

        cross_type_text = str(cross_type_text or "").lower()

        if "bullish" in cross_type_text:
            return "Isolated EMA Bullish Cross Alert"

        if "bearish" in cross_type_text:
            return "Isolated EMA Bearish Cross Alert"

        return "Isolated Instrument EMA Alert"

    def _resolve_suggested_order_type(
        self,
        suggested_order_instruments: list,
        isolated_instrument_type: str,
        cross_type: str,
    ) -> str:
        """
        Resolves suggested order side.

        Priority:
        1. Use first suggested_order_instruments type if available.
        2. Fallback to cross type rule.
        """

        for item in suggested_order_instruments or []:
            if not isinstance(item, dict):
                continue

            candidate_type = self._normalize_option_type(
                item.get("instrument_type", "")
            )

            if candidate_type in ["CE", "PE"]:
                return candidate_type

        isolated_instrument_type = self._normalize_option_type(isolated_instrument_type)
        cross_type = str(cross_type or "").strip().lower()

        if "bullish" in cross_type:
            return isolated_instrument_type

        if "bearish" in cross_type:
            return self._get_opposite_option_type(isolated_instrument_type)

        return "not_available"

    def _format_opening_range_context(
        self,
        ema_event: dict,
        selected_state: dict,
    ) -> str:
        """Formats Opening Range context for EMA alert."""

        opening_range = ema_event.get("opening_range") or {}
        levels = {}

        if isinstance(opening_range, dict):
            levels = opening_range.get("levels") or {}

        if not levels and isinstance(selected_state, dict):
            levels = selected_state.get("levels") or {}

        r3 = self._get_level_value(levels, "r3", "R3", "resistance3")
        r2 = self._get_level_value(levels, "r2", "R2", "resistance2")
        s2 = self._get_level_value(levels, "s2", "S2", "support2")
        s3 = self._get_level_value(levels, "s3", "S3", "support3")

        return (
            "Opening Range Context:\n"
            f"R3: {r3}\n"
            f"R2: {r2}\n"
            f"S2: {s2}\n"
            f"S3: {s3}"
        )

    def _get_level_value(self, levels: dict, *keys):
        """Returns level value by trying multiple key names."""

        if not isinstance(levels, dict):
            return "not_available"

        for key in keys:
            if key in levels:
                return levels.get(key)

        return "not_available"

    def _format_suggested_order_instruments(
        self,
        suggested_order_instruments: list,
    ) -> str:
        """
        Formats nearest CE/PE instruments for EMA alert.

        Example:
            Nearest Order Instruments:
            - NIFTY 24300 PE - 120 rs
            - NIFTY 24350 PE - 135 rs
            - NIFTY 24400 PE - 150 rs
        """

        if not suggested_order_instruments:
            return "Nearest Order Instruments: not_available"

        lines = ["Nearest Order Instruments:"]

        for item in suggested_order_instruments:
            if not isinstance(item, dict):
                continue

            strike = item.get("strike_price", "N/A")
            instrument_type = self._normalize_option_type(
                item.get("instrument_type", "N/A")
            )

            live_ltp = (
                item.get("live_ltp")
                if item.get("live_ltp") is not None
                else item.get("ltp")
            )

            if live_ltp is None:
                price_text = "ltp_not_available"
            else:
                price_text = f"{live_ltp} rs"

            lines.append(f"- NIFTY {strike} {instrument_type} - {price_text}")

        if len(lines) == 1:
            return "Nearest Order Instruments: not_available"

        return "\n".join(lines)

    def _format_budget_order_instruments(
        self,
        budget_order_instruments: list,
    ) -> str:
        """
        Formats budget range order instruments.

        These instruments should be pre-filtered by option_service using:
        - suggested order side
        - LTP between budget min and max
        - nearest strike to current NIFTY LTP
        """

        budget_enabled = bool(getattr(config, "EMA_ALERT_BUDGET_ORDER_ENABLED", False))

        if not budget_enabled:
            return "Budget Range Instruments: disabled"

        min_price = getattr(config, "EMA_ALERT_BUDGET_MIN_PRICE", 20.0)
        max_price = getattr(config, "EMA_ALERT_BUDGET_MAX_PRICE", 30.0)

        lines = [
            f"Budget Range Instruments: {min_price} rs to {max_price} rs",
            (
                "Selection Rule: Suggested order side only, "
                f"LTP between {min_price} and {max_price} rs, "
                "nearest to current NIFTY LTP."
            ),
        ]

        if not budget_order_instruments:
            empty_message = getattr(
                config,
                "EMA_ALERT_BUDGET_EMPTY_MESSAGE",
                "No budget range instruments found for configured price range.",
            )
            lines.append("")
            lines.append(empty_message)
            return "\n".join(lines)

        lines.append("")

        for item in budget_order_instruments:
            if not isinstance(item, dict):
                continue

            strike = item.get("strike_price", "N/A")
            instrument_type = self._normalize_option_type(
                item.get("instrument_type", "N/A")
            )

            live_ltp = (
                item.get("live_ltp")
                if item.get("live_ltp") is not None
                else item.get("ltp")
            )

            if live_ltp is None:
                price_text = "ltp_not_available"
            else:
                price_text = f"{live_ltp} rs"

            lines.append(f"- NIFTY {strike} {instrument_type} - {price_text}")

        return "\n".join(lines)

    def send_telegram_message(
        title: str,
        message: str,
        level: str = "INFO",
    ) -> bool:
        return telegram_service.send_message(
            title=title,
            message=message,
            level=level,
        )

    # ========================================================
    # Exception Messages
    # ========================================================

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
