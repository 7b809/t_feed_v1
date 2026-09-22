from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from core import config
from core.logger import get_logger
from models.ema_events import EmaEvent

logger = get_logger(__file__)

ALL_INSTRUMENTS = "*"


@dataclass(slots=True)
class WebSocketConnection:
    websocket: WebSocket
    subscriptions: set[str] = field(
        default_factory=set
    )
    connected_at: str | None = None
    client_id: str | None = None


class EmaWebSocketManager:
    def __init__(self) -> None:
        self.connections: dict[
            int,
            WebSocketConnection,
        ] = {}

        self._lock = asyncio.Lock()
        self._connection_counter = 0

    @staticmethod
    def _get_bool_setting(
        name: str,
        default: bool,
    ) -> bool:
        value = getattr(
            config,
            name,
            default,
        )

        if isinstance(value, bool):
            return value

        if isinstance(value, str):
            return (
                value.strip().lower()
                in {
                    "1",
                    "true",
                    "yes",
                    "on",
                    "enabled",
                }
            )

        return bool(value)

    @staticmethod
    def _get_int_setting(
        name: str,
        default: int,
    ) -> int:
        value = getattr(
            config,
            name,
            default,
        )

        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _get_float_setting(
        name: str,
        default: float,
    ) -> float:
        value = getattr(
            config,
            name,
            default,
        )

        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(
            config.MARKET_TIMEZONE
        ).isoformat()

    def is_enabled(self) -> bool:
        return self._get_bool_setting(
            "EMA_WEBSOCKET_ENABLED",
            True,
        )

    def _get_max_connections(self) -> int:
        value = self._get_int_setting(
            "EMA_WS_MAX_CONNECTIONS",
            1000,
        )

        return max(1, value)

    def _get_send_timeout(self) -> float:
        value = self._get_float_setting(
            "EMA_WS_SEND_TIMEOUT_SECONDS",
            5.0,
        )

        return max(0.1, value)

    def _broadcast_crossovers_only(
        self,
    ) -> bool:
        return self._get_bool_setting(
            "EMA_BROADCAST_CROSSOVERS_ONLY",
            False,
        )

    def _events_enabled(self) -> bool:
        return self._get_bool_setting(
            "EMA_EVENTS_ENABLED",
            True,
        )

    def _get_connection_id(
        self,
        connection: WebSocketConnection,
    ) -> int | None:
        for connection_id, current in (
            self.connections.items()
        ):
            if current is connection:
                return connection_id

        return None

    def _get_connection_by_websocket(
        self,
        websocket: WebSocket,
    ) -> WebSocketConnection | None:
        for connection in (
            self.connections.values()
        ):
            if (
                connection.websocket
                is websocket
            ):
                return connection

        return None

    def _resolve_connection(
        self,
        connection_or_websocket: (
            WebSocketConnection
            | WebSocket
        ),
    ) -> WebSocketConnection | None:
        if isinstance(
            connection_or_websocket,
            WebSocketConnection,
        ):
            return connection_or_websocket

        if isinstance(
            connection_or_websocket,
            WebSocket,
        ):
            return (
                self._get_connection_by_websocket(
                    connection_or_websocket
                )
            )

        return None

    @staticmethod
    def _normalize_instrument_keys(
        instrument_keys: (
            str
            | list[str]
            | set[str]
            | tuple[str, ...]
            | None
        ),
    ) -> set:
        if instrument_keys is None:
            return set()

        if isinstance(
            instrument_keys,
            str,
        ):
            values = instrument_keys.split(",")
        elif isinstance(
            instrument_keys,
            (list, set, tuple),
        ):
            values = instrument_keys
        else:
            return set()

        normalized: set[str] = set()

        for instrument_key in values:
            if not isinstance(
                instrument_key,
                str,
            ):
                continue

            value = instrument_key.strip()

            if not value:
                continue

            if value == ALL_INSTRUMENTS:
                return {ALL_INSTRUMENTS}

            normalized.add(value)

        return normalized

    @staticmethod
    def _extract_instrument_key(
        payload: dict[str, Any],
    ) -> str | None:
        value = payload.get(
            "instrument_key"
        )

        if value is None:
            instrument = payload.get(
                "instrument"
            )

            if isinstance(
                instrument,
                dict,
            ):
                value = instrument.get(
                    "instrument_key"
                )

        if value is None:
            return None

        normalized = str(value).strip()

        return normalized or None

    @staticmethod
    def _extract_cross_type(
        payload: dict[str, Any],
    ) -> str | None:
        event_content = payload.get(
            "event"
        )

        if isinstance(
            event_content,
            dict,
        ):
            value = event_content.get(
                "cross_type"
            )
        else:
            value = payload.get(
                "cross_type"
            )

        if value is None:
            return None

        normalized = str(
            value
        ).strip().lower()

        return normalized or None

    @staticmethod
    def _prepare_payload(
        event: EmaEvent | dict[str, Any],
    ) -> dict[str, Any]:
        if isinstance(event, EmaEvent):
            return (
                event.to_websocket_payload()
            )

        if isinstance(event, dict):
            return dict(event)

        raise TypeError(
            "event must be EmaEvent "
            "or dictionary"
        )

    async def connect(
        self,
        websocket: WebSocket,
        *,
        client_id: str | None = None,
    ) -> WebSocketConnection:
        if not self.is_enabled():
            raise RuntimeError(
                "EMA WebSocket is disabled"
            )

        async with self._lock:
            existing_connection = (
                self._get_connection_by_websocket(
                    websocket
                )
            )

            if existing_connection is not None:
                return existing_connection

            current_connections = len(
                self.connections
            )

            max_connections = (
                self._get_max_connections()
            )

            if (
                current_connections
                >= max_connections
            ):
                logger.warning(
                    "WebSocket connection rejected "
                    "reason=max_connections_reached "
                    "current=%s max=%s",
                    current_connections,
                    max_connections,
                )

                raise RuntimeError(
                    "Maximum WebSocket "
                    "connections reached"
                )

            if (
                websocket.client_state
                == WebSocketState.CONNECTING
            ):
                await websocket.accept()

            elif (
                websocket.client_state
                != WebSocketState.CONNECTED
            ):
                raise RuntimeError(
                    "WebSocket is not available "
                    "for connection"
                )

            self._connection_counter += 1

            connection_id = (
                self._connection_counter
            )

            connection = WebSocketConnection(
                websocket=websocket,
                client_id=(
                    client_id
                    or (
                        "ema-client-"
                        f"{connection_id}"
                    )
                ),
                connected_at=self._now_iso(),
            )

            self.connections[
                connection_id
            ] = connection

            active_connections = len(
                self.connections
            )

        logger.info(
            "EMA WebSocket connected "
            "connection_id=%s client_id=%s "
            "active_connections=%s",
            connection_id,
            connection.client_id,
            active_connections,
        )

        return connection

    async def register(
        self,
        websocket: WebSocket,
        *,
        client_id: str | None = None,
    ) -> WebSocketConnection:
        return await self.connect(
            websocket,
            client_id=client_id,
        )

    async def add_client(
        self,
        websocket: WebSocket,
        *,
        client_id: str | None = None,
    ) -> WebSocketConnection:
        return await self.connect(
            websocket,
            client_id=client_id,
        )

    async def disconnect(
        self,
        connection_or_websocket: (
            WebSocketConnection
            | WebSocket
        ),
    ) -> None:
        async with self._lock:
            connection = (
                self._resolve_connection(
                    connection_or_websocket
                )
            )

            if connection is None:
                return

            connection_id = (
                self._get_connection_id(
                    connection
                )
            )

            if connection_id is None:
                return

            self.connections.pop(
                connection_id,
                None,
            )

            active_connections = len(
                self.connections
            )

        logger.info(
            "EMA WebSocket disconnected "
            "connection_id=%s client_id=%s "
            "active_connections=%s",
            connection_id,
            connection.client_id,
            active_connections,
        )

    async def unregister(
        self,
        connection_or_websocket: (
            WebSocketConnection
            | WebSocket
        ),
    ) -> None:
        await self.disconnect(
            connection_or_websocket
        )

    async def remove_client(
        self,
        connection_or_websocket: (
            WebSocketConnection
            | WebSocket
        ),
    ) -> None:
        await self.disconnect(
            connection_or_websocket
        )

    async def subscribe(
        self,
        connection_or_websocket: (
            WebSocketConnection
            | WebSocket
        ),
        instrument_keys: (
            str
            | list[str]
            | set[str]
            | tuple[str, ...]
        ),
    ) -> dict[str, Any]:
        connection = (
            self._resolve_connection(
                connection_or_websocket
            )
        )

        if connection is None:
            raise RuntimeError(
                "WebSocket connection "
                "is not registered"
            )

        normalized = (
            self._normalize_instrument_keys(
                instrument_keys
            )
        )

        if not normalized:
            raise ValueError(
                "At least one valid instrument "
                "key is required"
            )

        async with self._lock:
            if (
                ALL_INSTRUMENTS
                in normalized
            ):
                connection.subscriptions = {
                    ALL_INSTRUMENTS
                }
            else:
                if (
                    ALL_INSTRUMENTS
                    in connection.subscriptions
                ):
                    connection.subscriptions.clear()

                connection.subscriptions.update(
                    normalized
                )

            subscriptions = sorted(
                connection.subscriptions
            )

        logger.info(
            "WebSocket subscription updated "
            "client_id=%s instruments=%s",
            connection.client_id,
            subscriptions,
        )

        return self.subscription_status(
            connection
        )

    async def update_subscription(
        self,
        connection_or_websocket: (
            WebSocketConnection
            | WebSocket
        ),
        instrument_keys: (
            str
            | list[str]
            | set[str]
            | tuple[str, ...]
        ),
    ) -> dict[str, Any]:
        connection = (
            self._resolve_connection(
                connection_or_websocket
            )
        )

        if connection is None:
            raise RuntimeError(
                "WebSocket connection "
                "is not registered"
            )

        normalized = (
            self._normalize_instrument_keys(
                instrument_keys
            )
        )

        async with self._lock:
            if not normalized:
                connection.subscriptions.clear()

            elif (
                ALL_INSTRUMENTS
                in normalized
            ):
                connection.subscriptions = {
                    ALL_INSTRUMENTS
                }

            else:
                connection.subscriptions = set(
                    normalized
                )

            subscriptions = sorted(
                connection.subscriptions
            )

        logger.info(
            "WebSocket subscriptions replaced "
            "client_id=%s instruments=%s",
            connection.client_id,
            subscriptions,
        )

        return self.subscription_status(
            connection
        )

    async def update_subscriptions(
        self,
        connection_or_websocket: (
            WebSocketConnection
            | WebSocket
        ),
        instrument_keys: (
            str
            | list[str]
            | set[str]
            | tuple[str, ...]
        ),
    ) -> dict[str, Any]:
        return await self.update_subscription(
            connection_or_websocket,
            instrument_keys,
        )

    async def subscribe_instruments(
        self,
        connection_or_websocket: (
            WebSocketConnection
            | WebSocket
        ),
        instrument_keys: (
            str
            | list[str]
            | set[str]
            | tuple[str, ...]
        ),
    ) -> dict[str, Any]:
        return await self.update_subscription(
            connection_or_websocket,
            instrument_keys,
        )

    async def set_subscription(
        self,
        connection_or_websocket: (
            WebSocketConnection
            | WebSocket
        ),
        instrument_keys: (
            str
            | list[str]
            | set[str]
            | tuple[str, ...]
        ),
    ) -> dict[str, Any]:
        return await self.update_subscription(
            connection_or_websocket,
            instrument_keys,
        )

    async def subscribe_all(
        self,
        connection_or_websocket: (
            WebSocketConnection
            | WebSocket
        ),
    ) -> dict[str, Any]:
        return await self.update_subscription(
            connection_or_websocket,
            [ALL_INSTRUMENTS],
        )

    async def unsubscribe(
        self,
        connection_or_websocket: (
            WebSocketConnection
            | WebSocket
        ),
        instrument_keys: (
            str
            | list[str]
            | set[str]
            | tuple[str, ...]
        ),
    ) -> dict[str, Any]:
        connection = (
            self._resolve_connection(
                connection_or_websocket
            )
        )

        if connection is None:
            raise RuntimeError(
                "WebSocket connection "
                "is not registered"
            )

        normalized = (
            self._normalize_instrument_keys(
                instrument_keys
            )
        )

        async with self._lock:
            if (
                ALL_INSTRUMENTS
                in normalized
            ):
                connection.subscriptions.clear()
            else:
                connection.subscriptions.difference_update(
                    normalized
                )

            subscriptions = sorted(
                connection.subscriptions
            )

        logger.info(
            "WebSocket subscriptions removed "
            "client_id=%s removed=%s "
            "remaining=%s",
            connection.client_id,
            sorted(normalized),
            subscriptions,
        )

        return self.subscription_status(
            connection
        )

    async def unsubscribe_instruments(
        self,
        connection_or_websocket: (
            WebSocketConnection
            | WebSocket
        ),
        instrument_keys: (
            str
            | list[str]
            | set[str]
            | tuple[str, ...]
        ),
    ) -> dict[str, Any]:
        return await self.unsubscribe(
            connection_or_websocket,
            instrument_keys,
        )

    async def remove_subscription(
        self,
        connection_or_websocket: (
            WebSocketConnection
            | WebSocket
        ),
        instrument_keys: (
            str
            | list[str]
            | set[str]
            | tuple[str, ...]
        ),
    ) -> dict[str, Any]:
        return await self.unsubscribe(
            connection_or_websocket,
            instrument_keys,
        )

    async def unsubscribe_all(
        self,
        connection_or_websocket: (
            WebSocketConnection
            | WebSocket
        ),
    ) -> dict[str, Any]:
        connection = (
            self._resolve_connection(
                connection_or_websocket
            )
        )

        if connection is None:
            raise RuntimeError(
                "WebSocket connection "
                "is not registered"
            )

        async with self._lock:
            connection.subscriptions.clear()

        logger.info(
            "WebSocket unsubscribed from all "
            "instruments client_id=%s",
            connection.client_id,
        )

        return self.subscription_status(
            connection
        )

    def subscription_status(
        self,
        connection_or_websocket: (
            WebSocketConnection
            | WebSocket
        ),
    ) -> dict[str, Any]:
        connection = (
            self._resolve_connection(
                connection_or_websocket
            )
        )

        if connection is None:
            return {
                "event": (
                    "ema.subscription_status"
                ),
                "client_id": None,
                "subscriptions": [],
                "subscribed_to_all": False,
                "subscription_count": 0,
                "timestamp": self._now_iso(),
            }

        subscriptions = sorted(
            connection.subscriptions
        )

        subscribed_to_all = (
            ALL_INSTRUMENTS
            in connection.subscriptions
        )

        return {
            "event": (
                "ema.subscription_status"
            ),
            "client_id": (
                connection.client_id
            ),
            "subscriptions": (
                subscriptions
            ),
            "subscribed_to_all": (
                subscribed_to_all
            ),
            "subscription_count": (
                0
                if subscribed_to_all
                else len(subscriptions)
            ),
            "timestamp": self._now_iso(),
        }

    @staticmethod
    def is_subscribed(
        connection: WebSocketConnection,
        instrument_key: str | None,
    ) -> bool:
        if (
            ALL_INSTRUMENTS
            in connection.subscriptions
        ):
            return True

        if not instrument_key:
            return False

        return (
            instrument_key
            in connection.subscriptions
        )

    async def get_matching_connections(
        self,
        instrument_key: str,
    ) -> list:
        async with self._lock:
            connections = list(
                self.connections.values()
            )

            return [
                connection
                for connection in connections
                if self.is_subscribed(
                    connection,
                    instrument_key,
                )
            ]

    async def broadcast_instrument(
        self,
        instrument_key: str,
        event: EmaEvent | dict[str, Any],
    ) -> dict[str, Any]:
        normalized_key = str(
            instrument_key or ""
        ).strip()

        if not normalized_key:
            raise ValueError(
                "instrument_key is required"
            )

        payload = self._prepare_payload(
            event
        )

        payload[
            "instrument_key"
        ] = normalized_key

        return await self._broadcast_payload(
            normalized_key,
            payload,
        )

    async def broadcast_to_instrument(
        self,
        instrument_key: str,
        event: EmaEvent | dict[str, Any],
    ) -> dict[str, Any]:
        return await self.broadcast_instrument(
            instrument_key,
            event,
        )

    async def publish(
        self,
        instrument_key: str,
        event: EmaEvent | dict[str, Any],
    ) -> dict[str, Any]:
        return await self.broadcast_instrument(
            instrument_key,
            event,
        )

    async def broadcast_event(
        self,
        event: EmaEvent | dict[str, Any],
    ) -> dict[str, Any]:
        payload = self._prepare_payload(
            event
        )

        instrument_key = (
            self._extract_instrument_key(
                payload
            )
        )

        if not instrument_key:
            return {
                "instrument_key": None,
                "matching_connections": 0,
                "sent": 0,
                "failed": 0,
                "skipped": 1,
                "reason": (
                    "instrument_key_not_available"
                ),
            }

        return await self._broadcast_payload(
            instrument_key,
            payload,
        )

    async def broadcast(
        self,
        event: EmaEvent | dict[str, Any],
    ) -> dict[str, Any]:
        return await self.broadcast_event(
            event
        )

    async def _broadcast_payload(
        self,
        instrument_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.is_enabled():
            return {
                "instrument_key": (
                    instrument_key
                ),
                "matching_connections": 0,
                "sent": 0,
                "failed": 0,
                "skipped": 1,
                "reason": (
                    "websocket_disabled"
                ),
            }

        if not self._events_enabled():
            return {
                "instrument_key": (
                    instrument_key
                ),
                "matching_connections": 0,
                "sent": 0,
                "failed": 0,
                "skipped": 1,
                "reason": (
                    "ema_events_disabled"
                ),
            }

        if (
            self._broadcast_crossovers_only()
            and not self._extract_cross_type(
                payload
            )
        ):
            return {
                "instrument_key": (
                    instrument_key
                ),
                "matching_connections": 0,
                "sent": 0,
                "failed": 0,
                "skipped": 1,
                "reason": (
                    "non_crossover_event_filtered"
                ),
            }

        matching_connections = (
            await self.get_matching_connections(
                instrument_key
            )
        )

        if not matching_connections:
            return {
                "instrument_key": (
                    instrument_key
                ),
                "matching_connections": 0,
                "sent": 0,
                "failed": 0,
                "skipped": 0,
                "reason": (
                    "no_matching_subscribers"
                ),
            }

        results = await asyncio.gather(
            *[
                self._send_to_connection(
                    connection,
                    payload,
                )
                for connection
                in matching_connections
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

        event_type = payload.get(
            "event_type"
        )

        if event_type is None:
            event_value = payload.get(
                "event"
            )

            if isinstance(
                event_value,
                str,
            ):
                event_type = event_value

        logger.debug(
            "EMA WebSocket broadcast completed "
            "event_type=%s instrument=%s "
            "matching=%s sent=%s failed=%s "
            "skipped=%s",
            event_type,
            instrument_key,
            len(matching_connections),
            sent,
            failed,
            skipped,
        )

        return {
            "instrument_key": (
                instrument_key
            ),
            "matching_connections": len(
                matching_connections
            ),
            "sent": sent,
            "failed": failed,
            "skipped": skipped,
        }

    async def _send_to_connection(
        self,
        connection: WebSocketConnection,
        payload: dict[str, Any],
    ) -> str:
        websocket = connection.websocket

        if (
            websocket.client_state
            != WebSocketState.CONNECTED
        ):
            await self.disconnect(
                connection
            )
            return "skipped"

        try:
            await asyncio.wait_for(
                websocket.send_json(
                    payload
                ),
                timeout=(
                    self._get_send_timeout()
                ),
            )

            return "sent"

        except (
            WebSocketDisconnect,
            ConnectionError,
            RuntimeError,
            asyncio.TimeoutError,
        ) as error:
            logger.warning(
                "EMA WebSocket send failed "
                "client_id=%s error=%s",
                connection.client_id,
                error,
            )

            await self.disconnect(
                connection
            )

            return "failed"

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Unexpected EMA WebSocket "
                "send error client_id=%s",
                connection.client_id,
            )

            await self.disconnect(
                connection
            )

            return "failed"

    async def send_subscription_status(
        self,
        connection_or_websocket: (
            WebSocketConnection
            | WebSocket
        ),
    ) -> bool:
        connection = (
            self._resolve_connection(
                connection_or_websocket
            )
        )

        if connection is None:
            return False

        try:
            await asyncio.wait_for(
                connection.websocket.send_json(
                    self.subscription_status(
                        connection
                    )
                ),
                timeout=(
                    self._get_send_timeout()
                ),
            )

            return True

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Failed to send subscription "
                "status client_id=%s",
                connection.client_id,
            )

            await self.disconnect(
                connection
            )

            return False

    async def send_to_client(
        self,
        connection_or_websocket: (
            WebSocketConnection
            | WebSocket
        ),
        payload: dict[str, Any],
    ) -> bool:
        connection = (
            self._resolve_connection(
                connection_or_websocket
            )
        )

        if connection is None:
            return False

        result = await self._send_to_connection(
            connection,
            payload,
        )

        return result == "sent"

    def get_connection_count(self) -> int:
        return len(self.connections)

    def get_status(self) -> dict[str, Any]:
        connections = list(
            self.connections.values()
        )

        subscribed_to_all_count = sum(
            ALL_INSTRUMENTS
            in connection.subscriptions
            for connection in connections
        )

        subscribed_connection_count = sum(
            bool(connection.subscriptions)
            for connection in connections
        )

        return {
            "enabled": self.is_enabled(),
            "events_enabled": (
                self._events_enabled()
            ),
            "broadcast_crossovers_only": (
                self._broadcast_crossovers_only()
            ),
            "path": getattr(
                config,
                "EMA_WEBSOCKET_PATH",
                "/ws/ema",
            ),
            "active_connections": len(
                connections
            ),
            "subscribed_connections": (
                subscribed_connection_count
            ),
            "subscribed_to_all_connections": (
                subscribed_to_all_count
            ),
            "max_connections": (
                self._get_max_connections()
            ),
            "send_timeout_seconds": (
                self._get_send_timeout()
            ),
            "connections": [
                {
                    "client_id": (
                        connection.client_id
                    ),
                    "connected_at": (
                        connection.connected_at
                    ),
                    "subscriptions": sorted(
                        connection.subscriptions
                    ),
                    "subscribed_to_all": (
                        ALL_INSTRUMENTS
                        in connection.subscriptions
                    ),
                }
                for connection in connections
            ],
        }

    async def close_all(self) -> None:
        async with self._lock:
            connections = list(
                self.connections.values()
            )

        close_results = await asyncio.gather(
            *[
                self._close_connection(
                    connection
                )
                for connection in connections
            ],
            return_exceptions=True,
        )

        failure_count = sum(
            isinstance(
                result,
                Exception,
            )
            for result in close_results
        )

        logger.info(
            "All EMA WebSocket connections "
            "closed total=%s failures=%s",
            len(connections),
            failure_count,
        )

    async def _close_connection(
        self,
        connection: WebSocketConnection,
    ) -> None:
        try:
            if (
                connection.websocket.client_state
                == WebSocketState.CONNECTED
            ):
                await connection.websocket.close(
                    code=1001
                )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Failed to close WebSocket "
                "client_id=%s",
                connection.client_id,
            )

        finally:
            await self.disconnect(
                connection
            )


websocket_manager = EmaWebSocketManager()


__all__ = [
    "ALL_INSTRUMENTS",
    "WebSocketConnection",
    "EmaWebSocketManager",
    "websocket_manager",
]