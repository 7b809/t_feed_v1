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
from ws_feed.broadcaster import broadcaster

logger = get_logger(__file__)


class UpstoxStreamer:
    def __init__(self):
        self.is_running = False
        self.streamer = None
        self.task = None
        self.loop = None

        # Market timezone loaded from core/config.py
        self.market_timezone = self._load_market_timezone()
        self.market_time_format = getattr(
            config,
            "MARKET_TIME_FORMAT",
            "%Y-%m-%d %H:%M:%S %Z",
        )

        # Runtime counters. Kept for internal status/debug if needed.
        self.message_count = 0
        self.feed_count = 0
        self.broadcast_success_count = 0
        self.broadcast_failed_count = 0
        self.contract_match_count = 0
        self.contract_miss_count = 0

        # Live EMA counters
        self.live_ema_processed_count = 0
        self.live_ema_cross_count = 0
        self.live_ema_failed_count = 0

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

    def _should_process_incoming_message(self) -> bool:
        """
        Decides whether incoming Upstox messages should be scheduled for processing.

        Old behavior:
            Process only when local browser/WebSocket clients are connected.

        New behavior:
            Process if either:
            1. local WebSocket clients are connected, or
            2. LIVE_EMA_ENABLED=True

        Reason:
            Live EMA crossover detection must continue even when no dashboard/browser
            client is connected.
        """

        connected_clients = broadcaster.get_active_connections_count()

        if connected_clients > 0:
            return True

        return bool(getattr(config, "LIVE_EMA_ENABLED", True))

    def get_status(self) -> dict:
        """
        Returns current streamer status.

        Useful for health/debug endpoints.
        """

        live_ema_status = {}

        try:
            live_ema_status = live_ema_service.get_status()
        except Exception as ex:
            live_ema_status = {
                "status": "error",
                "error": f"{type(ex).__name__}: {ex}",
            }

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
            "live_ema_status": live_ema_status,
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
            f"market_time={self._now_market_time()}"
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

        Use this after:
            1. token_service.refresh_tokens()
            2. get_options_contracts(save_data=True)
            3. options_cache["subscribed_keys"] is updated
            4. Historical EMA is calculated
            5. live_ema_service is initialized from historical EMA summary
        """

        logger.info(f"UpstoxStreamer.restart() called at {self._now_market_time()}")

        try:
            await self.stop()

            # Small delay to allow SDK websocket cleanup.
            await asyncio.sleep(2)

            await self.start()

            logger.info(
                f"UpstoxStreamer restarted successfully with latest "
                f"subscription keys. market_time={self._now_market_time()}"
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

        logger.info(f"Entered UpstoxStreamer._run_loop at {self._now_market_time()}")

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

                # Get current subscription keys safely from option_service helper.
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

                def on_message(message):
                    """
                    Upstox tick callback.

                    Important:
                    Old behavior skipped message processing when no local browser/WebSocket
                    clients were connected.

                    New behavior:
                    We still process messages when LIVE_EMA_ENABLED=True because live EMA
                    crossover detection must continue even without UI clients.
                    """

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

                # Important:
                # connect() can block, so run it in a thread.
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

                # Keep loop alive if connect returns.
                # No heartbeat logs here to avoid console noise.
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

    async def _process_message(self, message):
        """Routes decoded ticks to live EMA service and FastAPI WebSocket Broadcaster."""

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

                # --------------------------------------------------------
                # 1. Process live EMA crossover continuation.
                # --------------------------------------------------------
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

                            logger.info(
                                f"Live EMA cross event generated for {key}: "
                                f"{live_ema_cross_event.get('cross_type')} "
                                f"at {live_ema_cross_event.get('timestamp')}"
                            )

                except Exception as ema_ex:
                    self.live_ema_failed_count += 1

                    logger.error(
                        f"Live EMA processing failed for key {key}: "
                        f"{type(ema_ex).__name__}: {ema_ex}. "
                        f"market_time={self._now_market_time()}"
                    )

                # --------------------------------------------------------
                # 2. Broadcast live EMA cross event if broadcaster supports it.
                # --------------------------------------------------------
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

                # --------------------------------------------------------
                # 3. Broadcast normal live tick only when local clients exist.
                # --------------------------------------------------------
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