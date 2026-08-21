import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import upstox_client

from core import config
from core.logger import get_logger
from services.token_service import token_service
from services.option_service import (
    options_cache,
    get_feed_by_instrument_key,
    get_subscribed_instrument_keys,
)
from services.live_ema_service import live_ema_service
from services.opening_range_service import (
    get_opening_range_status,
    process_live_tick_for_opening_range,
    get_opening_range_levels_for_ema_event,
    process_selected_or_ema_cross_alert,
    flush_pending_touch_alerts,
)
from services.telegram_service import send_telegram_message
from services.order_place import place_order
from ws_feed.broadcaster import broadcaster

logger = get_logger(__file__)


class UpstoxStreamer:
    def __init__(self):
        self.is_running = False
        self.streamer = None
        self.task = None
        self.loop = None

        self.market_timezone = self._load_market_timezone()
        self.market_time_format = getattr(
            config,
            "MARKET_TIME_FORMAT",
            "%Y-%m-%d %H:%M:%S %Z",
        )

        self.message_count = 0
        self.feed_count = 0
        self.broadcast_success_count = 0
        self.broadcast_failed_count = 0
        self.contract_match_count = 0
        self.contract_miss_count = 0

        self.live_ema_processed_count = 0
        self.live_ema_cross_count = 0
        self.live_ema_failed_count = 0

        self.ema_opening_range_enriched_count = 0
        self.ema_opening_range_enrichment_failed_count = 0

        # Isolated instrument EMA Telegram alert counters.
        self.selected_or_ema_alert_processed_count = 0
        self.selected_or_ema_alert_sent_count = 0
        self.selected_or_ema_alert_failed_count = 0

        self.opening_range_processed_count = 0
        self.opening_range_touch_count = 0
        self.opening_range_failed_count = 0
        self.opening_range_broadcast_count = 0
        self.opening_range_alert_flush_count = 0

    def _load_market_timezone(self):
        """
        Loads market timezone from config.

        Default:
            Asia/Kolkata
        """

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
        """Returns current configured market time as formatted string."""

        return datetime.now(self.market_timezone).strftime(self.market_time_format)

    def _get_live_ema_calculation_mode_text(self) -> str:
        """
        Returns readable live EMA calculation mode.

        Config:
            LIVE_EMA_CALCULATION_MODE = False
                completed 1-minute candle close based EMA

            LIVE_EMA_CALCULATION_MODE = True
                live tick/LTP based EMA
        """

        tick_based_mode = bool(getattr(config, "LIVE_EMA_CALCULATION_MODE", False))

        return "tick_ltp" if tick_based_mode else "candle_close"

    def _should_process_incoming_message(self) -> bool:
        """
        Decides whether incoming Upstox messages should be scheduled for processing.
        """

        connected_clients = broadcaster.get_active_connections_count()

        if connected_clients > 0:
            return True

        if bool(getattr(config, "LIVE_EMA_ENABLED", True)):
            return True

        if bool(getattr(config, "OPENING_RANGE_TOUCH_ALERT_ENABLED", True)):
            return True

        if bool(getattr(config, "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS", True)):
            return True

        if bool(getattr(config, "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED", True)):
            return True

        return False

    def get_status(self) -> dict:
        """
        Returns current streamer status.
        """

        live_ema_status = {}

        try:
            live_ema_status = live_ema_service.get_status()
        except Exception as ex:
            live_ema_status = {
                "status": "error",
                "error": f"{type(ex).__name__}: {ex}",
            }

        opening_range_status = {}

        try:
            opening_range_status = get_opening_range_status()
        except Exception as ex:
            opening_range_status = {
                "status": "error",
                "error": f"{type(ex).__name__}: {ex}",
            }

        live_ema_calculation_mode_flag = bool(
            getattr(config, "LIVE_EMA_CALCULATION_MODE", False)
        )

        live_ema_calculation_mode = self._get_live_ema_calculation_mode_text()

        return {
            "is_running": self.is_running,
            "has_streamer": self.streamer is not None,
            "has_task": self.task is not None,
            "loop_available": self.loop is not None,
            "loop_running": bool(self.loop and self.loop.is_running()),
            "message_count": self.message_count,
            "feed_count": self.feed_count,
            "broadcast_success_count": self.broadcast_success_count,
            "broadcast_failed_count": self.broadcast_failed_count,
            "contract_match_count": self.contract_match_count,
            "contract_miss_count": self.contract_miss_count,
            "live_ema_processed_count": self.live_ema_processed_count,
            "live_ema_cross_count": self.live_ema_cross_count,
            "live_ema_failed_count": self.live_ema_failed_count,
            "live_ema_calculation_mode_flag": live_ema_calculation_mode_flag,
            "live_ema_calculation_mode": live_ema_calculation_mode,
            "live_ema_calculation_mode_description": (
                "tick/LTP based EMA calculation"
                if live_ema_calculation_mode_flag
                else "completed 1-minute candle close based EMA calculation"
            ),
            "ema_opening_range_enriched_count": self.ema_opening_range_enriched_count,
            "ema_opening_range_enrichment_failed_count": (
                self.ema_opening_range_enrichment_failed_count
            ),
            "selected_or_ema_alert_processed_count": (
                self.selected_or_ema_alert_processed_count
            ),
            "selected_or_ema_alert_sent_count": self.selected_or_ema_alert_sent_count,
            "selected_or_ema_alert_failed_count": (
                self.selected_or_ema_alert_failed_count
            ),
            "selected_or_ema_alert_flow": "isolated_instrument_only",
            "live_ema_status": live_ema_status,
            "opening_range_processed_count": self.opening_range_processed_count,
            "opening_range_touch_count": self.opening_range_touch_count,
            "opening_range_failed_count": self.opening_range_failed_count,
            "opening_range_broadcast_count": self.opening_range_broadcast_count,
            "opening_range_alert_flush_count": self.opening_range_alert_flush_count,
            "opening_range_status": opening_range_status,
            "ema_cross_include_opening_range_levels": getattr(
                config,
                "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
                True,
            ),
            "ema_isolated_instrument_telegram_enabled": getattr(
                config,
                "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED",
                True,
            ),
            "place_order_enabled": getattr(config, "PLACE_ORDER_ENABLED", False),
            "market_time": self._now_market_time(),
        }

    async def start(self):
        """Starts Upstox WebSocket streamer in the background."""

        logger.info(f"UpstoxStreamer.start() called at {self._now_market_time()}")

        if self.is_running:
            logger.info(
                f"UpstoxStreamer already running. Skipping start. "
                f"market_time={self._now_market_time()}"
            )
            return

        self.is_running = True
        self.loop = asyncio.get_running_loop()

        self.task = asyncio.create_task(self._run_loop())

        logger.info(
            f"UpstoxStreamer background task created successfully. "
            f"market_time={self._now_market_time()}, "
            f"live_ema_calculation_mode={self._get_live_ema_calculation_mode_text()}"
        )

    async def stop(self):
        """Gracefully disconnects the streamer."""

        logger.info(f"UpstoxStreamer.stop() called at {self._now_market_time()}")

        self.is_running = False

        if self.streamer:
            try:
                logger.info(
                    f"Disconnecting Upstox streamer. "
                    f"market_time={self._now_market_time()}"
                )

                self.streamer.disconnect()

                logger.info(
                    f"Upstox streamer disconnected successfully. "
                    f"market_time={self._now_market_time()}"
                )

            except Exception as ex:
                logger.error(
                    f"Error disconnecting Upstox Streamer: "
                    f"{type(ex).__name__}: {ex}. "
                    f"market_time={self._now_market_time()}"
                )

            finally:
                self.streamer = None

        if self.task:
            try:
                current_task = asyncio.current_task()

                if self.task is not current_task:
                    self.task.cancel()

                    try:
                        await self.task

                    except asyncio.CancelledError:
                        logger.info(
                            f"Upstox background task cancelled successfully. "
                            f"market_time={self._now_market_time()}"
                        )

                else:
                    logger.warning(
                        "UpstoxStreamer.stop() called from inside its own task. "
                        "Skipping await on same task."
                    )

            except Exception as ex:
                logger.error(
                    f"Error cancelling Upstox task: "
                    f"{type(ex).__name__}: {ex}. "
                    f"market_time={self._now_market_time()}"
                )

            finally:
                self.task = None

    async def restart(self):
        """
        Restarts Upstox streamer so latest token and subscription keys are applied.
        """

        logger.info(
            f"UpstoxStreamer.restart() called at {self._now_market_time()}, "
            f"live_ema_calculation_mode={self._get_live_ema_calculation_mode_text()}"
        )

        try:
            await self.stop()

            await asyncio.sleep(2)

            await self.start()

            logger.info(
                f"UpstoxStreamer restarted successfully with latest "
                f"subscription keys. market_time={self._now_market_time()}, "
                f"live_ema_calculation_mode={self._get_live_ema_calculation_mode_text()}"
            )

        except Exception as ex:
            logger.error(
                f"Failed to restart UpstoxStreamer: "
                f"{type(ex).__name__}: {ex}. "
                f"market_time={self._now_market_time()}"
            )

            raise

    async def _run_loop(self):
        """Main Upstox connection loop."""

        logger.info(
            f"Entered UpstoxStreamer._run_loop at {self._now_market_time()}, "
            f"live_ema_calculation_mode={self._get_live_ema_calculation_mode_text()}"
        )

        while self.is_running:
            access_token = token_service.get_access_token()

            if not access_token:
                logger.warning(
                    f"No access token found in token_service. Retrying in 10s. "
                    f"market_time={self._now_market_time()}"
                )

                await asyncio.sleep(10)
                continue

            try:
                logger.info(
                    f"Configuring Upstox SDK client. "
                    f"market_time={self._now_market_time()}"
                )

                configuration = upstox_client.Configuration()
                configuration.access_token = access_token

                api_client = upstox_client.ApiClient(configuration)

                try:
                    keys = get_subscribed_instrument_keys()

                except Exception:
                    logger.warning(
                        "get_subscribed_instrument_keys() failed. "
                        "Falling back to direct options_cache read."
                    )
                    keys = options_cache.get("subscribed_keys", [])

                mode = getattr(config, "WEBSOCKET_FEED_MODE", "full")

                logger.info(f"WebSocket feed mode: {mode}")
                logger.info(f"Subscribed keys count from options_cache: {len(keys)}")
                logger.info(
                    f"Live EMA calculation mode: "
                    f"{self._get_live_ema_calculation_mode_text()}"
                )

                if not keys:
                    logger.warning(
                        f"No instrument keys found in options_cache. Waiting 5s. "
                        f"market_time={self._now_market_time()}"
                    )

                    await asyncio.sleep(5)
                    continue

                logger.info(
                    f"Creating Upstox MarketDataStreamerV3 instance. "
                    f"market_time={self._now_market_time()}"
                )

                self.streamer = upstox_client.MarketDataStreamerV3(
                    api_client,
                    keys,
                    mode,
                )

                logger.info(
                    f"MarketDataStreamerV3 instance created successfully. "
                    f"market_time={self._now_market_time()}"
                )

                def on_open():
                    logger.info(
                        f"Connected to Upstox Market Stream V3 WebSocket successfully. "
                        f"market_time={self._now_market_time()}"
                    )

                    logger.info(f"Subscribed to {len(keys)} instruments.")
                    logger.info(
                        f"Live EMA calculation mode active: "
                        f"{self._get_live_ema_calculation_mode_text()}"
                    )

                def on_message(message):
                    self.message_count += 1

                    if not self._should_process_incoming_message():
                        return

                    if self.loop and self.loop.is_running():
                        future = asyncio.run_coroutine_threadsafe(
                            self._process_message(message),
                            self.loop,
                        )

                        def callback(f):
                            try:
                                f.result()

                            except Exception as ex:
                                logger.error(
                                    f"_process_message future failed: "
                                    f"{type(ex).__name__}: {ex}. "
                                    f"market_time={self._now_market_time()}"
                                )

                        future.add_done_callback(callback)

                    else:
                        logger.error(
                            f"Main event loop not available or not running. "
                            f"market_time={self._now_market_time()}"
                        )

                def on_error(error):
                    logger.error(
                        f"Upstox WebSocket Error: "
                        f"{type(error).__name__}: {error}. "
                        f"market_time={self._now_market_time()}"
                    )

                def on_close(close_status_code, close_msg):
                    logger.warning(
                        f"Upstox WebSocket Closed: "
                        f"{close_status_code} - {close_msg}. "
                        f"market_time={self._now_market_time()}"
                    )

                logger.info(
                    f"Attaching Upstox event listeners. "
                    f"market_time={self._now_market_time()}"
                )

                self.streamer.on("open", on_open)
                self.streamer.on("message", on_message)
                self.streamer.on("error", on_error)
                self.streamer.on("close", on_close)

                logger.info(
                    f"Upstox event listeners attached successfully. "
                    f"market_time={self._now_market_time()}"
                )

                logger.info(
                    f"Starting Upstox Market Stream V3 connection. "
                    f"market_time={self._now_market_time()}"
                )

                await asyncio.to_thread(self.streamer.connect)

                logger.warning(
                    "self.streamer.connect() returned. "
                    "This may mean stream closed or SDK returned control. "
                    f"market_time={self._now_market_time()}"
                )

                while self.is_running:
                    await asyncio.sleep(30)

            except asyncio.CancelledError:
                logger.warning(
                    f"Upstox _run_loop task cancelled. "
                    f"market_time={self._now_market_time()}"
                )

                break

            except Exception as ex:
                logger.error(
                    f"WebSocket Connection Exception: "
                    f"{type(ex).__name__}: {ex}. Reconnecting in 5s. "
                    f"market_time={self._now_market_time()}"
                )

                await asyncio.sleep(5)

        logger.info(f"Exiting UpstoxStreamer._run_loop at {self._now_market_time()}")

    def _enrich_ema_event_with_opening_range(
        self,
        instrument_key: str,
        live_ema_cross_event: dict,
    ) -> dict:
        """
        Adds compact Opening Range payload to EMA crossover event.
        """

        if not isinstance(live_ema_cross_event, dict):
            return live_ema_cross_event

        if not bool(getattr(config, "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS", True)):
            live_ema_cross_event.update(
                {
                    "opening_range": {},
                    "touch_status": {},
                    "latest_intraday_close": None,
                    "latest_main_index_ltp": None,
                    "processed_at": None,
                }
            )

            return live_ema_cross_event

        try:
            opening_range_payload = get_opening_range_levels_for_ema_event(
                instrument_key
            )

            if isinstance(opening_range_payload, dict):
                live_ema_cross_event.update(opening_range_payload)

            self.ema_opening_range_enriched_count += 1

        except Exception as ex:
            self.ema_opening_range_enrichment_failed_count += 1

            logger.error(
                f"Failed enriching EMA crossover event for {instrument_key}: "
                f"{type(ex).__name__}: {ex}. "
                f"market_time={self._now_market_time()}"
            )

            live_ema_cross_event.update(
                {
                    "opening_range": {},
                    "touch_status": {},
                    "latest_intraday_close": None,
                    "latest_main_index_ltp": None,
                    "processed_at": None,
                }
            )

        return live_ema_cross_event

    def _process_isolated_ema_telegram_alert(
        self,
        live_ema_cross_event: dict,
    ):
        """
        Sends Telegram EMA alert and places ONE order on the selected
        budget-range instrument when order placement is enabled.

        Order Placement Conditions:
            - PLACE_ORDER_ENABLED must be True in config.
            - The EMA alert must belong to the isolated instrument.
            - The suggested order side is determined by the EMA cross:
                * bullish_cross -> same side as isolated instrument
                * bearish_cross -> opposite side of isolated instrument
            - Budget candidates are filtered using the configured budget range.
            - Budget candidates are sorted by LOWEST option LTP.
            - The FIRST budget candidate is selected for the actual order.
            - Exactly ONE order is submitted for one EMA alert.
            - Stop Loss is calculated as:
                candle_low - min(candle_low * 5%, SL_THRESHOLD)

        Important:
            The EMA-crossing instrument itself is NOT automatically used
            for order placement.

            Example:
                EMA isolated instrument:
                    NIFTY 23950 CE

                bearish_cross:
                    Suggested side = PE

                Budget candidates:
                    NIFTY 24100 PE = 27.95
                    NIFTY 24050 PE = 20.40

                Selected order:
                    NIFTY 24050 PE = 20.40
        """

        if not live_ema_cross_event:
            return

        if not bool(
            getattr(
                config,
                "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED",
                True,
            )
        ):
            return

        try:
            self.selected_or_ema_alert_processed_count += 1

            # ============================================================
            # 1. Process primary isolated EMA Telegram alert
            #
            # process_selected_or_ema_cross_alert() now returns:
            #
            # {
            #     "sent": bool,
            #     "selected_order_instrument": dict | None,
            # }
            # ============================================================

            alert_result = process_selected_or_ema_cross_alert(live_ema_cross_event)

            # Safety: support unexpected/legacy return values.
            if isinstance(alert_result, dict):
                sent = bool(alert_result.get("sent", False))

                selected_order_instrument = alert_result.get(
                    "selected_order_instrument"
                )
            else:
                # Backward-compatible fallback.
                sent = bool(alert_result)
                selected_order_instrument = None

            if sent:
                self.selected_or_ema_alert_sent_count += 1

                logger.info(
                    "Isolated instrument EMA Telegram alert sent. "
                    f"instrument_key="
                    f"{live_ema_cross_event.get('instrument_key')}, "
                    f"cross_type="
                    f"{live_ema_cross_event.get('cross_type')}, "
                    f"timestamp="
                    f"{live_ema_cross_event.get('timestamp')}, "
                    f"selected_order_instrument="
                    f"{(
                        selected_order_instrument.get('instrument_key')
                        if isinstance(selected_order_instrument, dict)
                        else None
                    )}, "
                    f"selected_order_symbol="
                    f"{(
                        selected_order_instrument.get('trading_symbol')
                        if isinstance(selected_order_instrument, dict)
                        else None
                    )}, "
                    f"selected_order_ltp="
                    f"{(
                        selected_order_instrument.get('live_ltp')
                        if isinstance(selected_order_instrument, dict)
                        else None
                    )}"
                )

            # ============================================================
            # 2. Check PLACE_ORDER_ENABLED
            # ============================================================

            is_place_order_enabled = bool(
                getattr(
                    config,
                    "PLACE_ORDER_ENABLED",
                    False,
                )
            )

            if not is_place_order_enabled:
                logger.info(
                    "Order placement skipped: "
                    "PLACE_ORDER_ENABLED is set to False in config."
                )
                return

            # ============================================================
            # 3. Validate selected budget instrument
            #
            # IMPORTANT:
            # Do NOT use:
            #
            #     live_ema_cross_event["instrument_key"]
            #
            # for the order.
            #
            # We now use the first/lowest-LTP budget instrument selected
            # by option_service.py.
            # ============================================================

            if not isinstance(
                selected_order_instrument,
                dict,
            ):
                warning_msg = (
                    "⚠️ <b>[ORDER SKIPPED]</b> "
                    "No eligible budget-range order instrument "
                    "was selected.\n"
                    f"<b>EMA Instrument:</b> "
                    f"<code>{live_ema_cross_event.get('instrument_key')}</code>\n"
                    f"<b>EMA Cross:</b> "
                    f"<code>{live_ema_cross_event.get('cross_type')}</code>\n"
                    "<b>Reason:</b> "
                    "No instrument matched the suggested option side "
                    "and configured budget price range."
                )

                logger.warning(warning_msg)
                send_telegram_message(warning_msg)
                return

            # ============================================================
            # 4. Extract SELECTED ORDER instrument key
            # ============================================================

            instrument_key = selected_order_instrument.get(
                "instrument_key"
            ) or selected_order_instrument.get("instrument_token")

            if not instrument_key:
                warning_msg = (
                    "⚠️ <b>[ORDER SKIPPED]</b> "
                    "Selected budget instrument has no instrument key.\n"
                    f"<b>Selected Symbol:</b> "
                    f"<code>{selected_order_instrument.get('trading_symbol', 'N/A')}</code>\n"
                    f"<b>Selected LTP:</b> "
                    f"<code>{selected_order_instrument.get('live_ltp', 'N/A')}</code>"
                )

                logger.warning(warning_msg)
                send_telegram_message(warning_msg)
                return

            # ============================================================
            # 5. Fetch selected order instrument metadata
            # ============================================================

            contract_info = get_feed_by_instrument_key(instrument_key) or {}

            trading_symbol = (
                selected_order_instrument.get("trading_symbol")
                or contract_info.get("trading_symbol")
                or "N/A"
            )

            strike_price = (
                selected_order_instrument.get("strike_price")
                or contract_info.get("strike_price")
                or "N/A"
            )

            strike_type = (
                selected_order_instrument.get("option_type")
                or selected_order_instrument.get("instrument_type")
                or contract_info.get("option_type")
                or contract_info.get("strike_type")
                or "N/A"
            )

            selected_ltp = selected_order_instrument.get("live_ltp")

            # ============================================================
            # 6. Get EMA candle data
            #
            # The SL is based on the EMA event candle low.
            # ============================================================

            candle_data = (
                live_ema_cross_event.get("candle")
                or live_ema_cross_event.get("candle_data")
                or {}
            )

            candle_low = (
                candle_data.get("low")
                or live_ema_cross_event.get("candle_low")
                or live_ema_cross_event.get("low")
            )

            if candle_low is None:
                warning_msg = (
                    "⚠️ <b>[ORDER SKIPPED]</b> "
                    "Missing candle low required for Stop Loss.\n"
                    f"<b>Selected Symbol:</b> "
                    f"<code>{trading_symbol}</code>\n"
                    f"<b>Selected Instrument:</b> "
                    f"<code>{instrument_key}</code>\n"
                    f"<b>Selected LTP:</b> "
                    f"<code>{selected_ltp}</code>\n"
                    f"<b>Candle Low:</b> "
                    f"<code>{candle_low}</code>"
                )

                logger.warning(warning_msg)
                send_telegram_message(warning_msg)
                return

            # ============================================================
            # 7. Validate candle low
            # ============================================================

            try:
                candle_low_float = float(candle_low)
            except (TypeError, ValueError):
                candle_low_float = None

            if candle_low_float is None or candle_low_float <= 0:
                warning_msg = (
                    "⚠️ <b>[ORDER SKIPPED]</b> "
                    "Invalid candle low for Stop Loss calculation.\n"
                    f"<b>Selected Symbol:</b> "
                    f"<code>{trading_symbol}</code>\n"
                    f"<b>Instrument:</b> "
                    f"<code>{instrument_key}</code>\n"
                    f"<b>Candle Low:</b> "
                    f"<code>{candle_low}</code>"
                )

                logger.warning(warning_msg)
                send_telegram_message(warning_msg)
                return

            # ============================================================
            # 8. Calculate Stop Loss
            #
            # New rule:
            #
            #     SL distance = min(
            #         candle_low * 5%,
            #         SL_THRESHOLD
            #     )
            #
            #     calculated_sl = candle_low - sl_distance
            #
            # Example:
            #     candle_low = 3.70
            #     5% = 0.185
            #     SL ≈ 3.52
            #
            # This prevents the old negative SL problem caused by:
            #
            #     candle_low - 5
            # ============================================================

            sl_threshold = float(
                getattr(
                    config,
                    "SL_THRESHOLD",
                    5.0,
                )
            )

            sl_distance = min(
                candle_low_float * 0.05,
                sl_threshold,
            )

            calculated_sl = round(
                candle_low_float - sl_distance,
                2,
            )

            # Final safety check.
            if calculated_sl <= 0:
                warning_msg = (
                    "⚠️ <b>[ORDER SKIPPED]</b> "
                    "Calculated Stop Loss is not valid.\n"
                    f"<b>Selected Symbol:</b> "
                    f"<code>{trading_symbol}</code>\n"
                    f"<b>Instrument:</b> "
                    f"<code>{instrument_key}</code>\n"
                    f"<b>Candle Low:</b> "
                    f"<code>{candle_low_float}</code>\n"
                    f"<b>SL Distance:</b> "
                    f"<code>{sl_distance}</code>\n"
                    f"<b>Calculated SL:</b> "
                    f"<code>{calculated_sl}</code>"
                )

                logger.warning(warning_msg)
                send_telegram_message(warning_msg)
                return

            # ============================================================
            # 9. Quantity
            # ============================================================

            quantity = getattr(
                config,
                "DEFAULT_ORDER_QUANTITY",
                65,
            )

            # ============================================================
            # 10. Log selected instrument
            # ============================================================

            logger.info(
                "Selected budget-range instrument for order. "
                f"EMA instrument="
                f"{live_ema_cross_event.get('instrument_key')}, "
                f"selected_order_instrument={instrument_key}, "
                f"selected_symbol={trading_symbol}, "
                f"selected_ltp={selected_ltp}, "
                f"option_type={strike_type}, "
                f"cross_type="
                f"{live_ema_cross_event.get('cross_type')}"
            )

            # ============================================================
            # 11. Telegram notification BEFORE placing order
            # ============================================================

            pre_order_msg = (
                f"🚀 <b>[PLACING ORDER]</b> "
                f"Initiating Upstox Order...\n"
                f"<b>EMA Instrument:</b> "
                f"<code>{live_ema_cross_event.get('instrument_key')}</code>\n"
                f"<b>EMA Cross:</b> "
                f"<code>{live_ema_cross_event.get('cross_type', 'N/A')}</code>\n"
                f"<b>Selected Order Symbol:</b> "
                f"<code>{trading_symbol}</code>\n"
                f"<b>Selected Order Instrument:</b> "
                f"<code>{instrument_key}</code>\n"
                f"<b>Selected Order LTP:</b> "
                f"<code>{selected_ltp}</code>\n"
                f"<b>Strike Price:</b> "
                f"<code>{strike_price}</code>\n"
                f"<b>Strike Type:</b> "
                f"<code>{strike_type}</code>\n"
                f"<b>Candle Low:</b> "
                f"<code>{candle_low_float}</code>\n"
                f"<b>SL Distance:</b> "
                f"<code>{sl_distance}</code>\n"
                f"<b>SL Trigger Price:</b> "
                f"<code>{calculated_sl}</code>\n"
                f"<b>Quantity:</b> "
                f"<code>{quantity}</code>"
            )

            send_telegram_message(pre_order_msg)

            # ============================================================
            # 12. Place exactly ONE order
            #
            # IMPORTANT:
            # This is now the SELECTED budget instrument, not the
            # original EMA-crossing instrument.
            # ============================================================

            order_result = place_order(
                instrument_key=instrument_key,
                stop_loss=calculated_sl,
                quantity=quantity,
                order_type="SL-M",
                transaction_type="BUY",
            )

            # ============================================================
            # 13. Telegram notification AFTER placing order
            # ============================================================

            if order_result.get("status") == "success":
                api_data = order_result.get(
                    "api_response",
                    {},
                )

                order_id = (
                    api_data.get("order_id")
                    if isinstance(api_data, dict)
                    else getattr(
                        api_data,
                        "order_id",
                        "N/A",
                    )
                )

                post_order_msg = (
                    f"✅ <b>[ORDER SUCCESS]</b> "
                    f"Order placed successfully!\n"
                    f"<b>Order ID:</b> "
                    f"<code>{order_id}</code>\n"
                    f"<b>EMA Instrument:</b> "
                    f"<code>{live_ema_cross_event.get('instrument_key')}</code>\n"
                    f"<b>Selected Order Symbol:</b> "
                    f"<code>{trading_symbol}</code>\n"
                    f"<b>Selected Order Instrument:</b> "
                    f"<code>{instrument_key}</code>\n"
                    f"<b>Selected Order LTP:</b> "
                    f"<code>{selected_ltp}</code>\n"
                    f"<b>Strike Price:</b> "
                    f"<code>{strike_price}</code>\n"
                    f"<b>Strike Type:</b> "
                    f"<code>{strike_type}</code>\n"
                    f"<b>SL Trigger:</b> "
                    f"<code>{calculated_sl}</code>"
                )

                logger.info(post_order_msg)
                send_telegram_message(post_order_msg)

            else:
                err_msg = order_result.get(
                    "message",
                    "Unknown error",
                )

                post_order_msg = (
                    f"❌ <b>[ORDER FAILED]</b> "
                    f"Order rejected by API.\n"
                    f"<b>EMA Instrument:</b> "
                    f"<code>{live_ema_cross_event.get('instrument_key')}</code>\n"
                    f"<b>Selected Order Symbol:</b> "
                    f"<code>{trading_symbol}</code>\n"
                    f"<b>Selected Order Instrument:</b> "
                    f"<code>{instrument_key}</code>\n"
                    f"<b>Selected Order LTP:</b> "
                    f"<code>{selected_ltp}</code>\n"
                    f"<b>Strike Price:</b> "
                    f"<code>{strike_price}</code>\n"
                    f"<b>Strike Type:</b> "
                    f"<code>{strike_type}</code>\n"
                    f"<b>SL Trigger:</b> "
                    f"<code>{calculated_sl}</code>\n"
                    f"<b>Reason:</b> "
                    f"{err_msg}"
                )

                logger.error(post_order_msg)
                send_telegram_message(post_order_msg)

        except Exception as ex:
            self.selected_or_ema_alert_failed_count += 1

            error_details = f"{type(ex).__name__}: {str(ex)}"

            logger.error(
                "Isolated instrument EMA alert or "
                f"order execution error: {error_details}"
            )

            # ------------------------------------------------------------
            # Safely extract original EMA instrument details
            # ------------------------------------------------------------

            inst_key = live_ema_cross_event.get(
                "instrument_key",
                "N/A",
            )

            c_info = get_feed_by_instrument_key(inst_key) or {}

            # ------------------------------------------------------------
            # Exception Telegram notification
            # ------------------------------------------------------------

            exception_msg = (
                f"🚨 <b>[ORDER EXECUTION ERROR]</b> "
                f"An exception occurred!\n"
                f"<b>EMA Symbol:</b> "
                f"<code>{c_info.get('trading_symbol', 'N/A')}</code>\n"
                f"<b>EMA Instrument:</b> "
                f"<code>{inst_key}</code>\n"
                f"<b>Strike Price:</b> "
                f"<code>{c_info.get('strike_price', 'N/A')}</code>\n"
                f"<b>Strike Type:</b> "
                f"<code>{c_info.get('option_type', c_info.get('strike_type', 'N/A'))}</code>\n"
                f"<b>Error Details:</b> "
                f"<code>{error_details}</code>"
            )

            try:
                send_telegram_message(exception_msg)
            except Exception as tg_ex:
                logger.error("Failed to send exception " f"Telegram message: {tg_ex}")

    async def _process_message(self, message):
        """
        Routes decoded ticks to services and handlers.
        """

        try:
            has_local_clients = broadcaster.get_active_connections_count() > 0

            if isinstance(message, dict):
                tick_dict = message

            elif isinstance(message, str):
                tick_dict = json.loads(message)

            elif isinstance(message, bytes):
                decoded_message = message.decode("utf-8")
                tick_dict = json.loads(decoded_message)

            else:
                logger.warning(
                    f"Unsupported Upstox message type: {type(message)}. "
                    f"market_time={self._now_market_time()}"
                )

                return

            feeds = tick_dict.get("feeds", {})

            if not isinstance(feeds, dict):
                logger.warning(
                    f"Invalid feeds object type: {type(feeds)}. "
                    f"market_time={self._now_market_time()}"
                )

                return

            if len(feeds) == 0:
                return

            self.feed_count += len(feeds)

            for key, tick_data in feeds.items():
                if not tick_data:
                    continue

                contract_info = get_feed_by_instrument_key(key)

                if contract_info:
                    self.contract_match_count += 1
                else:
                    self.contract_miss_count += 1

                # 1. Process live EMA crossover continuation
                live_ema_cross_event = None

                try:
                    if getattr(config, "LIVE_EMA_ENABLED", True):
                        live_ema_cross_event = live_ema_service.process_live_feed(
                            instrument_key=key,
                            tick_data=tick_data,
                            contract_info=contract_info,
                        )

                        self.live_ema_processed_count += 1

                        if live_ema_cross_event:
                            self.live_ema_cross_count += 1

                except Exception as ema_ex:
                    self.live_ema_failed_count += 1

                    logger.error(
                        f"Live EMA processing failed for key {key}: "
                        f"{type(ema_ex).__name__}: {ema_ex}. "
                        f"market_time={self._now_market_time()}"
                    )

                # 2. Process Opening Range touch detection
                opening_range_touch_events = []

                try:
                    if getattr(config, "OPENING_RANGE_TOUCH_ALERT_ENABLED", True):
                        opening_range_touch_events = (
                            process_live_tick_for_opening_range(
                                instrument_key=key,
                                tick_data=tick_data,
                                contract_info=contract_info,
                            )
                        )

                        self.opening_range_processed_count += 1

                        if opening_range_touch_events:
                            self.opening_range_touch_count += len(
                                opening_range_touch_events
                            )

                            logger.info(
                                f"Opening Range touch event generated for {key}. "
                                f"events_count={len(opening_range_touch_events)}"
                            )

                except Exception as or_ex:
                    self.opening_range_failed_count += 1

                    logger.error(
                        f"Opening Range live touch processing failed for key {key}: "
                        f"{type(or_ex).__name__}: {or_ex}. "
                        f"market_time={self._now_market_time()}"
                    )

                # 3. Enrich and alert EMA event after OR processing
                if live_ema_cross_event:
                    live_ema_cross_event = self._enrich_ema_event_with_opening_range(
                        instrument_key=key,
                        live_ema_cross_event=live_ema_cross_event,
                    )

                    logger.info(
                        f"Live EMA cross event generated for {key}: "
                        f"{live_ema_cross_event.get('cross_type')} "
                        f"at {live_ema_cross_event.get('timestamp')}, "
                        f"ema_calculation_mode="
                        f"{live_ema_cross_event.get('ema_calculation_mode')}"
                    )

                    self._process_isolated_ema_telegram_alert(live_ema_cross_event)

                # 4. Broadcast live EMA cross event if clients exist
                if live_ema_cross_event and has_local_clients:
                    try:
                        if hasattr(broadcaster, "broadcast_ema_cross"):
                            await broadcaster.broadcast_ema_cross(live_ema_cross_event)

                    except Exception as ema_broadcast_ex:
                        logger.error(
                            f"Broadcasting live EMA cross failed for key {key}: "
                            f"{type(ema_broadcast_ex).__name__}: {ema_broadcast_ex}. "
                            f"market_time={self._now_market_time()}"
                        )

                # 5. Broadcast Opening Range touch events if clients exist
                if opening_range_touch_events and has_local_clients:
                    for or_event in opening_range_touch_events:
                        try:
                            if hasattr(broadcaster, "broadcast_opening_range"):
                                await broadcaster.broadcast_opening_range(or_event)
                                self.opening_range_broadcast_count += 1

                        except Exception as or_broadcast_ex:
                            logger.error(
                                f"Broadcasting Opening Range touch failed for key {key}: "
                                f"{type(or_broadcast_ex).__name__}: "
                                f"{or_broadcast_ex}. "
                                f"market_time={self._now_market_time()}"
                            )

                # 6. Broadcast normal live tick only when local clients exist
                if not has_local_clients:
                    continue

                try:
                    await broadcaster.broadcast_tick(
                        key,
                        tick_data,
                        contract_info,
                    )

                    self.broadcast_success_count += 1

                except Exception as b_ex:
                    self.broadcast_failed_count += 1

                    logger.error(
                        f"Broadcaster failed for key {key}: "
                        f"{type(b_ex).__name__}: {b_ex}. "
                        f"market_time={self._now_market_time()}"
                    )

            # 7. Flush pending Opening Range touch Telegram alerts
            try:
                if getattr(config, "OPENING_RANGE_TOUCH_ALERT_ENABLED", True):
                    sent = flush_pending_touch_alerts(
                        force=False,
                        source="live_tick",
                    )

                    if sent:
                        self.opening_range_alert_flush_count += 1

            except Exception as flush_ex:
                logger.error(
                    f"Opening Range pending touch alert flush failed: "
                    f"{type(flush_ex).__name__}: {flush_ex}. "
                    f"market_time={self._now_market_time()}"
                )

        except json.JSONDecodeError as json_ex:
            logger.error(
                f"JSON decode failed in Upstox message: "
                f"{type(json_ex).__name__}: {json_ex}. "
                f"market_time={self._now_market_time()}"
            )

        except Exception as ex:
            logger.error(
                f"Error processing Upstox tick message: "
                f"{type(ex).__name__}: {ex}. "
                f"market_time={self._now_market_time()}"
            )


upstox_streamer = UpstoxStreamer()
