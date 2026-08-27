import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import upstox_client

from core import config
from core.logger import get_logger
from services.live_ema_service import live_ema_service
from services.opening_range_service import (
    flush_pending_touch_alerts,
    get_opening_range_levels_for_ema_event,
    get_opening_range_status,
    process_live_tick_for_opening_range,
    process_selected_or_ema_cross_alert,
)
from services.option_service import (
    get_feed_by_instrument_key,
    get_subscribed_instrument_keys,
    options_cache,
)
from services.token_service import token_service
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

        self.isolated_ema_alert_processed_count = 0
        self.isolated_ema_alert_accepted_count = 0
        self.isolated_ema_alert_failed_count = 0

        self.selected_or_ema_alert_processed_count = 0
        self.selected_or_ema_alert_sent_count = 0
        self.selected_or_ema_alert_failed_count = 0

        self.opening_range_processed_count = 0
        self.opening_range_touch_count = 0
        self.opening_range_failed_count = 0
        self.opening_range_broadcast_count = 0
        self.opening_range_alert_flush_count = 0

    # ============================================================
    # Time Helpers
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

    def _get_live_ema_calculation_mode_text(
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

    # ============================================================
    # Message Processing Decision
    # ============================================================

    def _should_process_incoming_message(
        self,
    ) -> bool:
        connected_clients = broadcaster.get_active_connections_count()

        if connected_clients > 0:
            return True

        if bool(
            getattr(
                config,
                "LIVE_EMA_ENABLED",
                True,
            )
        ):
            return True

        if bool(
            getattr(
                config,
                "OPENING_RANGE_TOUCH_ALERT_ENABLED",
                True,
            )
        ):
            return True

        if bool(
            getattr(
                config,
                "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
                True,
            )
        ):
            return True

        if bool(
            getattr(
                config,
                "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED",
                True,
            )
        ):
            return True

        if bool(
            getattr(
                config,
                "ALGO_APP_ENABLED",
                False,
            )
        ):
            return True

        return False

    # ============================================================
    # Status
    # ============================================================

    def get_status(self) -> dict:
        try:
            live_ema_status = live_ema_service.get_status()
        except Exception as ex:
            live_ema_status = {
                "status": "error",
                "error": (f"{type(ex).__name__}: {ex}"),
            }

        try:
            opening_range_status = get_opening_range_status()
        except Exception as ex:
            opening_range_status = {
                "status": "error",
                "error": (f"{type(ex).__name__}: {ex}"),
            }

        live_ema_mode_flag = bool(
            getattr(
                config,
                "LIVE_EMA_CALCULATION_MODE",
                False,
            )
        )

        live_ema_mode = self._get_live_ema_calculation_mode_text()

        return {
            "is_running": self.is_running,
            "has_streamer": self.streamer is not None,
            "has_task": self.task is not None,
            "loop_available": self.loop is not None,
            "loop_running": bool(self.loop and self.loop.is_running()),
            "message_count": self.message_count,
            "feed_count": self.feed_count,
            "broadcast_success_count": (self.broadcast_success_count),
            "broadcast_failed_count": (self.broadcast_failed_count),
            "contract_match_count": (self.contract_match_count),
            "contract_miss_count": (self.contract_miss_count),
            "live_ema_processed_count": (self.live_ema_processed_count),
            "live_ema_cross_count": (self.live_ema_cross_count),
            "live_ema_failed_count": (self.live_ema_failed_count),
            "live_ema_calculation_mode_flag": (live_ema_mode_flag),
            "live_ema_calculation_mode": (live_ema_mode),
            "live_ema_calculation_mode_description": (
                "tick/LTP based EMA calculation"
                if live_ema_mode_flag
                else ("completed 1-minute candle " "close based EMA calculation")
            ),
            "ema_opening_range_enriched_count": (self.ema_opening_range_enriched_count),
            "ema_opening_range_enrichment_failed_count": (
                self.ema_opening_range_enrichment_failed_count
            ),
            "isolated_ema_alert_processed_count": (
                self.isolated_ema_alert_processed_count
            ),
            "isolated_ema_alert_accepted_count": (
                self.isolated_ema_alert_accepted_count
            ),
            "isolated_ema_alert_failed_count": (self.isolated_ema_alert_failed_count),
            "selected_or_ema_alert_processed_count": (
                self.selected_or_ema_alert_processed_count
            ),
            "selected_or_ema_alert_sent_count": (self.selected_or_ema_alert_sent_count),
            "selected_or_ema_alert_failed_count": (
                self.selected_or_ema_alert_failed_count
            ),
            "selected_or_ema_alert_flow": ("isolated_instrument_only"),
            "telegram_enabled": bool(
                getattr(
                    config,
                    "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED",
                    True,
                )
            ),
            "algo_app_enabled": bool(
                getattr(
                    config,
                    "ALGO_APP_ENABLED",
                    False,
                )
            ),
            "algo_app_url_configured": bool(
                getattr(
                    config,
                    "ALGO_APP_URL",
                    "",
                )
            ),
            "budget_range_enabled": bool(
                getattr(
                    config,
                    "EMA_ALERT_BUDGET_RANGE_ENABLED",
                    True,
                )
            ),
            "budget_range_min_price": getattr(
                config,
                "EMA_ALERT_BUDGET_MIN_PRICE",
                20.0,
            ),
            "budget_range_max_price": getattr(
                config,
                "EMA_ALERT_BUDGET_MAX_PRICE",
                30.0,
            ),
            "budget_range_max_instruments": getattr(
                config,
                "EMA_ALERT_BUDGET_MAX_INSTRUMENTS",
                2,
            ),
            "live_ema_status": live_ema_status,
            "opening_range_processed_count": (self.opening_range_processed_count),
            "opening_range_touch_count": (self.opening_range_touch_count),
            "opening_range_failed_count": (self.opening_range_failed_count),
            "opening_range_broadcast_count": (self.opening_range_broadcast_count),
            "opening_range_alert_flush_count": (self.opening_range_alert_flush_count),
            "opening_range_status": (opening_range_status),
            "ema_cross_include_opening_range_levels": (
                getattr(
                    config,
                    "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
                    True,
                )
            ),
            "market_time": self._now_market_time(),
        }

    # ============================================================
    # Streamer Lifecycle
    # ============================================================

    async def start(self) -> None:
        logger.info(
            "UpstoxStreamer.start() called. " "market_time=%s",
            self._now_market_time(),
        )

        if self.is_running:
            logger.info("UpstoxStreamer already running.")

            return

        self.is_running = True
        self.loop = asyncio.get_running_loop()

        self.task = asyncio.create_task(self._run_loop())

        logger.info(
            "UpstoxStreamer task created. " "market_time=%s, ema_mode=%s",
            self._now_market_time(),
            self._get_live_ema_calculation_mode_text(),
        )

    async def stop(self) -> None:
        logger.info(
            "UpstoxStreamer.stop() called. " "market_time=%s",
            self._now_market_time(),
        )

        self.is_running = False

        if self.streamer:
            try:
                self.streamer.disconnect()

                logger.info("Upstox streamer disconnected.")
            except Exception as ex:
                logger.error(
                    "Upstox streamer disconnect failed: " "%s: %s",
                    type(ex).__name__,
                    ex,
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
                        logger.info("Upstox background task " "cancelled.")
                else:
                    logger.warning("Streamer stop called from " "its own task.")

            except Exception as ex:
                logger.error(
                    "Upstox task cancellation failed: " "%s: %s",
                    type(ex).__name__,
                    ex,
                )
            finally:
                self.task = None

    async def restart(self) -> None:
        logger.info(
            "UpstoxStreamer.restart() called. " "market_time=%s",
            self._now_market_time(),
        )

        try:
            await self.stop()
            await asyncio.sleep(2)
            await self.start()

            logger.info("UpstoxStreamer restarted.")

        except Exception as ex:
            logger.error(
                "UpstoxStreamer restart failed: " "%s: %s",
                type(ex).__name__,
                ex,
            )

            raise

    # ============================================================
    # Connection Loop
    # ============================================================

    async def _run_loop(self) -> None:
        logger.info(
            "Entered Upstox streamer loop. " "market_time=%s",
            self._now_market_time(),
        )

        while self.is_running:
            access_token = token_service.get_access_token()

            if not access_token:
                logger.warning("No Upstox access token. " "Retrying in 10 seconds.")

                await asyncio.sleep(10)
                continue

            try:
                configuration = upstox_client.Configuration()

                configuration.access_token = access_token

                api_client = upstox_client.ApiClient(configuration)

                try:
                    keys = get_subscribed_instrument_keys()
                except Exception as ex:
                    logger.warning(
                        "Subscribed key helper failed. "
                        "Using direct cache. error=%s: %s",
                        type(ex).__name__,
                        ex,
                    )

                    keys = options_cache.get(
                        "subscribed_keys",
                        [],
                    )

                mode = getattr(
                    config,
                    "WEBSOCKET_FEED_MODE",
                    "full",
                )

                logger.info(
                    "WebSocket configuration. " "mode=%s, keys=%s, ema_mode=%s",
                    mode,
                    len(keys),
                    self._get_live_ema_calculation_mode_text(),
                )

                if not keys:
                    logger.warning(
                        "No subscribed instrument keys. " "Waiting 5 seconds."
                    )

                    await asyncio.sleep(5)
                    continue

                self.streamer = upstox_client.MarketDataStreamerV3(
                    api_client,
                    keys,
                    mode,
                )

                def on_open():
                    logger.info(
                        "Connected to Upstox Market " "Stream V3. instruments=%s",
                        len(keys),
                    )

                def on_message(message):
                    self.message_count += 1

                    if not (self._should_process_incoming_message()):
                        return

                    if self.loop and self.loop.is_running():
                        future = asyncio.run_coroutine_threadsafe(
                            self._process_message(message),
                            self.loop,
                        )

                        def callback(
                            completed_future,
                        ):
                            try:
                                completed_future.result()
                            except Exception as ex:
                                logger.error(
                                    "Message processing future " "failed: %s: %s",
                                    type(ex).__name__,
                                    ex,
                                )

                        future.add_done_callback(callback)
                    else:
                        logger.error("Main event loop is unavailable.")

                def on_error(error):
                    logger.error(
                        "Upstox WebSocket error: %s: %s",
                        type(error).__name__,
                        error,
                    )

                def on_close(
                    close_status_code,
                    close_message,
                ):
                    logger.warning(
                        "Upstox WebSocket closed. " "status=%s, message=%s",
                        close_status_code,
                        close_message,
                    )

                self.streamer.on(
                    "open",
                    on_open,
                )

                self.streamer.on(
                    "message",
                    on_message,
                )

                self.streamer.on(
                    "error",
                    on_error,
                )

                self.streamer.on(
                    "close",
                    on_close,
                )

                logger.info("Starting Upstox stream connection.")

                await asyncio.to_thread(self.streamer.connect)

                logger.warning("Upstox streamer connect returned.")

                while self.is_running:
                    await asyncio.sleep(30)

            except asyncio.CancelledError:
                logger.warning("Upstox streamer loop cancelled.")

                break

            except Exception as ex:
                logger.error(
                    "Upstox connection exception: "
                    "%s: %s. Reconnecting in 5 seconds.",
                    type(ex).__name__,
                    ex,
                )

                await asyncio.sleep(5)

        logger.info("Exited Upstox streamer loop.")

    # ============================================================
    # EMA Opening Range Enrichment
    # ============================================================

    def _enrich_ema_event_with_opening_range(
        self,
        instrument_key: str,
        live_ema_cross_event: dict,
    ) -> dict:
        if not isinstance(
            live_ema_cross_event,
            dict,
        ):
            return live_ema_cross_event

        if not bool(
            getattr(
                config,
                "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
                True,
            )
        ):
            live_ema_cross_event.update(
                {
                    "opening_range": {},
                    "touch_status": {},
                    "latest_intraday_close": None,
                    "latest_main_index_ltp": None,
                    "processed_at": None,
                    "isolated_instrument": {},
                }
            )

            return live_ema_cross_event

        try:
            opening_range_payload = get_opening_range_levels_for_ema_event(
                instrument_key
            )

            if isinstance(
                opening_range_payload,
                dict,
            ):
                live_ema_cross_event.update(opening_range_payload)

            self.ema_opening_range_enriched_count += 1

        except Exception as ex:
            self.ema_opening_range_enrichment_failed_count += 1

            logger.error(
                "EMA Opening Range enrichment failed. "
                "instrument_key=%s, error=%s: %s",
                instrument_key,
                type(ex).__name__,
                ex,
            )

            live_ema_cross_event.update(
                {
                    "opening_range": {},
                    "touch_status": {},
                    "latest_intraday_close": None,
                    "latest_main_index_ltp": None,
                    "processed_at": None,
                    "isolated_instrument": {},
                }
            )

        return live_ema_cross_event

    # ============================================================
    # Isolated EMA Processing
    # ============================================================

    def _process_isolated_ema_alert(
        self,
        live_ema_cross_event: dict,
    ) -> bool:
        if not isinstance(
            live_ema_cross_event,
            dict,
        ):
            return False

        telegram_enabled = bool(
            getattr(
                config,
                "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED",
                True,
            )
        )

        algo_app_enabled = bool(
            getattr(
                config,
                "ALGO_APP_ENABLED",
                False,
            )
        )

        if not telegram_enabled and not algo_app_enabled:
            return False

        self.isolated_ema_alert_processed_count += 1
        self.selected_or_ema_alert_processed_count += 1

        try:
            accepted = bool(process_selected_or_ema_cross_alert(live_ema_cross_event))

            if accepted:
                self.isolated_ema_alert_accepted_count += 1
                self.selected_or_ema_alert_sent_count += 1

                logger.info(
                    "Isolated EMA alert accepted. "
                    "instrument_key=%s, "
                    "cross_type=%s, timestamp=%s, "
                    "ema_mode=%s, telegram_enabled=%s, "
                    "algo_app_enabled=%s",
                    live_ema_cross_event.get("instrument_key"),
                    live_ema_cross_event.get("cross_type"),
                    live_ema_cross_event.get("timestamp"),
                    live_ema_cross_event.get("ema_calculation_mode"),
                    telegram_enabled,
                    algo_app_enabled,
                )

                return True

            logger.info(
                "Isolated EMA event not accepted. " "instrument_key=%s, cross_type=%s",
                live_ema_cross_event.get("instrument_key"),
                live_ema_cross_event.get("cross_type"),
            )

            return False

        except Exception as ex:
            self.isolated_ema_alert_failed_count += 1
            self.selected_or_ema_alert_failed_count += 1

            logger.error(
                "Isolated EMA alert processing failed. "
                "instrument_key=%s, error=%s: %s",
                live_ema_cross_event.get("instrument_key"),
                type(ex).__name__,
                ex,
            )

            return False

    def _process_isolated_ema_telegram_alert(
        self,
        live_ema_cross_event: dict,
    ) -> bool:
        return self._process_isolated_ema_alert(live_ema_cross_event)

    # ============================================================
    # Message Processing
    # ============================================================

    async def _process_message(
        self,
        message,
    ) -> None:
        try:
            has_local_clients = broadcaster.get_active_connections_count() > 0

            if isinstance(message, dict):
                tick_dict = message

            elif isinstance(message, str):
                tick_dict = json.loads(message)

            elif isinstance(message, bytes):
                tick_dict = json.loads(message.decode("utf-8"))

            else:
                logger.warning(
                    "Unsupported Upstox message type: %s",
                    type(message),
                )

                return

            feeds = tick_dict.get(
                "feeds",
                {},
            )

            if not isinstance(feeds, dict):
                logger.warning(
                    "Invalid Upstox feeds type: %s",
                    type(feeds),
                )

                return

            if not feeds:
                return

            self.feed_count += len(feeds)

            for instrument_key, tick_data in feeds.items():
                if not tick_data:
                    continue

                contract_info = get_feed_by_instrument_key(instrument_key)

                if contract_info:
                    self.contract_match_count += 1
                else:
                    self.contract_miss_count += 1

                live_ema_cross_event = None
                opening_range_touch_events = []

                try:
                    if bool(
                        getattr(
                            config,
                            "LIVE_EMA_ENABLED",
                            True,
                        )
                    ):
                        live_ema_cross_event = live_ema_service.process_live_feed(
                            instrument_key=(instrument_key),
                            tick_data=tick_data,
                            contract_info=(contract_info),
                        )

                        self.live_ema_processed_count += 1

                        if live_ema_cross_event:
                            self.live_ema_cross_count += 1

                except Exception as ex:
                    self.live_ema_failed_count += 1

                    logger.error(
                        "Live EMA processing failed. "
                        "instrument_key=%s, error=%s: %s",
                        instrument_key,
                        type(ex).__name__,
                        ex,
                    )

                try:
                    if bool(
                        getattr(
                            config,
                            "OPENING_RANGE_TOUCH_ALERT_ENABLED",
                            True,
                        )
                    ):
                        opening_range_touch_events = (
                            process_live_tick_for_opening_range(
                                instrument_key=(instrument_key),
                                tick_data=tick_data,
                                contract_info=(contract_info),
                            )
                            or []
                        )

                        self.opening_range_processed_count += 1

                        if opening_range_touch_events:
                            self.opening_range_touch_count += len(
                                opening_range_touch_events
                            )

                            logger.info(
                                "Opening Range touch generated. "
                                "instrument_key=%s, count=%s",
                                instrument_key,
                                len(opening_range_touch_events),
                            )

                except Exception as ex:
                    self.opening_range_failed_count += 1

                    logger.error(
                        "Opening Range processing failed. "
                        "instrument_key=%s, error=%s: %s",
                        instrument_key,
                        type(ex).__name__,
                        ex,
                    )

                if live_ema_cross_event:
                    live_ema_cross_event = self._enrich_ema_event_with_opening_range(
                        instrument_key=(instrument_key),
                        live_ema_cross_event=(live_ema_cross_event),
                    )

                    logger.info(
                        "Live EMA cross generated. "
                        "instrument_key=%s, "
                        "cross_type=%s, timestamp=%s, "
                        "ema_mode=%s",
                        instrument_key,
                        live_ema_cross_event.get("cross_type"),
                        live_ema_cross_event.get("timestamp"),
                        live_ema_cross_event.get("ema_calculation_mode"),
                    )

                    self._process_isolated_ema_alert(live_ema_cross_event)

                if live_ema_cross_event and has_local_clients:
                    try:
                        if hasattr(
                            broadcaster,
                            "broadcast_ema_cross",
                        ):
                            await broadcaster.broadcast_ema_cross(live_ema_cross_event)

                    except Exception as ex:
                        logger.error(
                            "EMA broadcast failed. "
                            "instrument_key=%s, "
                            "error=%s: %s",
                            instrument_key,
                            type(ex).__name__,
                            ex,
                        )

                if opening_range_touch_events and has_local_clients:
                    for opening_range_event in opening_range_touch_events:
                        try:
                            if hasattr(
                                broadcaster,
                                "broadcast_opening_range",
                            ):
                                await broadcaster.broadcast_opening_range(
                                    opening_range_event
                                )

                                self.opening_range_broadcast_count += 1

                        except Exception as ex:
                            logger.error(
                                "Opening Range broadcast failed. "
                                "instrument_key=%s, "
                                "error=%s: %s",
                                instrument_key,
                                type(ex).__name__,
                                ex,
                            )

                if not has_local_clients:
                    continue

                try:
                    await broadcaster.broadcast_tick(
                        instrument_key,
                        tick_data,
                        contract_info,
                    )

                    self.broadcast_success_count += 1

                except Exception as ex:
                    self.broadcast_failed_count += 1

                    logger.error(
                        "Tick broadcast failed. " "instrument_key=%s, error=%s: %s",
                        instrument_key,
                        type(ex).__name__,
                        ex,
                    )

            try:
                if bool(
                    getattr(
                        config,
                        "OPENING_RANGE_TOUCH_ALERT_ENABLED",
                        True,
                    )
                ):
                    sent = flush_pending_touch_alerts(
                        force=False,
                        source="live_tick",
                    )

                    if sent:
                        self.opening_range_alert_flush_count += 1

            except Exception as ex:
                logger.error(
                    "Opening Range alert flush failed: " "%s: %s",
                    type(ex).__name__,
                    ex,
                )

        except json.JSONDecodeError as ex:
            logger.error(
                "Upstox JSON decode failed: %s: %s",
                type(ex).__name__,
                ex,
            )

        except Exception as ex:
            logger.error(
                "Upstox message processing failed: " "%s: %s",
                type(ex).__name__,
                ex,
            )


upstox_streamer = UpstoxStreamer()
