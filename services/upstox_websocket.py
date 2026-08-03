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
        self.loop = None  # Holds reference to the main asyncio event loop

    async def start(self):
        """Starts WebSocket streamer in the background."""
        if self.is_running:
            return
        self.is_running = True
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.create_task(self._run_loop())

    async def stop(self):
        """Gracefully disconnects the streamer."""
        self.is_running = False
        if self.streamer:
            try:
                self.streamer.disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting Upstox Streamer: {e}")
        if self.task:
            self.task.cancel()

    async def _run_loop(self):
        while self.is_running:
            access_token = token_service.get_access_token()
            if not access_token:
                logger.warning("No access token found in token_service. Retrying in 10s...")
                await asyncio.sleep(10)
                continue

            try:
                # 1. Configure SDK Client
                configuration = upstox_client.Configuration()
                configuration.access_token = access_token
                api_client = upstox_client.ApiClient(configuration)

                # 2. Extract instrument keys cached on startup
                keys = options_cache.get("subscribed_keys", [])
                mode = getattr(config, "WEBSOCKET_FEED_MODE", "full")

                if not keys:
                    logger.warning("No instrument keys found in options_cache to subscribe. Waiting 5s...")
                    await asyncio.sleep(5)
                    continue

                # 3. Instantiate Official MarketDataStreamerV3 using positional args
                self.streamer = upstox_client.MarketDataStreamerV3(
                    api_client,
                    keys,
                    mode
                )

                # 4. Attach Event Listeners
                def on_open():
                    logger.info("Connected to Upstox Market Stream V3 WebSocket successfully.")
                    logger.info(f"Subscribed to {len(keys)} instruments.")

                def on_message(message):
                    if self.loop and self.loop.is_running():
                        asyncio.run_coroutine_threadsafe(self._process_message(message), self.loop)

                def on_error(error):
                    logger.error(f"Upstox WebSocket Error: {error}")

                def on_close(close_status_code, close_msg):
                    logger.warning(f"Upstox WebSocket Closed: {close_status_code} - {close_msg}")

                self.streamer.on("open", on_open)
                self.streamer.on("message", on_message)
                self.streamer.on("error", on_error)
                self.streamer.on("close", on_close)

                # 5. Connect Streamer
                self.streamer.connect()

                # Keep loop running while streamer is active
                while self.is_running:
                    await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"WebSocket Connection Exception: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _process_message(self, message):
        """Routes decoded ticks directly to FastAPI WebSocket Broadcaster."""
        try:
            if isinstance(message, dict):
                tick_dict = message
            elif isinstance(message, (str, bytes)):
                tick_dict = json.loads(message)
            else:
                return

            # Skip non-live feed packets (e.g., market_info)
            msg_type = tick_dict.get("type")
            if msg_type and msg_type not in ("live_feed", "feed", None):
                return

            feeds = tick_dict.get("feeds", {})
            if not isinstance(feeds, dict):
                return

            for key, tick_data in feeds.items():
                if not tick_data:
                    continue
                    
                contract_info = get_feed_by_instrument_key(key)
                
                try:
                    await broadcaster.broadcast_tick(key, tick_data, contract_info)
                except Exception as b_ex:
                    logger.error(f"Broadcaster failed for key {key}: {b_ex}")

        except Exception as ex:
            logger.error(f"Error processing tick message: {ex}")


upstox_streamer = UpstoxStreamer()