import asyncio
from collections import defaultdict
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.logger import get_logger


logger = get_logger(__file__)


class WebSocketHub:
    def __init__(self) -> None:
        self._clients: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

        logger.info("WebSocketHub initialized")

    @staticmethod
    def _client_address(
        websocket: WebSocket,
    ) -> str:
        if websocket.client is None:
            return "unknown"

        return (
            f"{websocket.client.host}:"
            f"{websocket.client.port}"
        )

    @staticmethod
    def _is_connected(
        websocket: WebSocket,
    ) -> bool:
        return (
            websocket.client_state
            == WebSocketState.CONNECTED
            and websocket.application_state
            == WebSocketState.CONNECTED
        )

    @staticmethod
    def _is_expected_disconnect(
        exception: BaseException,
    ) -> bool:
        if isinstance(
            exception,
            WebSocketDisconnect,
        ):
            return True

        exception_name = type(
            exception
        ).__name__.lower()

        exception_message = str(
            exception
        ).lower()

        expected_exception_names = {
            "clientdisconnected",
            "connectionclosed",
            "connectionclosedok",
            "connectionclosederror",
        }

        expected_message_parts = (
            "connection closed",
            "connection is closed",
            "websocket.disconnect",
            "websocket.close",
            "close message has been sent",
            "not connected",
            "received 1000",
            "received 1001",
            "received 1005",
            "received 1006",
        )

        if (
            exception_name
            in expected_exception_names
        ):
            return True

        return any(
            text in exception_message
            for text in expected_message_parts
        )

    async def connect(
        self,
        instrument_key: str,
        websocket: WebSocket,
    ) -> None:
        instrument_key = (
            instrument_key.strip()
            if instrument_key
            else ""
        )

        client_address = (
            self._client_address(
                websocket
            )
        )

        logger.info(
            "WebSocket connection requested | "
            "instrument_key=%s | client=%s",
            instrument_key,
            client_address,
        )

        if not instrument_key:
            logger.warning(
                "WebSocket connection rejected due to "
                "empty instrument key | client=%s",
                client_address,
            )

            await self._safe_close(
                websocket=websocket,
                code=1008,
                reason="Instrument key is required",
            )

            return

        accepted = False

        try:
            await websocket.accept()
            accepted = True

            async with self._lock:
                self._clients[
                    instrument_key
                ].add(
                    websocket
                )

                instrument_client_count = len(
                    self._clients[
                        instrument_key
                    ]
                )

            logger.info(
                "WebSocket connected | "
                "instrument_key=%s | client=%s | "
                "instrument_clients=%s",
                instrument_key,
                client_address,
                instrument_client_count,
            )

        except asyncio.CancelledError:
            logger.debug(
                "WebSocket connection cancelled | "
                "instrument_key=%s | client=%s",
                instrument_key,
                client_address,
            )

            raise

        except Exception as exc:
            if self._is_expected_disconnect(
                exc
            ):
                logger.debug(
                    "WebSocket disconnected while connecting | "
                    "instrument_key=%s | client=%s",
                    instrument_key,
                    client_address,
                )
            else:
                logger.exception(
                    "Failed to connect WebSocket | "
                    "instrument_key=%s | client=%s",
                    instrument_key,
                    client_address,
                )

            if accepted:
                await self.disconnect(
                    instrument_key=instrument_key,
                    websocket=websocket,
                )
            else:
                await self._safe_close(
                    websocket=websocket,
                    code=1011,
                    reason="WebSocket connection failed",
                )

            if not self._is_expected_disconnect(
                exc
            ):
                raise

    async def disconnect(
        self,
        instrument_key: str,
        websocket: WebSocket,
    ) -> None:
        client_address = (
            self._client_address(
                websocket
            )
        )

        removed = False
        remaining_clients = 0

        try:
            async with self._lock:
                clients = self._clients.get(
                    instrument_key
                )

                if (
                    clients is not None
                    and websocket in clients
                ):
                    clients.discard(
                        websocket
                    )

                    removed = True
                    remaining_clients = len(
                        clients
                    )

                    if not clients:
                        self._clients.pop(
                            instrument_key,
                            None,
                        )

                elif clients is not None:
                    remaining_clients = len(
                        clients
                    )

            if removed:
                logger.info(
                    "WebSocket disconnected | "
                    "instrument_key=%s | client=%s | "
                    "remaining_clients=%s",
                    instrument_key,
                    client_address,
                    remaining_clients,
                )
            else:
                logger.debug(
                    "WebSocket was not registered during "
                    "disconnect | instrument_key=%s | "
                    "client=%s",
                    instrument_key,
                    client_address,
                )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Failed to remove WebSocket client | "
                "instrument_key=%s | client=%s",
                instrument_key,
                client_address,
            )

    async def broadcast(
        self,
        instrument_key: str,
        payload: Any,
    ) -> None:
        try:
            async with self._lock:
                clients = list(
                    self._clients.get(
                        instrument_key,
                        set(),
                    )
                )

            if not clients:
                return

            send_tasks = [
                self._send_to_client(
                    instrument_key=instrument_key,
                    websocket=client,
                    payload=payload,
                )
                for client in clients
            ]

            results = await asyncio.gather(
                *send_tasks,
                return_exceptions=True,
            )

            dead_clients: list[
                WebSocket
            ] = []

            successful_sends = 0

            for client, result in zip(
                clients,
                results,
            ):
                if result is True:
                    successful_sends += 1
                    continue

                dead_clients.append(
                    client
                )

                if isinstance(
                    result,
                    BaseException,
                ):
                    if self._is_expected_disconnect(
                        result
                    ):
                        logger.debug(
                            "WebSocket client disconnected "
                            "during broadcast | "
                            "instrument_key=%s | client=%s",
                            instrument_key,
                            self._client_address(
                                client
                            ),
                        )
                    else:
                        logger.error(
                            "Unexpected WebSocket send failure | "
                            "instrument_key=%s | client=%s | "
                            "error_type=%s | error=%s",
                            instrument_key,
                            self._client_address(
                                client
                            ),
                            type(result).__name__,
                            result,
                            exc_info=(
                                type(result),
                                result,
                                result.__traceback__,
                            ),
                        )

            if dead_clients:
                await self._remove_dead_clients(
                    instrument_key=instrument_key,
                    dead_clients=dead_clients,
                )

            logger.debug(
                "WebSocket broadcast completed | "
                "instrument_key=%s | successful=%s | "
                "removed=%s",
                instrument_key,
                successful_sends,
                len(dead_clients),
            )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Unexpected WebSocket broadcast failure | "
                "instrument_key=%s",
                instrument_key,
            )

    async def _send_to_client(
        self,
        instrument_key: str,
        websocket: WebSocket,
        payload: Any,
    ) -> bool:
        client_address = (
            self._client_address(
                websocket
            )
        )

        if not self._is_connected(
            websocket
        ):
            logger.debug(
                "Skipping disconnected WebSocket | "
                "instrument_key=%s | client=%s | "
                "client_state=%s | application_state=%s",
                instrument_key,
                client_address,
                websocket.client_state,
                websocket.application_state,
            )

            return False

        try:
            await websocket.send_json(
                payload
            )

            return True

        except asyncio.CancelledError:
            raise

        except WebSocketDisconnect:
            logger.debug(
                "WebSocket disconnected while sending | "
                "instrument_key=%s | client=%s",
                instrument_key,
                client_address,
            )

            return False

        except RuntimeError as exc:
            if self._is_expected_disconnect(
                exc
            ):
                logger.debug(
                    "WebSocket already closed while sending | "
                    "instrument_key=%s | client=%s",
                    instrument_key,
                    client_address,
                )

                return False

            logger.exception(
                "Unexpected WebSocket runtime error | "
                "instrument_key=%s | client=%s",
                instrument_key,
                client_address,
            )

            return False

        except Exception as exc:
            if self._is_expected_disconnect(
                exc
            ):
                logger.debug(
                    "WebSocket connection closed while sending | "
                    "instrument_key=%s | client=%s | "
                    "error_type=%s",
                    instrument_key,
                    client_address,
                    type(exc).__name__,
                )

                return False

            logger.exception(
                "Unexpected WebSocket send error | "
                "instrument_key=%s | client=%s",
                instrument_key,
                client_address,
            )

            return False

    async def _remove_dead_clients(
        self,
        instrument_key: str,
        dead_clients: list[WebSocket],
    ) -> None:
        if not dead_clients:
            return

        unique_dead_clients = set(
            dead_clients
        )

        removed_count = 0

        try:
            async with self._lock:
                registered_clients = (
                    self._clients.get(
                        instrument_key
                    )
                )

                if registered_clients is None:
                    return

                for client in (
                    unique_dead_clients
                ):
                    if (
                        client
                        in registered_clients
                    ):
                        registered_clients.discard(
                            client
                        )

                        removed_count += 1

                if not registered_clients:
                    self._clients.pop(
                        instrument_key,
                        None,
                    )

            if removed_count:
                logger.info(
                    "Disconnected WebSocket clients removed | "
                    "instrument_key=%s | count=%s",
                    instrument_key,
                    removed_count,
                )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Failed to remove disconnected "
                "WebSocket clients | instrument_key=%s",
                instrument_key,
            )

    async def _safe_close(
        self,
        websocket: WebSocket,
        code: int = 1000,
        reason: str = "",
    ) -> None:
        client_address = (
            self._client_address(
                websocket
            )
        )

        try:
            if (
                websocket.client_state
                == WebSocketState.DISCONNECTED
                or websocket.application_state
                == WebSocketState.DISCONNECTED
            ):
                return

            await websocket.close(
                code=code,
                reason=reason,
            )

            logger.debug(
                "WebSocket closed | "
                "client=%s | code=%s",
                client_address,
                code,
            )

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            if self._is_expected_disconnect(
                exc
            ):
                logger.debug(
                    "WebSocket already disconnected | "
                    "client=%s",
                    client_address,
                )

                return

            logger.debug(
                "Unable to close WebSocket | "
                "client=%s",
                client_address,
                exc_info=True,
            )

    def client_counts(
        self,
    ) -> dict[str, int]:
        try:
            return {
                instrument_key: len(
                    clients
                )
                for instrument_key, clients
                in self._clients.items()
            }

        except Exception:
            logger.exception(
                "Failed to calculate WebSocket "
                "client counts"
            )

            return {}

    def total_client_count(
        self,
    ) -> int:
        try:
            return sum(
                len(clients)
                for clients
                in self._clients.values()
            )

        except Exception:
            logger.exception(
                "Failed to calculate total "
                "WebSocket client count"
            )

            return 0