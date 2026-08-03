import asyncio
import json
import upstox_client

from core import config
from core.logger import get_logger
from services.token_service import token_service
from services.option_service import options_cache, get_feed_by_instrument_key
from ws_feed.broadcaster import broadcaster

logger = get_logger(__file__)


class UpstoxStreamer:
    def __init__(self):
        self.is_running = False
        self.streamer = None
        self.task = None
        self.loop = None

        # Runtime counters. Kept for internal status/debug if needed.
        self.message_count = 0
        self.feed_count = 0
        self.broadcast_success_count = 0
        self.broadcast_failed_count = 0
        self.contract_match_count = 0
        self.contract_miss_count = 0

    async def start(self):
        """Starts Upstox WebSocket streamer in the background."""
        logger.info("UpstoxStreamer.start() called")

        if self.is_running:
            logger.info("UpstoxStreamer already running. Skipping start.")
            return

        self.is_running = True
        self.loop = asyncio.get_running_loop()

        self.task = asyncio.create_task(self._run_loop())

        logger.info("UpstoxStreamer background task created successfully")

    async def stop(self):
        """Gracefully disconnects the streamer."""
        logger.info("UpstoxStreamer.stop() called")

        self.is_running = False

        if self.streamer:
            try:
                logger.info("Disconnecting Upstox streamer")
                self.streamer.disconnect()
                logger.info("Upstox streamer disconnected successfully")
            except Exception as e:
                logger.error(
                    f"Error disconnecting Upstox Streamer: "
                    f"{type(e).__name__}: {e}"
                )

        if self.task:
            try:
                self.task.cancel()
                logger.info("Upstox background task cancelled")
            except Exception as e:
                logger.error(
                    f"Error cancelling Upstox task: "
                    f"{type(e).__name__}: {e}"
                )

    async def _run_loop(self):
        """Main Upstox connection loop."""
        logger.info("Entered UpstoxStreamer._run_loop")

        while self.is_running:
            access_token = token_service.get_access_token()

            if not access_token:
                logger.warning("No access token found in token_service. Retrying in 10s...")
                await asyncio.sleep(10)
                continue

            try:
                logger.info("Configuring Upstox SDK client")

                configuration = upstox_client.Configuration()
                configuration.access_token = access_token
                api_client = upstox_client.ApiClient(configuration)

                keys = options_cache.get("subscribed_keys", [])
                mode = getattr(config, "WEBSOCKET_FEED_MODE", "full")

                logger.info(f"WebSocket feed mode: {mode}")
                logger.info(f"Subscribed keys count from options_cache: {len(keys)}")

                if not keys:
                    logger.warning("No instrument keys found in options_cache. Waiting 5s...")
                    await asyncio.sleep(5)
                    continue

                logger.info("Creating Upstox MarketDataStreamerV3 instance")

                self.streamer = upstox_client.MarketDataStreamerV3(
                    api_client,
                    keys,
                    mode,
                )

                logger.info("MarketDataStreamerV3 instance created successfully")

                def on_open():
                    logger.info("Connected to Upstox Market Stream V3 WebSocket successfully.")
                    logger.info(f"Subscribed to {len(keys)} instruments.")

                def on_message(message):
                    """
                    Upstox tick callback.

                    Important:
                    If no local WebSocket clients are connected, do not schedule
                    tick processing on FastAPI event loop. This prevents event-loop
                    overload and allows /option handshakes to complete quickly.
                    """
                    self.message_count += 1

                    if broadcaster.get_active_connections_count() == 0:
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
                                    f"{type(ex).__name__}: {ex}"
                                )

                        future.add_done_callback(callback)

                    else:
                        logger.error("Main event loop not available or not running")

                def on_error(error):
                    logger.error(
                        f"Upstox WebSocket Error: "
                        f"{type(error).__name__}: {error}"
                    )

                def on_close(close_status_code, close_msg):
                    logger.warning(
                        f"Upstox WebSocket Closed: "
                        f"{close_status_code} - {close_msg}"
                    )

                logger.info("Attaching Upstox event listeners")

                self.streamer.on("open", on_open)
                self.streamer.on("message", on_message)
                self.streamer.on("error", on_error)
                self.streamer.on("close", on_close)

                logger.info("Upstox event listeners attached successfully")

                # Important:
                # connect() can block, so run it in a thread.
                logger.info("Starting Upstox Market Stream V3 connection")
                await asyncio.to_thread(self.streamer.connect)

                logger.warning(
                    "self.streamer.connect() returned. "
                    "This may mean stream closed or SDK returned control."
                )

                # Keep loop alive if connect returns.
                # No heartbeat logs here to avoid console noise.
                while self.is_running:
                    await asyncio.sleep(30)

            except asyncio.CancelledError:
                logger.warning("Upstox _run_loop task cancelled")
                break

            except Exception as e:
                logger.error(
                    f"WebSocket Connection Exception: "
                    f"{type(e).__name__}: {e}. Reconnecting in 5s..."
                )
                await asyncio.sleep(5)

        logger.info("Exiting UpstoxStreamer._run_loop")

    async def _process_message(self, message):
        """Routes decoded ticks to FastAPI WebSocket Broadcaster."""
        try:
            # Double-check local client count.
            # If clients disconnected after on_message scheduled this task, skip processing.
            if broadcaster.get_active_connections_count() == 0:
                return

            if isinstance(message, dict):
                tick_dict = message

            elif isinstance(message, str):
                tick_dict = json.loads(message)

            elif isinstance(message, bytes):
                decoded_message = message.decode("utf-8")
                tick_dict = json.loads(decoded_message)

            else:
                logger.warning(f"Unsupported Upstox message type: {type(message)}")
                return

            feeds = tick_dict.get("feeds", {})

            if not isinstance(feeds, dict):
                logger.warning(f"Invalid feeds object type: {type(feeds)}")
                return

            if len(feeds) == 0:
                return

            self.feed_count += len(feeds)

            for key, tick_data in feeds.items():
                if not tick_data:
                    continue

                # If all clients disconnected while processing, stop immediately.
                if broadcaster.get_active_connections_count() == 0:
                    return

                contract_info = get_feed_by_instrument_key(key)

                if contract_info:
                    self.contract_match_count += 1
                else:
                    self.contract_miss_count += 1

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
                        f"{type(b_ex).__name__}: {b_ex}"
                    )

        except json.JSONDecodeError as json_ex:
            logger.error(
                f"JSON decode failed in Upstox message: "
                f"{type(json_ex).__name__}: {json_ex}"
            )

        except Exception as ex:
            logger.error(
                f"Error processing Upstox tick message: "
                f"{type(ex).__name__}: {ex}"
            )


upstox_streamer = UpstoxStreamer()