import json
import logging
from collections import defaultdict
from typing import Dict, Set

from fastapi import WebSocket

logger = logging.getLogger("uvicorn")


class WebSocketManager:
    """
    Client Subscription Manager

    Structure:

    {
        "NSE_FO|63935": {
            0: {ws1, ws2},
            1: {ws3},
            3: {ws4},
        }
    }

    interval:
        0 = live ticks
        1 = 1 minute candle
        3 = 3 minute candle
        5 = 5 minute candle
    """

    def __init__(self):

        self.connections: Dict[str, Dict[int, Set[WebSocket]]] = defaultdict(
            lambda: defaultdict(set)
        )

    # --------------------------------------------------
    # Client Lifecycle
    # --------------------------------------------------

    async def connect(
        self,
        websocket: WebSocket,
    ):
        """
        Accept incoming websocket.
        """

        await websocket.accept()

        logger.info("WebSocket client connected.")

    async def disconnect(
        self,
        websocket: WebSocket,
    ):
        """
        Remove websocket from all subscriptions.
        """

        removed = 0

        for instrument_map in self.connections.values():

            for ws_set in instrument_map.values():

                if websocket in ws_set:

                    ws_set.remove(websocket)
                    removed += 1

        logger.info(f"Client disconnected. " f"Removed {removed} subscriptions.")

    # --------------------------------------------------
    # Subscription Management
    # --------------------------------------------------

    def subscribe(
        self,
        websocket: WebSocket,
        instrument_key: str,
        interval: int = 0,
    ):
        """
        Subscribe client.

        interval:
            0=ticks
            1=1m
            3=3m
            5=5m
        """

        self.connections[instrument_key][interval].add(websocket)

        logger.info(f"Subscribed: {instrument_key} " f"(interval={interval})")

    def unsubscribe(
        self,
        websocket: WebSocket,
        instrument_key: str,
        interval: int = 0,
    ):
        """
        Remove subscription.
        """

        try:

            self.connections[instrument_key][interval].discard(websocket)

            logger.info(f"Unsubscribed: " f"{instrument_key} " f"(interval={interval})")

        except Exception:
            pass

    # --------------------------------------------------
    # Publishers
    # --------------------------------------------------

    async def publish_tick(
        self,
        instrument_key: str,
        payload: dict,
    ):
        """
        Send live tick subscribers.

        interval = 0
        """

        subscribers = self.connections.get(
            instrument_key,
            {},
        ).get(
            0,
            set(),
        )

        if not subscribers:
            return

        dead_connections = []

        for websocket in subscribers:

            try:

                await websocket.send_json(payload)

            except Exception:

                dead_connections.append(websocket)

        for websocket in dead_connections:

            subscribers.discard(websocket)

    async def publish_candle(
        self,
        instrument_key: str,
        interval: int,
        candle: dict,
    ):
        """
        Publish completed candle.
        """

        subscribers = self.connections.get(
            instrument_key,
            {},
        ).get(
            interval,
            set(),
        )

        if not subscribers:
            return

        dead_connections = []

        for websocket in subscribers:

            try:

                await websocket.send_json(candle)

            except Exception:

                dead_connections.append(websocket)

        for websocket in dead_connections:

            subscribers.discard(websocket)

    async def publish_message(
        self,
        instrument_key: str,
        interval: int,
        payload: dict,
    ):
        """
        Generic publisher.
        """

        subscribers = self.connections.get(
            instrument_key,
            {},
        ).get(
            interval,
            set(),
        )

        if not subscribers:
            return

        dead_connections = []

        for websocket in subscribers:

            try:

                await websocket.send_text(json.dumps(payload))

            except Exception:

                dead_connections.append(websocket)

        for websocket in dead_connections:

            subscribers.discard(websocket)

    # --------------------------------------------------
    # Stats
    # --------------------------------------------------

    def get_stats(self):
        """
        Useful for diagnostics API.
        """

        output = {}

        for instrument_key, intervals in self.connections.items():

            output[instrument_key] = {}

            for interval, clients in intervals.items():

                output[instrument_key][interval] = len(clients)

        return output

    def get_total_clients(self):

        unique_clients = set()

        for intervals in self.connections.values():

            for clients in intervals.values():

                unique_clients.update(clients)

        return len(unique_clients)

    def clear(self):

        self.connections.clear()

        logger.info("WebSocket manager cleared.")


websocket_manager = WebSocketManager()
