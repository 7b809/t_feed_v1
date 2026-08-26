import html
from datetime import datetime
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
            getattr(config, "TELEGRAM_TIMEOUT_SECONDS", 10)
        )

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

    # ========================================================
    # Time / Configuration
    # ========================================================

    def _load_market_timezone(self):
        """Loads market timezone from config, defaulting to Asia/Kolkata."""

        timezone_name = getattr(
            config,
            "MARKET_TIMEZONE",
            "Asia/Kolkata",
        )

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

        return datetime.now(
            self.market_timezone
        ).strftime(
            self.market_time_format
        )

    def is_configured(self) -> bool:
        """Returns True if Telegram service has required configuration."""

        return bool(
            self.enabled
            and self.bot_token
            and self.chat_id
            and self.api_url
        )

    # ========================================================
    # Formatting Helpers
    # ========================================================

    def _escape(self, value) -> str:
        """Escapes text for Telegram HTML parse mode."""

        return html.escape(
            str(value),
            quote=False,
        )

    def _get_live_ema_calculation_mode(self) -> str:
        """
        Returns configured live EMA calculation mode.

        LIVE_EMA_CALCULATION_MODE=False:
            candle_close

        LIVE_EMA_CALCULATION_MODE=True:
            tick_ltp
        """

        tick_based_mode = bool(
            getattr(
                config,
                "LIVE_EMA_CALCULATION_MODE",
                False,
            )
        )

        return (
            "tick_ltp"
            if tick_based_mode
            else "candle_close"
        )

    def _get_live_ema_calculation_mode_description(
        self,
        mode: str | None = None,
    ) -> str:
        """Returns readable description for EMA calculation mode."""

        mode = (
            mode
            or self._get_live_ema_calculation_mode()
        )

        if mode == "tick_ltp":
            return (
                "Live tick/LTP based EMA cross detection"
            )

        return (
            "Completed candle close based EMA cross detection"
        )

    # ========================================================
    # Raw Telegram Send
    # ========================================================

    def _send_raw_message(
        self,
        message: str,
        *,
        notification_title: str = "Unknown",
        notification_level: str = "INFO",
        notification_context: str = "",
    ) -> bool:
        """
        Sends raw HTML message to Telegram.

        Returns:
            True if message was sent successfully.
            False otherwise.

        notification_title / notification_level / notification_context
        are used only for diagnostics and logging.
        """

        # ----------------------------------------------------
        # Configuration Check
        # ----------------------------------------------------

        if not self.is_configured():

            logger.warning(
                "Telegram notification skipped. "
                "service_configured=False, "
                "title=%s, "
                "level=%s, "
                "context=%s",
                notification_title,
                notification_level,
                notification_context or "not_available",
            )

            return False

        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        # ----------------------------------------------------
        # Send Telegram Message
        # ----------------------------------------------------

        logger.info(
            "Sending Telegram notification. "
            "title=%s, "
            "level=%s, "
            "context=%s",
            notification_title,
            notification_level,
            notification_context or "not_available",
        )

        try:

            response = requests.post(
                self.api_url,
                json=payload,
                timeout=self.timeout_seconds,
            )

            # ------------------------------------------------
            # Failed HTTP Response
            # ------------------------------------------------

            if response.status_code != 200:

                logger.error(
                    "Telegram notification failed. "
                    "title=%s, "
                    "level=%s, "
                    "context=%s, "
                    "status_code=%s, "
                    "response=%s",
                    notification_title,
                    notification_level,
                    notification_context or "not_available",
                    response.status_code,
                    response.text,
                )

                return False

            # ------------------------------------------------
            # Successful Telegram Send
            # ------------------------------------------------

            logger.info(
                "Telegram notification sent successfully. "
                "title=%s, "
                "level=%s, "
                "context=%s, "
                "status_code=%s",
                notification_title,
                notification_level,
                notification_context or "not_available",
                response.status_code,
            )

            return True

        except Exception as ex:

            logger.error(
                "Telegram notification exception. "
                "title=%s, "
                "level=%s, "
                "context=%s, "
                "exception_type=%s, "
                "error=%s",
                notification_title,
                notification_level,
                notification_context or "not_available",
                type(ex).__name__,
                ex,
            )

            return False

    # ========================================================
    # Generic Telegram Message
    # ========================================================

    def send_message(
        self,
        title: str,
        message: str,
        level: str = "INFO",
        notification_context: str = "",
    ) -> bool:
        """
        Sends a formatted Telegram notification.

        Args:
            title:
                Notification title.

            message:
                Notification body.

            level:
                INFO, SUCCESS, WARNING, ERROR, STARTUP,
                REFRESH, SUBSCRIPTION, TOKEN, INSTRUMENTS,
                EMA, OPENING_RANGE.

            notification_context:
                Optional diagnostic context.

                This is NOT sent to Telegram.
                It is only written to telegram_service.log.
        """

        level_upper = str(
            level or "INFO"
        ).upper()

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
        market_time = self._escape(
            self._now_market_time()
        )

        formatted_message = (
            f"{emoji} <b>{safe_title}</b>\n\n"
            f"{safe_message}\n\n"
            f"<b>Level:</b> "
            f"{self._escape(level_upper)}\n"
            f"<b>Time:</b> "
            f"{market_time}"
        )

        return self._send_raw_message(
            formatted_message,
            notification_title=title,
            notification_level=level_upper,
            notification_context=notification_context,
        )

    # ========================================================
    # Application Lifecycle Messages
    # ========================================================

    def send_startup_message(
        self,
        status: str,
        details: str = "",
    ) -> bool:
        """Sends application startup notification."""

        message = (
            f"Application startup status: {status}"
        )

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
        """Sends application shutdown notification."""

        message = (
            "Application shutdown sequence executed."
        )

        if details:
            message += f"\n\n{details}"

        return self.send_message(
            title="Option Feed Engine Shutdown",
            message=message,
            level="SHUTDOWN",
            notification_context="application_shutdown",
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

            message = (
                "Access token document refreshed "
                "successfully from MongoDB."
            )

            if updated_at:
                message += (
                    f"\nToken Updated At: {updated_at}"
                )

            return self.send_message(
                title="Token Refresh Successful",
                message=message,
                level="TOKEN",
                notification_context="token_refresh_success",
            )

        message = (
            "Access token refresh failed."
        )

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
                "Option instruments fetched and cache "
                "updated successfully.\n\n"
                f"Nearest Expiry: {nearest_expiry}\n"
                f"Total Contracts: {total_contracts}\n"
                f"Subscribed Keys: {subscribed_keys_count}\n"
                f"Strike Range: "
                f"{strike_from} to {strike_to}"
            )

            return self.send_message(
                title="Instruments Fetch Successful",
                message=message,
                level="INSTRUMENTS",
                notification_context="instruments_fetch_success",
            )

        message = (
            "Option instruments fetch failed."
        )

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
        subscribed_keys_count=0,
        feed_mode=None,
        error: str = "",
    ) -> bool:
        """Sends Upstox subscription or streamer restart notification."""

        if success:

            message = (
                "Upstox streamer subscription is active.\n\n"
                f"Subscribed Instruments: "
                f"{subscribed_keys_count}\n"
                f"Feed Mode: {feed_mode}"
            )

            return self.send_message(
                title="Feed Subscription Successful",
                message=message,
                level="SUBSCRIPTION",
                notification_context="feed_subscription_success",
            )

        message = (
            "Upstox feed subscription failed."
        )

        if error:
            message += f"\nError: {error}"

        return self.send_message(
            title="Feed Subscription Failed",
            message=message,
            level="ERROR",
            notification_context="feed_subscription_failed",
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
                "Daily market hard refresh completed "
                "successfully.\n\n"
                f"Nearest Expiry: {nearest_expiry}\n"
                f"Subscribed Instruments: "
                f"{subscribed_keys_count}"
            )

            return self.send_message(
                title="Daily Market Hard Refresh Successful",
                message=message,
                level="REFRESH",
                notification_context="daily_refresh_success",
            )

        message = (
            "Daily market hard refresh failed."
        )

        if error:
            message += f"\nError: {error}"

        return self.send_message(
            title="Daily Market Hard Refresh Failed",
            message=message,
            level="ERROR",
            notification_context="daily_refresh_failed",
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
        Sends Telegram notification when an Opening Range
        instrument is isolated.

        Selection logic is handled outside this service:

        - Average +/- configured window.
        - R3/S3 priority before R2/S2.
        - Nearest strike to Opening Range average.
        - Day-level isolated instrument.

        This method now provides detailed Telegram-service
        logging so we can verify exactly whether this alert
        was sent.
        """

        # ----------------------------------------------------
        # Feature Flag
        # ----------------------------------------------------

        if not bool(
            getattr(
                config,
                "OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED",
                True,
            )
        ):

            logger.info(
                "Isolated instrument Telegram notification skipped. "
                "reason=OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED=False, "
                "instrument_key=%s, "
                "level=%s, "
                "strike=%s, "
                "instrument_type=%s",
                instrument_key,
                level,
                strike_price,
                instrument_type,
            )

            return False

        # ----------------------------------------------------
        # Normalize Instrument Details
        # ----------------------------------------------------

        strike_text = (
            strike_price
            if strike_price is not None
            else "N/A"
        )

        type_text = str(
            instrument_type or "N/A"
        ).upper()

        if type_text == "CALL":
            type_text = "CE"

        elif type_text == "PUT":
            type_text = "PE"

        window_text = "not_available"

        if isinstance(average_window, dict):

            window_text = (
                f"{average_window.get('final_lower')} "
                f"to "
                f"{average_window.get('final_upper')}"
            )

        ema_mode = (
            self._get_live_ema_calculation_mode()
        )

        ema_mode_description = (
            self._get_live_ema_calculation_mode_description(
                ema_mode
            )
        )

        # ----------------------------------------------------
        # Build Telegram Message
        # ----------------------------------------------------

        message = (
            "Opening Range instrument isolated for "
            "EMA Telegram alerts.\n\n"
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
            f"NIFTY LTP: "
            f"{nifty_ltp if nifty_ltp is not None else 'not_available'}\n"
            f"EMA Calculation Mode: {ema_mode}\n"
            f"EMA Mode Description: "
            f"{ema_mode_description}\n\n"
            "From now, Telegram EMA alerts will be "
            "sent only for this isolated instrument."
        )

        # ----------------------------------------------------
        # IMPORTANT DEBUG LOG
        # ----------------------------------------------------

        logger.info(
            "Sending isolated instrument Telegram alert. "
            "instrument_key=%s, "
            "symbol=%s, "
            "selected_level=%s, "
            "level_value=%s, "
            "trigger_field=%s, "
            "trigger_price=%s, "
            "touch_time=%s, "
            "touch_source=%s, "
            "strike=%s, "
            "instrument_type=%s, "
            "reference_average=%s",
            instrument_key,
            symbol,
            level,
            level_value,
            trigger_field,
            trigger_price,
            touch_time,
            source,
            strike_text,
            type_text,
            reference_average,
        )

        # ----------------------------------------------------
        # Send
        # ----------------------------------------------------

        result = self.send_message(
            title="Opening Range Instrument Isolated",
            message=message,
            level="OPENING_RANGE",
            notification_context=(
                "isolated_instrument"
                f"|instrument_key={instrument_key}"
                f"|strike={strike_text}"
                f"|type={type_text}"
                f"|level={level}"
            ),
        )

        # ----------------------------------------------------
        # Result Log
        # ----------------------------------------------------

        if result:

            logger.info(
                "Isolated instrument Telegram alert "
                "sent successfully. "
                "instrument_key=%s, "
                "strike=%s, "
                "instrument_type=%s, "
                "level=%s",
                instrument_key,
                strike_text,
                type_text,
                level,
            )

        else:

            logger.error(
                "Isolated instrument Telegram alert "
                "FAILED. "
                "instrument_key=%s, "
                "strike=%s, "
                "instrument_type=%s, "
                "level=%s",
                instrument_key,
                strike_text,
                type_text,
                level,
            )

        return result

    # ========================================================
    # Isolated Instrument EMA Alert
    # ========================================================

    def send_selected_or_ema_cross_message(
        self,
        selected_state: dict,
        ema_event: dict,
        nifty_ltp=None,
        suggested_order_instruments: list | None = None,
    ) -> bool:
        """
        Sends EMA crossover Telegram alert for isolated
        instrument only.

        This service does not decide CE/PE order side.
        Order side is decided before this method is called.

        Expected order-side rule:

            bullish_cross:
                Same side as isolated instrument.

            bearish_cross:
                Opposite side of isolated instrument.
        """

        if not bool(
            getattr(
                config,
                "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED",
                True,
            )
        ):

            logger.info(
                "Isolated EMA Telegram notification skipped because "
                "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED=False."
            )

            return False

        selected_state = selected_state or {}
        ema_event = ema_event or {}
        suggested_order_instruments = (
            suggested_order_instruments or []
        )

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

        isolated_instrument_type = str(
            contract_info.get(
                "instrument_type",
                "N/A",
            )
        ).strip().upper()

        if isolated_instrument_type == "CALL":
            isolated_instrument_type = "CE"

        elif isolated_instrument_type == "PUT":
            isolated_instrument_type = "PE"

        selected_level = selected_state.get(
            "selected_level",
            "N/A",
        )

        instrument_key = (
            ema_event.get("instrument_key")
            or selected_state.get("instrument_key")
        )

        cross_type = ema_event.get(
            "cross_type",
            "N/A",
        )

        current_signal = ema_event.get(
            "current_signal",
            "N/A",
        )

        close_price = ema_event.get(
            "close"
        )

        event_timestamp = ema_event.get(
            "timestamp"
        )

        ema_fast = ema_event.get(
            "ema_fast"
        )

        ema_slow = ema_event.get(
            "ema_slow"
        )

        previous_ema_fast = ema_event.get(
            "previous_ema_fast"
        )

        previous_ema_slow = ema_event.get(
            "previous_ema_slow"
        )

        ema_fast_period = ema_event.get(
            "ema_fast_period",
            getattr(
                config,
                "LIVE_EMA_FAST_PERIOD",
                9,
            ),
        )

        ema_slow_period = ema_event.get(
            "ema_slow_period",
            getattr(
                config,
                "LIVE_EMA_SLOW_PERIOD",
                21,
            ),
        )

        ema_mode = ema_event.get(
            "ema_calculation_mode",
            self._get_live_ema_calculation_mode(),
        )

        ema_mode_description = (
            self._get_live_ema_calculation_mode_description(
                ema_mode
            )
        )

        source = ema_event.get(
            "source",
            "live_feed",
        )

        if ema_mode == "tick_ltp":

            price_label = "Live Tick LTP"
            time_label = "Tick Time"

        else:

            price_label = "EMA Candle Close"
            time_label = "EMA Candle Time"

        nifty_text = (
            nifty_ltp
            if nifty_ltp is not None
            else "NIFTY_LTP_NOT_AVAILABLE"
        )

        suggested_order_option_type = None

        for item in suggested_order_instruments:

            if not isinstance(item, dict):
                continue

            candidate_type = str(
                item.get(
                    "instrument_type",
                    "",
                )
            ).strip().upper()

            if candidate_type in [
                "CE",
                "CALL",
            ]:

                suggested_order_option_type = "CE"
                break

            if candidate_type in [
                "PE",
                "PUT",
            ]:

                suggested_order_option_type = "PE"
                break

        suggested_order_type_text = (
            suggested_order_option_type
            if suggested_order_option_type
            else "not_available"
        )

        cross_text = str(
            cross_type or ""
        ).strip().lower()

        if "bullish" in cross_text:

            order_side_rule = (
                "Bullish cross uses the same side "
                "as the isolated instrument."
            )

        elif "bearish" in cross_text:

            order_side_rule = (
                "Bearish cross uses the opposite side "
                "of the isolated instrument."
            )

        else:

            order_side_rule = (
                "Order side could not be resolved because "
                "EMA cross type is unknown."
            )

        header = (
            f"{strike} "
            f"{isolated_instrument_type} - "
            f"crosses {selected_level} - "
            f"At {nifty_text}"
        )

        order_details_text = (
            self._format_suggested_order_instruments(
                suggested_order_instruments
            )
        )

        message = (
            f"{header}\n\n"
            "EMA Cross Details:\n"
            f"Cross Type: {cross_type}\n"
            f"Signal: {current_signal}\n"
            f"Isolated Instrument Type: "
            f"{isolated_instrument_type}\n"
            f"Suggested Order Side: "
            f"{suggested_order_type_text}\n"
            f"Order Side Rule: "
            f"{order_side_rule}\n"
            f"EMA Calculation Mode: "
            f"{ema_mode}\n"
            f"EMA Mode Description: "
            f"{ema_mode_description}\n"
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
            "Sending isolated EMA Telegram message. "
            "instrument_key=%s, "
            "cross_type=%s, "
            "isolated_instrument_type=%s, "
            "suggested_order_option_type=%s, "
            "suggested_instruments_count=%s",
            instrument_key,
            cross_type,
            isolated_instrument_type,
            suggested_order_option_type,
            len(suggested_order_instruments),
        )

        result = self.send_message(
            title="Isolated Instrument EMA Alert",
            message=message,
            level="EMA",
            notification_context=(
                "isolated_ema"
                f"|instrument_key={instrument_key}"
                f"|cross_type={cross_type}"
            ),
        )

        if result:

            logger.info(
                "Isolated EMA Telegram alert sent successfully. "
                "instrument_key=%s, "
                "cross_type=%s",
                instrument_key,
                cross_type,
            )

        else:

            logger.error(
                "Isolated EMA Telegram alert FAILED. "
                "instrument_key=%s, "
                "cross_type=%s",
                instrument_key,
                cross_type,
            )

        return result

    # ========================================================
    # Convenience Wrapper
    # ========================================================

    def send_isolated_instrument_message(
        self,
        isolated_state: dict,
    ) -> bool:
        """
        Convenience wrapper for isolated instrument notification.

        Allows opening_range_service.py to pass the full
        isolated state instead of individual fields.
        """

        isolated_state = (
            isolated_state or {}
        )

        contract_info = (
            isolated_state.get(
                "contract_info"
            )
            or {}
        )

        symbol = (
            contract_info.get(
                "trading_symbol"
            )
            or contract_info.get(
                "instrument_key"
            )
            or isolated_state.get(
                "instrument_key"
            )
            or "N/A"
        )

        return self.send_selected_or_instrument_message(
            instrument_key=isolated_state.get(
                "instrument_key"
            ),
            symbol=symbol,
            level=isolated_state.get(
                "selected_level"
            ),
            level_value=isolated_state.get(
                "level_value"
            ),
            trigger_field=isolated_state.get(
                "trigger_field"
            ),
            trigger_price=isolated_state.get(
                "trigger_price"
            ),
            touch_time=isolated_state.get(
                "touch_time"
            ),
            source=isolated_state.get(
                "touch_source"
            ),
            nifty_ltp=isolated_state.get(
                "latest_main_index_ltp"
            ),
            strike_price=contract_info.get(
                "strike_price"
            ),
            instrument_type=contract_info.get(
                "instrument_type"
            ),
            reference_average=isolated_state.get(
                "reference_average"
            ),
            average_window=isolated_state.get(
                "average_window"
            ),
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

            return (
                "Nearest Instrument Details: "
                "not_available"
            )

        lines = [
            "Nearest Instrument Details:"
        ]

        for item in suggested_order_instruments:

            if not isinstance(item, dict):
                continue

            strike = item.get(
                "strike_price",
                "N/A",
            )

            instrument_type = str(
                item.get(
                    "instrument_type",
                    "N/A",
                )
            ).upper()

            live_ltp = item.get(
                "live_ltp"
            )

            if live_ltp is None:

                price_text = (
                    "ltp_not_available"
                )

            else:

                price_text = (
                    f"{live_ltp}rs"
                )

            lines.append(
                f"- {strike}"
                f"{instrument_type}"
                f" - {price_text}"
            )

        if len(lines) == 1:

            return (
                "Nearest Instrument Details: "
                "not_available"
            )

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
            f"Exception Type: "
            f"{type(exception).__name__}\n"
            f"Exception Message: "
            f"{exception}"
        )

        if context:

            message = (
                f"Context: {context}\n\n"
                f"{message}"
            )

        return self.send_message(
            title=title,
            message=message,
            level="ERROR",
            notification_context=(
                f"exception|context={context}"
                if context
                else "exception"
            ),
        )


# ============================================================
# Global Telegram Service Instance
# ============================================================

telegram_service = TelegramService()