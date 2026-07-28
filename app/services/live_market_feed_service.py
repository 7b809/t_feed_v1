# app/services/live_market_feed_service.py

import asyncio
import json
import logging
import threading
from datetime import datetime

import upstox_client

from app.database import (
    token_state,
    refresh_token_if_changed,
)

from app.upstox_services.fetch_options import (
    options_cache,
    NIFTY_INDEX_FEED,
)

from app.services.feed_cache_service import (
    update_live_feed,
)

from app.websocket.websocket_manager import (
    websocket_manager,
)

logger = logging.getLogger("uvicorn")


class LiveMarketFeedService:
    """
    Handles Upstox MarketDataStreamerV3 connection.

    Responsibilities:
    -----------------
    1. Connect to Upstox websocket
    2. Subscribe Nifty index + filtered option instruments
    3. Receive full live ticks
    4. Store full feed payload in local cache
    5. Publish full ticks to custom websocket subscribers
    6. Forward only option feeds to live EMA/candle engine
    7. Manage reconnects

    Important:
    ----------
    Nifty Index is subscribed only for live tick websocket feed.
    It is NOT used for EMA calculation or candle aggregation.
    """

    def __init__(self):
        self.streamer = None
        self.connected = False
        self.subscribed = False
        self.running = False
        self.ws_thread = None

        # FastAPI event loop reference.
        # Needed because Upstox websocket callbacks run in a separate thread.
        self.event_loop = None

        self.tick_count = 0

    # --------------------------------------------------
    # Helper Methods
    # --------------------------------------------------

    def get_nifty_index_key(self):
        """
        Returns configured Nifty index instrument key.
        """

        return NIFTY_INDEX_FEED.get(
            "instrument_key",
            "NSE_INDEX|Nifty 50",
        )

    def is_nifty_index(self, instrument_key):
        """
        Check whether instrument is Nifty Index.
        """

        return instrument_key == self.get_nifty_index_key()

    def get_subscription_instruments(self):
        """
        Returns instrument keys for Upstox subscription.

        Includes:
        - Nifty Index
        - All filtered option contracts
        """

        instruments = []

        # Always subscribe to Nifty Index
        instruments.append(self.get_nifty_index_key())

        # Subscribe all filtered option instruments
        data = options_cache.get("data", [])

        for item in data:
            instrument_key = item.get("instrument_key")

            if instrument_key:
                instruments.append(instrument_key)

        # Remove duplicates while preserving order
        unique_instruments = list(dict.fromkeys(instruments))

        return unique_instruments

    def publish_tick_to_clients(
        self,
        instrument_key,
        payload,
    ):
        """
        Publish full tick payload to custom websocket clients.

        interval=0 means live tick feed.
        """

        if not self.event_loop:
            return

        try:
            asyncio.run_coroutine_threadsafe(
                websocket_manager.publish_tick(
                    instrument_key=instrument_key,
                    payload=payload,
                ),
                self.event_loop,
            )

        except Exception as ex:
            logger.error(
                f"Failed publishing tick to clients for " f"{instrument_key}: {ex}"
            )

    # --------------------------------------------------
    # WebSocket Events
    # --------------------------------------------------

    def on_open(self):
        """
        Called automatically once Upstox websocket connects.
        """

        logger.info("Upstox WebSocket Connected")

        self.connected = True

        instruments = self.get_subscription_instruments()

        if not instruments:
            logger.warning("No instrument keys available for subscription.")
            return

        try:
            logger.info(f"Subscribing {len(instruments)} instruments in FULL mode...")

            self.streamer.subscribe(
                instruments,
                "full",
            )

            self.subscribed = True

            logger.info("Instrument subscription completed successfully.")

        except Exception as ex:
            logger.exception(f"Subscription failed: {ex}")

    def on_message(self, message):
        """
        Called for every incoming Upstox market feed message.
        """

        try:
            if isinstance(message, str):
                payload = json.loads(message)
            else:
                payload = message

            self.tick_count += 1

            current_ts = payload.get("currentTs")

            feeds = payload.get("feeds", {})

            # Market status / heartbeat messages may not contain feeds.
            if not feeds:
                return

            for instrument_key, feed_data in feeds.items():

                # --------------------------------------------------
                # 1. Store complete full feed payload in cache
                # --------------------------------------------------

                try:
                    update_live_feed(
                        instrument_key=instrument_key,
                        feed=feed_data,
                        current_ts=current_ts,
                    )

                except Exception as ex:
                    logger.error(
                        f"Failed updating live feed cache for "
                        f"{instrument_key}: {ex}"
                    )

                # --------------------------------------------------
                # 2. Publish full tick to custom websocket clients
                #    interval=0 clients receive this payload
                # --------------------------------------------------

                tick_payload = {
                    "feed_type": "tick",
                    "instrument_key": instrument_key,
                    "current_ts": current_ts,
                    "received_at": datetime.now().isoformat(),
                    "data": feed_data,
                }

                self.publish_tick_to_clients(
                    instrument_key=instrument_key,
                    payload=tick_payload,
                )

                # --------------------------------------------------
                # 3. Nifty Index is live tick only.
                #    Do NOT send Nifty to EMA/candle engine.
                # --------------------------------------------------

                if self.is_nifty_index(instrument_key):
                    continue

                # --------------------------------------------------
                # 4. Forward only option instruments to EMA/candle service
                # --------------------------------------------------

                try:
                    from app.services.live_ema_service import (
                        live_ema_service,
                    )

                    live_ema_service.process_market_feed(
                        instrument_key=instrument_key,
                        feed=feed_data,
                        current_ts=current_ts,
                    )

                except Exception as ex:
                    logger.error(
                        f"EMA processing failed for " f"{instrument_key}: {ex}"
                    )

        except Exception as ex:
            logger.exception(f"Message processing failed: {ex}")

    def on_error(self, error):
        """
        Upstox websocket error callback.
        """

        logger.error(f"WebSocket Error: {error}")

        self.connected = False
        self.subscribed = False

    def on_close(self, *args):
        """
        Upstox websocket closed callback.
        """

        logger.warning("Upstox WebSocket Closed")

        self.connected = False
        self.subscribed = False

    # --------------------------------------------------
    # Connection Lifecycle
    # --------------------------------------------------

    def _connect(self):
        """
        Internal websocket bootstrap.
        Runs inside a background thread.
        """

        try:
            # ----------------------------------------------
            # Sync token before opening websocket
            # ----------------------------------------------

            try:
                token_changed = refresh_token_if_changed()

                if token_changed:
                    logger.info("Latest Upstox token loaded before websocket startup.")

            except Exception as ex:
                logger.error(f"Token synchronization failed: {ex}")

            access_token = token_state.access_token

            if not access_token:
                logger.error("Access token not available.")
                return

            configuration = upstox_client.Configuration()
            configuration.access_token = access_token

            api_client = upstox_client.ApiClient(configuration)

            self.streamer = upstox_client.MarketDataStreamerV3(api_client)

            self.streamer.on(
                "open",
                self.on_open,
            )

            self.streamer.on(
                "message",
                self.on_message,
            )

            self.streamer.on(
                "error",
                self.on_error,
            )

            self.streamer.on(
                "close",
                self.on_close,
            )

            logger.info("Connecting to Upstox WebSocket...")

            self.streamer.connect()

        except Exception as ex:
            logger.exception(f"WebSocket connection failed: {ex}")

    async def start(self):
        """
        Start websocket service.
        """

        if self.running:
            logger.info("WebSocket service already running.")
            return

        logger.info("Starting Live Market Feed Service...")

        self.running = True

        # Capture FastAPI event loop for publishing messages
        # from Upstox websocket background thread.
        try:
            self.event_loop = asyncio.get_running_loop()
        except Exception:
            self.event_loop = None

        self.ws_thread = threading.Thread(
            target=self._connect,
            daemon=True,
        )

        self.ws_thread.start()

    async def stop(self):
        """
        Stop websocket service.
        """

        logger.info("Stopping Live Market Feed Service...")

        try:
            self.running = False

            if self.streamer:
                try:
                    self.streamer.disconnect()
                except Exception:
                    pass

            self.connected = False
            self.subscribed = False
            self.streamer = None

            logger.info("Live Market Feed Service stopped.")

        except Exception as ex:
            logger.exception(f"Failed stopping websocket: {ex}")

    async def reconnect(self):
        """
        Force websocket reconnect.
        """

        logger.info("Reconnecting WebSocket...")

        await self.stop()
        await self.start()

    # --------------------------------------------------
    # Status
    # --------------------------------------------------

    def get_status(self):
        """
        Runtime connection status.
        """

        instruments = self.get_subscription_instruments()

        return {
            "running": self.running,
            "connected": self.connected,
            "subscribed": self.subscribed,
            "subscription_count": len(instruments),
            "subscribed_instruments": instruments,
            "nifty_index_key": self.get_nifty_index_key(),
            "nifty_index_mode": "tick_only",
            "tick_count": self.tick_count,
            "token_updated_at": token_state.updated_at,
            "timestamp": datetime.now().isoformat(),
        }


market_feed_service = LiveMarketFeedService()
