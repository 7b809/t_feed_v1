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

        Selection logic is handled outside this service:
        - Average +/- configured window.
        - R3/S3 priority before R2/S2.
        - Nearest strike to Opening Range average.
        - Day-level isolated instrument.
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
        type_text = str(instrument_type or "N/A").upper()

        if type_text == "CALL":
            type_text = "CE"
        elif type_text == "PUT":
            type_text = "PE"

        window_text = "not_available"

        if isinstance(average_window, dict):
            window_text = (
                f"{average_window.get('final_lower')} to "
                f"{average_window.get('final_upper')}"
            )

        ema_mode = self._get_live_ema_calculation_mode()
        ema_mode_description = self._get_live_ema_calculation_mode_description(ema_mode)

        message = (
            "Opening Range instrument isolated for EMA Telegram alerts.\n\n"
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
            f"EMA Calculation Mode: {ema_mode}\n"
            f"EMA Mode Description: {ema_mode_description}\n\n"
            "From now, Telegram EMA alerts will be sent only for this isolated instrument."
        )

        return self.send_message(
            title="Opening Range Instrument Isolated",
            message=message,
            level="OPENING_RANGE",
        )

    def send_selected_or_ema_cross_message(
        self,
        selected_state: dict,
        ema_event: dict,
        nifty_ltp=None,
        suggested_order_instruments: list | None = None,
    ) -> bool:
        """
        Sends EMA crossover Telegram alert for isolated instrument only.

        This service does not decide CE/PE order side.
        Order side is decided before this method is called.

        Expected order-side rule from opening_range_service/option_service:
            bullish_cross:
                Same side as isolated instrument.

            bearish_cross:
                Opposite side of isolated instrument.

        Examples:
            Isolated CE + bullish_cross -> suggested CE instruments.
            Isolated CE + bearish_cross -> suggested PE instruments.
            Isolated PE + bullish_cross -> suggested PE instruments.
            Isolated PE + bearish_cross -> suggested CE instruments.

        Suggested strikes are selected around current NIFTY spot/LTP.
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

        contract_info = (
            selected_state.get("contract_info")
            or ema_event.get("contract_info")
            or ema_event.get("info")
            or {}
        )

        strike = contract_info.get("strike_price", "N/A")

        isolated_instrument_type = (
            str(contract_info.get("instrument_type", "N/A")).strip().upper()
        )

        if isolated_instrument_type == "CALL":
            isolated_instrument_type = "CE"
        elif isolated_instrument_type == "PUT":
            isolated_instrument_type = "PE"

        selected_level = selected_state.get("selected_level", "N/A")

        instrument_key = ema_event.get("instrument_key") or selected_state.get(
            "instrument_key"
        )

        cross_type = ema_event.get("cross_type", "N/A")
        current_signal = ema_event.get("current_signal", "N/A")
        close_price = ema_event.get("close")
        event_timestamp = ema_event.get("timestamp")

        ema_fast = ema_event.get("ema_fast")
        ema_slow = ema_event.get("ema_slow")
        previous_ema_fast = ema_event.get("previous_ema_fast")
        previous_ema_slow = ema_event.get("previous_ema_slow")

        ema_fast_period = ema_event.get(
            "ema_fast_period",
            getattr(config, "LIVE_EMA_FAST_PERIOD", 9),
        )
        ema_slow_period = ema_event.get(
            "ema_slow_period",
            getattr(config, "LIVE_EMA_SLOW_PERIOD", 21),
        )

        ema_mode = ema_event.get(
            "ema_calculation_mode",
            self._get_live_ema_calculation_mode(),
        )

        ema_mode_description = self._get_live_ema_calculation_mode_description(ema_mode)

        source = ema_event.get("source", "live_feed")

        if ema_mode == "tick_ltp":
            price_label = "Live Tick LTP"
            time_label = "Tick Time"
        else:
            price_label = "EMA Candle Close"
            time_label = "EMA Candle Time"

        nifty_text = nifty_ltp if nifty_ltp is not None else "NIFTY_LTP_NOT_AVAILABLE"

        suggested_order_option_type = None

        for item in suggested_order_instruments:
            if not isinstance(item, dict):
                continue

            candidate_type = str(item.get("instrument_type", "")).strip().upper()

            if candidate_type in ["CE", "CALL"]:
                suggested_order_option_type = "CE"
                break

            if candidate_type in ["PE", "PUT"]:
                suggested_order_option_type = "PE"
                break

        suggested_order_type_text = (
            suggested_order_option_type
            if suggested_order_option_type
            else "not_available"
        )

        cross_text = str(cross_type or "").strip().lower()

        if "bullish" in cross_text:
            order_side_rule = (
                "Bullish cross uses the same side as the isolated instrument."
            )
        elif "bearish" in cross_text:
            order_side_rule = (
                "Bearish cross uses the opposite side of the isolated instrument."
            )
        else:
            order_side_rule = (
                "Order side could not be resolved because EMA cross type is unknown."
            )

        header = (
            f"{strike} {isolated_instrument_type} - "
            f"crosses {selected_level} - "
            f"At {nifty_text}"
        )

        order_details_text = self._format_suggested_order_instruments(
            suggested_order_instruments
        )

        message = (
            f"{header}\n\n"
            "EMA Cross Details:\n"
            f"Cross Type: {cross_type}\n"
            f"Signal: {current_signal}\n"
            f"Isolated Instrument Type: {isolated_instrument_type}\n"
            f"Suggested Order Side: {suggested_order_type_text}\n"
            f"Order Side Rule: {order_side_rule}\n"
            f"EMA Calculation Mode: {ema_mode}\n"
            f"EMA Mode Description: {ema_mode_description}\n"
            f"Source: {source}\n"
            f"{price_label}: {close_price}\n"
            f"{time_label}: {event_timestamp}\n"
            f"EMA Fast Period: {ema_fast_period}\n"
            f"EMA Slow Period: {ema_slow_period}\n"
            f"EMA Fast: {ema_fast}\n"
            f"EMA Slow: {ema_slow}\n"
            f"Previous EMA Fast: {previous_ema_fast}\n"
            f"Previous EMA Slow: {previous_ema_slow}\n"
            f"Instrument Key: {instrument_key}\n\n"
            f"{order_details_text}"
        )

        logger.info(
            f"Sending isolated EMA Telegram message. "
            f"instrument_key={instrument_key}, "
            f"cross_type={cross_type}, "
            f"isolated_instrument_type={isolated_instrument_type}, "
            f"suggested_order_option_type={suggested_order_option_type}, "
            f"suggested_instruments_count={len(suggested_order_instruments)}"
        )

        return self.send_message(
            title="Isolated Instrument EMA Alert",
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
    ) -> bool:
        """
        Convenience wrapper for isolated instrument EMA alert.
        """

        return self.send_selected_or_ema_cross_message(
            selected_state=isolated_state,
            ema_event=ema_event,
            nifty_ltp=nifty_ltp,
            suggested_order_instruments=suggested_order_instruments,
        )

    # ========================================================
    # Formatting Helpers
    # ========================================================

    def _format_suggested_order_instruments(
        self,
        suggested_order_instruments: list,
    ) -> str:
        """
        Formats nearest CE/PE instruments for EMA alert.

        Example:
            Nearest Instrument Details:
            - 24300PE - 120rs
            - 24350PE - 135rs
            - 24400PE - 150rs
        """

        if not suggested_order_instruments:
            return "Nearest Instrument Details: not_available"

        lines = ["Nearest Instrument Details:"]

        for item in suggested_order_instruments:
            if not isinstance(item, dict):
                continue

            strike = item.get("strike_price", "N/A")
            instrument_type = str(item.get("instrument_type", "N/A")).upper()
            live_ltp = item.get("live_ltp")

            if live_ltp is None:
                price_text = "ltp_not_available"
            else:
                price_text = f"{live_ltp}rs"

            lines.append(f"- {strike}{instrument_type} - {price_text}")

        if len(lines) == 1:
            return "Nearest Instrument Details: not_available"

        return "\n".join(lines)

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
