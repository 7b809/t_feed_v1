from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from core import config
from core.logger import get_logger
from models.ema_events import EmaEvent

logger = get_logger(__file__)


ALL_INSTRUMENTS = "*"


@dataclass
class WebSocketConnection:
    """
    Represents one connected EMA WebSocket client.
    """

    websocket: WebSocket

    subscriptions: set[str] = field(default_factory=set)

    connected_at: str | None = None

    client_id: str | None = None


class EmaWebSocketManager:
    """
    Central manager for EMA WebSocket connections.

    Supported subscription modes:

        1. Single instrument:
           {"instrument_keys": ["NSE_FO|..."]}

        2. Multiple instruments:
           {"instrument_keys": ["NSE_FO|...", "NSE_FO|..."]}

        3. All instruments:
           {"instrument_keys": ["*"]}

    The EMA runtime calculates EMA and crossover events only once.
    This manager only routes events to matching WebSocket clients.
    """

    def __init__(self) -> None:
        self.connections: dict[int, WebSocketConnection] = {}

        self._lock = asyncio.Lock()

        self._broadcast_lock = asyncio.Lock()

        self._connection_counter = 0

    # ========================================================
    # CONNECTION MANAGEMENT
    # ========================================================

    async def connect(
        self,
        websocket: WebSocket,
        *,
        client_id: str | None = None,
    ) -> WebSocketConnection:
        """
        Accept and register a new WebSocket connection.
        """

        if not config.EMA_WEBSOCKET_ENABLED:
            raise RuntimeError("EMA WebSocket is disabled")

        async with self._lock:
            current_connections = len(self.connections)

            if current_connections >= config.EMA_WS_MAX_CONNECTIONS:
                logger.warning(
                    "WebSocket connection rejected "
                    "reason=max_connections_reached "
                    "current=%s max=%s",
                    current_connections,
                    config.EMA_WS_MAX_CONNECTIONS,
                )

                raise RuntimeError("Maximum WebSocket connections reached")

            await websocket.accept()

            self._connection_counter += 1

            connection_id = self._connection_counter

            connection = WebSocketConnection(
                websocket=websocket,
                client_id=client_id or f"ema-client-{connection_id}",
                connected_at=(
                    __import__("datetime").datetime.now().astimezone().isoformat()
                ),
            )

            self.connections[connection_id] = connection

            logger.info(
                "EMA WebSocket connected "
                "connection_id=%s client_id=%s "
                "active_connections=%s",
                connection_id,
                connection.client_id,
                len(self.connections),
            )

            return connection

    async def disconnect(
        self,
        connection: WebSocketConnection,
    ) -> None:
        """
        Remove a connection from the manager.
        """

        async with self._lock:
            connection_id = self._get_connection_id(connection)

            if connection_id is None:
                return

            self.connections.pop(
                connection_id,
                None,
            )

            logger.info(
                "EMA WebSocket disconnected "
                "connection_id=%s client_id=%s "
                "active_connections=%s",
                connection_id,
                connection.client_id,
                len(self.connections),
            )

    def _get_connection_id(
        self,
        connection: WebSocketConnection,
    ) -> int | None:
        """
        Find the internal ID of a connection.
        """

        for connection_id, current in self.connections.items():
            if current is connection:
                return connection_id

        return None

    # ========================================================
    # SUBSCRIPTIONS
    # ========================================================

    async def subscribe(
        self,
        connection: WebSocketConnection,
        instrument_keys: list[str],
    ) -> dict[str, Any]:
        """
        Subscribe a client to one, multiple, or all instruments.

        The wildcard '*' represents all available instruments.
        """

        normalized = self._normalize_instrument_keys(instrument_keys)

        if not normalized:
            raise ValueError("At least one valid instrument key is required")

        if ALL_INSTRUMENTS in normalized:
            connection.subscriptions = {ALL_INSTRUMENTS}

            logger.info(
                "WebSocket subscribed to all instruments " "client_id=%s",
                connection.client_id,
            )

        else:
            connection.subscriptions.update(normalized)

            logger.info(
                "WebSocket instrument subscription updated "
                "client_id=%s instruments=%s",
                connection.client_id,
                sorted(connection.subscriptions),
            )

        return self.subscription_status(connection)

    async def unsubscribe(
        self,
        connection: WebSocketConnection,
        instrument_keys: list[str],
    ) -> dict[str, Any]:
        """
        Remove one or more instrument subscriptions.
        """

        normalized = self._normalize_instrument_keys(instrument_keys)

        if ALL_INSTRUMENTS in normalized:
            connection.subscriptions.clear()

        else:
            connection.subscriptions.difference_update(normalized)

        logger.info(
            "WebSocket subscriptions removed " "client_id=%s instruments=%s",
            connection.client_id,
            sorted(normalized),
        )

        return self.subscription_status(connection)

    async def subscribe_all(
        self,
        connection: WebSocketConnection,
    ) -> dict[str, Any]:
        """
        Subscribe a client to all instruments.
        """

        connection.subscriptions = {ALL_INSTRUMENTS}

        logger.info(
            "WebSocket subscribed to all instruments " "client_id=%s",
            connection.client_id,
        )

        return self.subscription_status(connection)

    async def unsubscribe_all(
        self,
        connection: WebSocketConnection,
    ) -> dict[str, Any]:
        """
        Remove all subscriptions from a client.
        """

        connection.subscriptions.clear()

        logger.info(
            "WebSocket unsubscribed from all instruments " "client_id=%s",
            connection.client_id,
        )

        return self.subscription_status(connection)

    def _normalize_instrument_keys(
        self,
        instrument_keys: list[str] | None,
    ) -> set[str]:
        """
        Normalize and validate instrument keys.
        """

        if not instrument_keys:
            return set()

        normalized: set[str] = set()

        for instrument_key in instrument_keys:
            if not isinstance(instrument_key, str):
                continue

            value = instrument_key.strip()

            if not value:
                continue

            if value == ALL_INSTRUMENTS:
                return {ALL_INSTRUMENTS}

            normalized.add(value)

        return normalized

    def subscription_status(
        self,
        connection: WebSocketConnection,
    ) -> dict[str, Any]:
        """
        Return the current subscription state.
        """

        subscriptions = sorted(connection.subscriptions)

        return {
            "event": "ema.subscription_status",
            "client_id": connection.client_id,
            "subscriptions": subscriptions,
            "subscribed_to_all": (ALL_INSTRUMENTS in connection.subscriptions),
            "subscription_count": len(connection.subscriptions),
        }

    # ========================================================
    # EVENT MATCHING
    # ========================================================

    def is_subscribed(
        self,
        connection: WebSocketConnection,
        instrument_key: str | None,
    ) -> bool:
        """
        Check whether a connection should receive an event.
        """

        if ALL_INSTRUMENTS in connection.subscriptions:
            return True

        if not instrument_key:
            return False

        return instrument_key in connection.subscriptions

    def get_matching_connections(
        self,
        instrument_key: str | None,
    ) -> list[WebSocketConnection]:
        """
        Return connections subscribed to the instrument.
        """

        return [
            connection
            for connection in self.connections.values()
            if self.is_subscribed(
                connection,
                instrument_key,
            )
        ]

    # ========================================================
    # BROADCASTING
    # ========================================================

    async def broadcast_event(
        self,
        event: EmaEvent | dict[str, Any],
    ) -> dict[str, int]:
        """
        Broadcast one centralized EMA event.

        The event is calculated by the runtime.
        This manager does not calculate or modify EMA values.
        """

        if not config.EMA_WEBSOCKET_ENABLED:
            return {
                "sent": 0,
                "failed": 0,
                "skipped": 0,
            }

        if isinstance(event, EmaEvent):
            payload = event.to_dict()

        elif isinstance(event, dict):
            payload = dict(event)

        else:
            raise TypeError("event must be EmaEvent or dictionary")

        instrument_key = payload.get("instrument_key")

        async with self._broadcast_lock:
            matching_connections = self.get_matching_connections(instrument_key)

            if not matching_connections:
                return {
                    "sent": 0,
                    "failed": 0,
                    "skipped": 0,
                }

            results = await asyncio.gather(
                *[
                    self._send_to_connection(
                        connection,
                        payload,
                    )
                    for connection in matching_connections
                ],
                return_exceptions=True,
            )

        sent = 0
        failed = 0
        skipped = 0

        for result in results:
            if result == "sent":
                sent += 1

            elif result == "skipped":
                skipped += 1

            else:
                failed += 1

        logger.debug(
            "EMA WebSocket broadcast completed "
            "event=%s instrument=%s sent=%s "
            "failed=%s skipped=%s",
            payload.get("event"),
            instrument_key,
            sent,
            failed,
            skipped,
        )

        return {
            "sent": sent,
            "failed": failed,
            "skipped": skipped,
        }

    async def _send_to_connection(
        self,
        connection: WebSocketConnection,
        payload: dict[str, Any],
    ) -> str:
        """
        Send one event to one WebSocket connection.
        """

        try:
            await asyncio.wait_for(
                connection.websocket.send_json(payload),
                timeout=(config.EMA_WS_SEND_TIMEOUT_SECONDS),
            )

            return "sent"

        except (
            WebSocketDisconnect,
            ConnectionError,
            RuntimeError,
            asyncio.TimeoutError,
        ) as ex:
            logger.warning(
                "EMA WebSocket send failed " "client_id=%s error=%s",
                connection.client_id,
                ex,
            )

            await self.disconnect(connection)

            return "failed"

        except Exception:
            logger.exception(
                "Unexpected EMA WebSocket send error " "client_id=%s",
                connection.client_id,
            )

            await self.disconnect(connection)

            return "failed"

    # ========================================================
    # CONNECTION INFORMATION
    # ========================================================

    async def send_subscription_status(
        self,
        connection: WebSocketConnection,
    ) -> bool:
        """
        Send the current subscription state to a client.
        """

        try:
            await asyncio.wait_for(
                connection.websocket.send_json(self.subscription_status(connection)),
                timeout=(config.EMA_WS_SEND_TIMEOUT_SECONDS),
            )

            return True

        except Exception:
            logger.exception(
                "Failed to send subscription status " "client_id=%s",
                connection.client_id,
            )

            await self.disconnect(connection)

            return False

    def get_connection_count(self) -> int:
        """
        Return the number of active connections.
        """

        return len(self.connections)

    def get_status(self) -> dict[str, Any]:
        """
        Return WebSocket manager status.
        """

        return {
            "enabled": config.EMA_WEBSOCKET_ENABLED,
            "path": config.EMA_WEBSOCKET_PATH,
            "active_connections": len(self.connections),
            "max_connections": (config.EMA_WS_MAX_CONNECTIONS),
            "connections": [
                {
                    "client_id": connection.client_id,
                    "connected_at": connection.connected_at,
                    "subscriptions": sorted(connection.subscriptions),
                    "subscribed_to_all": (ALL_INSTRUMENTS in connection.subscriptions),
                }
                for connection in self.connections.values()
            ],
        }

    # ========================================================
    # LIFECYCLE
    # ========================================================

    async def close_all(self) -> None:
        """
        Close all active WebSocket connections.
        """

        connections = list(self.connections.values())

        for connection in connections:
            try:
                await connection.websocket.close()

            except Exception:
                logger.exception(
                    "Failed to close WebSocket " "client_id=%s",
                    connection.client_id,
                )

            finally:
                await self.disconnect(connection)

        logger.info("All EMA WebSocket connections closed")


websocket_manager = EmaWebSocketManager()
