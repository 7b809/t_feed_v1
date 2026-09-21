# api/websocket_routes.py

from __future__ import annotations

import asyncio
import inspect
import json
from contextlib import suppress
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


# ---------------------------------------------------------------------------
# Debug Configuration
# ---------------------------------------------------------------------------

DEBUG_MODE = False


def _debug(message: str, *args: Any) -> None:
    """
    Print debug information only when DEBUG_MODE is enabled.
    """
    if not DEBUG_MODE:
        return

    if args:
        message = message.format(*args)

    print(f"[EMA WS DEBUG] {message}")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WEBSOCKET_PREFIX = getattr(
    config,
    "EMA_WEBSOCKET_PREFIX",
    "/ws",
)

WEBSOCKET_PATH = getattr(
    config,
    "EMA_WEBSOCKET_PATH",
    "/ema",
)

FULL_WEBSOCKET_PATH = f"{WEBSOCKET_PREFIX.rstrip('/')}" f"/{WEBSOCKET_PATH.strip('/')}"


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    """
    Convert Python objects into JSON-compatible values.
    """

    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]

    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump())

    if hasattr(value, "dict"):
        return _json_safe(value.dict())

    if hasattr(value, "to_dict"):
        return _json_safe(value.to_dict())

    if hasattr(value, "__dict__"):
        return _json_safe(vars(value))

    return str(value)


def _parse_json_message(message: Any) -> dict[str, Any]:
    """
    Parse a WebSocket message into a dictionary.
    """

    if isinstance(message, dict):
        return message

    if not isinstance(message, str):
        return {}

    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        _debug("Failed to decode JSON message: {}", message)
        return {}

    return parsed if isinstance(parsed, dict) else {}


def _normalise_instrument_keys(
    value: Any,
) -> list[str]:
    """
    Normalize instrument keys received from a client.

    Accepted examples:
        "NSE_INDEX|Nifty 50"
        ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"]
        {"instrument_key": "..."}
    """

    if value is None:
        return []

    if isinstance(value, str):
        value = [value]

    if not isinstance(value, (list, tuple, set)):
        return []

    result: list[str] = []

    for item in value:
        if not isinstance(item, str):
            continue

        instrument_key = item.strip()

        if instrument_key and instrument_key not in result:
            result.append(instrument_key)

    return result


def _get_runtime(websocket: WebSocket) -> Any:
    """
    Resolve EMA runtime from the FastAPI application state.
    """

    app = websocket.app
    state = getattr(app, "state", None)

    if state is None:
        _debug("App state is unavailable on websocket")
        return None

    runtime = getattr(
        state,
        "ema_runtime",
        None,
    )

    _debug(
        "Resolved runtime: {}",
        type(runtime).__name__ if runtime is not None else None,
    )

    return runtime


def _get_query_service(websocket: WebSocket) -> Any:
    """
    Resolve the EMA query service from FastAPI application state.
    """

    app = websocket.app
    state = getattr(app, "state", None)

    if state is None:
        return None

    query_service = getattr(
        state,
        "ema_query_service",
        None,
    )

    _debug(
        "Resolved query service: {}",
        type(query_service).__name__ if query_service is not None else None,
    )

    return query_service


def _get_websocket_manager(websocket: WebSocket) -> Any:
    """
    Resolve the WebSocket manager from FastAPI application state.

    Supported state names:
        websocket_manager
        ema_websocket_manager
    """

    app = websocket.app
    state = getattr(app, "state", None)

    if state is None:
        return None

    manager = getattr(
        state,
        "ema_websocket_manager",
        None,
    )

    if manager is not None:
        _debug("Resolved ema_websocket_manager")
        return manager

    manager = getattr(
        state,
        "websocket_manager",
        None,
    )

    _debug(
        "Resolved websocket_manager: {}",
        type(manager).__name__ if manager is not None else None,
    )

    return manager


def _get_runtime_contracts(
    runtime: Any,
) -> list[dict[str, Any]]:
    """
    Return currently selected runtime contracts.
    """

    if runtime is None:
        return []

    contracts = getattr(
        runtime,
        "contracts",
        [],
    )

    if not isinstance(contracts, (list, tuple)):
        return []

    return [contract for contract in contracts if isinstance(contract, dict)]


def _get_runtime_states(
    runtime: Any,
) -> dict[str, Any]:
    """
    Return current EMA states.
    """

    if runtime is None:
        return {}

    states = getattr(
        runtime,
        "states",
        {},
    )

    return states if isinstance(states, dict) else {}


def _get_contract_by_instrument_key(
    runtime: Any,
    instrument_key: str,
) -> dict[str, Any] | None:
    """
    Find a contract by instrument key.
    """

    for contract in _get_runtime_contracts(runtime):
        if contract.get("instrument_key") == instrument_key:
            return contract

    return None


def _get_selected_instrument_keys(
    runtime: Any,
) -> list[str]:
    """
    Return all instrument keys currently selected in runtime.
    """

    result: list[str] = []

    for contract in _get_runtime_contracts(runtime):
        instrument_key = contract.get("instrument_key")

        if (
            isinstance(instrument_key, str)
            and instrument_key.strip()
            and instrument_key not in result
        ):
            result.append(instrument_key)

    return result


async def _call_method(
    target: Any,
    method_name: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """
    Call a synchronous or asynchronous method safely.
    """

    if target is None:
        return None

    method = getattr(
        target,
        method_name,
        None,
    )

    if not callable(method):
        return None

    result = method(
        *args,
        **kwargs,
    )

    if inspect.isawaitable(result):
        return await result

    return result


async def _call_first_available(
    target: Any,
    method_names: list[str],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """
    Call the first available method from a list.
    """

    if target is None:
        return None

    for method_name in method_names:
        method = getattr(
            target,
            method_name,
            None,
        )

        if not callable(method):
            continue

        _debug(
            "Dispatching manager method '{}'",
            method_name,
        )

        result = method(
            *args,
            **kwargs,
        )

        if inspect.isawaitable(result):
            result = await result

        return result

    _debug(
        "No matching manager method found among {}",
        method_names,
    )

    return None


async def _send_json(
    websocket: WebSocket,
    payload: dict[str, Any],
) -> bool:
    """
    Send a JSON payload to the connected client.
    """

    try:
        await websocket.send_json(_json_safe(payload))

        _debug("Sent payload: {}", payload)

        return True

    except Exception:
        logger.exception("WebSocket message send failed")
        return False


def _success_response(
    message: str,
    **data: Any,
) -> dict[str, Any]:
    """
    Build a standard successful response.
    """

    return {
        "status": "success",
        "message": message,
        "data": data,
    }


def _error_response(
    message: str,
    code: str = "WEBSOCKET_ERROR",
    **data: Any,
) -> dict[str, Any]:
    """
    Build a standard error response.
    """

    return {
        "status": "error",
        "code": code,
        "message": message,
        "data": data,
    }


# ---------------------------------------------------------------------------
# Initial state helpers
# ---------------------------------------------------------------------------


def _build_initial_state_payload(
    runtime: Any,
    instrument_keys: list[str],
) -> dict[str, Any]:
    """
    Build the initial EMA state snapshot for subscribed instruments.
    """

    states = _get_runtime_states(runtime)
    instruments: list[dict[str, Any]] = []

    for instrument_key in instrument_keys:
        contract = _get_contract_by_instrument_key(
            runtime,
            instrument_key,
        )

        state = states.get(instrument_key)

        instruments.append(
            {
                "instrument_key": instrument_key,
                "contract": contract,
                "state": state,
            }
        )

    return {
        "event": "initial_state",
        "timestamp": datetime.now().isoformat(),
        "instruments": instruments,
    }


# ---------------------------------------------------------------------------
# WebSocket manager integration
# ---------------------------------------------------------------------------


async def _register_client(
    manager: Any,
    websocket: WebSocket,
    instrument_keys: list[str],
) -> Any:
    """
    Register a client with the WebSocket manager.

    Different manager method names are supported for compatibility.
    """

    return await _call_first_available(
        manager,
        [
            "connect",
            "register",
            "add_client",
            "subscribe",
        ],
        websocket,
        instrument_keys,
    )


async def _unregister_client(
    manager: Any,
    websocket: WebSocket,
) -> Any:
    """
    Remove a client from the WebSocket manager.
    """

    return await _call_first_available(
        manager,
        [
            "disconnect",
            "unregister",
            "remove_client",
        ],
        websocket,
    )


async def _update_subscription(
    manager: Any,
    websocket: WebSocket,
    instrument_keys: list[str],
) -> Any:
    """
    Update subscriptions for an existing client.
    """

    return await _call_first_available(
        manager,
        [
            "update_subscription",
            "update_subscriptions",
            "subscribe_instruments",
            "set_subscription",
        ],
        websocket,
        instrument_keys,
    )


async def _remove_subscription(
    manager: Any,
    websocket: WebSocket,
    instrument_keys: list[str],
) -> Any:
    """
    Remove selected instrument subscriptions.
    """

    return await _call_first_available(
        manager,
        [
            "unsubscribe_instruments",
            "remove_subscription",
            "unsubscribe",
        ],
        websocket,
        instrument_keys,
    )


# ---------------------------------------------------------------------------
# Client message handling
# ---------------------------------------------------------------------------


async def _handle_subscribe_message(
    websocket: WebSocket,
    manager: Any,
    runtime: Any,
    message: dict[str, Any],
    current_subscriptions: set[str],
) -> set[str]:
    """
    Handle subscribe messages.

    Examples:

    {
        "action": "subscribe",
        "instrument_key": "NSE_INDEX|Nifty 50"
    }

    {
        "action": "subscribe",
        "instrument_keys": [
            "NSE_INDEX|Nifty 50",
            "NSE_INDEX|Nifty Bank"
        ]
    }

    {
        "action": "subscribe",
        "all": true
    }
    """

    subscribe_all = bool(message.get("all") or message.get("subscribe_all"))

    if subscribe_all:
        requested_keys = _get_selected_instrument_keys(runtime)
    else:
        requested_keys = _normalise_instrument_keys(
            message.get(
                "instrument_keys",
                message.get("instrument_key"),
            )
        )

    _debug(
        "Subscribe request: all={} keys={}",
        subscribe_all,
        requested_keys,
    )

    if not requested_keys:
        await _send_json(
            websocket,
            _error_response(
                "No valid instrument keys provided",
                code="INVALID_SUBSCRIPTION",
            ),
        )
        return current_subscriptions

    available_keys = set(_get_selected_instrument_keys(runtime))

    unknown_keys = [key for key in requested_keys if key not in available_keys]

    if unknown_keys:
        await _send_json(
            websocket,
            _error_response(
                "One or more instruments are not selected",
                code="UNKNOWN_INSTRUMENT",
                unknown_instruments=unknown_keys,
                available_instruments=sorted(available_keys),
            ),
        )

        requested_keys = [key for key in requested_keys if key in available_keys]

    if not requested_keys:
        return current_subscriptions

    updated_subscriptions = current_subscriptions | set(requested_keys)

    await _update_subscription(
        manager,
        websocket,
        sorted(updated_subscriptions),
    )

    await _send_json(
        websocket,
        _success_response(
            "Subscription updated",
            subscribed_instruments=sorted(updated_subscriptions),
        ),
    )

    await _send_json(
        websocket,
        _build_initial_state_payload(
            runtime,
            sorted(updated_subscriptions),
        ),
    )

    logger.info(
        "WebSocket subscription updated " "instruments=%s",
        sorted(updated_subscriptions),
    )

    return updated_subscriptions


async def _handle_unsubscribe_message(
    websocket: WebSocket,
    manager: Any,
    message: dict[str, Any],
    current_subscriptions: set[str],
) -> set[str]:
    """
    Handle unsubscribe messages.
    """

    unsubscribe_all = bool(message.get("all") or message.get("unsubscribe_all"))

    if unsubscribe_all:
        requested_keys = list(current_subscriptions)
    else:
        requested_keys = _normalise_instrument_keys(
            message.get(
                "instrument_keys",
                message.get("instrument_key"),
            )
        )

    _debug(
        "Unsubscribe request: all={} keys={}",
        unsubscribe_all,
        requested_keys,
    )

    if not requested_keys:
        await _send_json(
            websocket,
            _error_response(
                "No valid instrument keys provided",
                code="INVALID_UNSUBSCRIPTION",
            ),
        )
        return current_subscriptions

    await _remove_subscription(
        manager,
        websocket,
        requested_keys,
    )

    updated_subscriptions = current_subscriptions - set(requested_keys)

    await _send_json(
        websocket,
        _success_response(
            "Subscription removed",
            unsubscribed_instruments=requested_keys,
            subscribed_instruments=sorted(updated_subscriptions),
        ),
    )

    logger.info(
        "WebSocket subscription removed " "instruments=%s",
        requested_keys,
    )

    return updated_subscriptions


async def _handle_snapshot_message(
    websocket: WebSocket,
    runtime: Any,
    current_subscriptions: set[str],
) -> None:
    """
    Send the current EMA state snapshot.
    """

    _debug(
        "Snapshot requested for {} instruments",
        len(current_subscriptions),
    )

    await _send_json(
        websocket,
        _build_initial_state_payload(
            runtime,
            sorted(current_subscriptions),
        ),
    )


async def _handle_ping_message(
    websocket: WebSocket,
) -> None:
    """
    Handle ping messages from clients.
    """

    _debug("Ping received")

    await _send_json(
        websocket,
        {
            "event": "pong",
            "timestamp": datetime.now().isoformat(),
        },
    )


async def _handle_client_message(
    websocket: WebSocket,
    manager: Any,
    runtime: Any,
    message: dict[str, Any],
    current_subscriptions: set[str],
) -> set[str]:
    """
    Dispatch an incoming WebSocket message.
    """

    action = (
        str(
            message.get(
                "action",
                message.get("type", ""),
            )
        )
        .strip()
        .lower()
    )

    _debug("Dispatching client action='{}'", action)

    if action in {
        "subscribe",
        "subscription",
        "subscribe_instruments",
    }:
        return await _handle_subscribe_message(
            websocket,
            manager,
            runtime,
            message,
            current_subscriptions,
        )

    if action in {
        "unsubscribe",
        "unsubscribe_instruments",
    }:
        return await _handle_unsubscribe_message(
            websocket,
            manager,
            message,
            current_subscriptions,
        )

    if action in {
        "snapshot",
        "state",
        "get_state",
        "initial_state",
    }:
        await _handle_snapshot_message(
            websocket,
            runtime,
            current_subscriptions,
        )
        return current_subscriptions

    if action in {
        "ping",
        "heartbeat",
    }:
        await _handle_ping_message(websocket)
        return current_subscriptions

    if action in {
        "disconnect",
        "close",
    }:
        await _send_json(
            websocket,
            _success_response("Disconnect requested"),
        )

        raise WebSocketDisconnect

    await _send_json(
        websocket,
        _error_response(
            f"Unsupported WebSocket action: {action}",
            code="UNSUPPORTED_ACTION",
            supported_actions=[
                "subscribe",
                "unsubscribe",
                "snapshot",
                "ping",
            ],
        ),
    )

    return current_subscriptions


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()


@router.websocket(FULL_WEBSOCKET_PATH)
async def ema_websocket(
    websocket: WebSocket,
) -> None:
    """
    EMA real-time WebSocket endpoint.

    Default endpoint:
        /ws/ema

    Client examples:

    Subscribe to one instrument:

    {
        "action": "subscribe",
        "instrument_key": "NSE_INDEX|Nifty 50"
    }

    Subscribe to multiple instruments:

    {
        "action": "subscribe",
        "instrument_keys": [
            "NSE_INDEX|Nifty 50",
            "NSE_INDEX|Nifty Bank"
        ]
    }

    Subscribe to all selected instruments:

    {
        "action": "subscribe",
        "all": true
    }

    Unsubscribe:

    {
        "action": "unsubscribe",
        "instrument_key": "NSE_INDEX|Nifty 50"
    }

    Request current state:

    {
        "action": "snapshot"
    }

    Ping:

    {
        "action": "ping"
    }
    """

    await websocket.accept()

    runtime = _get_runtime(websocket)
    manager = _get_websocket_manager(websocket)
    query_service = _get_query_service(websocket)

    current_subscriptions: set[str] = set()

    client_id = f"{id(websocket)}"

    _debug(
        "WebSocket connected client_id={} path={}",
        client_id,
        FULL_WEBSOCKET_PATH,
    )

    logger.info(
        "EMA WebSocket connected client_id=%s",
        client_id,
    )

    try:
        await _send_json(
            websocket,
            {
                "event": "connected",
                "status": "success",
                "client_id": client_id,
                "timestamp": datetime.now().isoformat(),
                "websocket_path": FULL_WEBSOCKET_PATH,
                "available_actions": [
                    "subscribe",
                    "unsubscribe",
                    "snapshot",
                    "ping",
                ],
            },
        )

        # Register the client without forcing an initial subscription.
        if manager is not None:
            await _register_client(
                manager,
                websocket,
                [],
            )

        # If configured, automatically subscribe to all selected instruments.
        auto_subscribe = bool(
            getattr(
                config,
                "EMA_WEBSOCKET_AUTO_SUBSCRIBE",
                False,
            )
        )

        _debug("Auto subscribe enabled: {}", auto_subscribe)

        if auto_subscribe:
            current_subscriptions = set(_get_selected_instrument_keys(runtime))

            if current_subscriptions:
                await _update_subscription(
                    manager,
                    websocket,
                    sorted(current_subscriptions),
                )

                await _send_json(
                    websocket,
                    _success_response(
                        "Automatically subscribed to selected instruments",
                        subscribed_instruments=sorted(current_subscriptions),
                    ),
                )

                await _send_json(
                    websocket,
                    _build_initial_state_payload(
                        runtime,
                        sorted(current_subscriptions),
                    ),
                )

        while True:
            raw_message = await websocket.receive()

            if raw_message.get("type") == "websocket.disconnect":
                break

            text_data = raw_message.get("text")

            if text_data is None:
                bytes_data = raw_message.get("bytes")

                if bytes_data is not None:
                    with suppress(UnicodeDecodeError):
                        text_data = bytes_data.decode("utf-8")

            message = _parse_json_message(text_data)

            if not message:
                await _send_json(
                    websocket,
                    _error_response(
                        "Message must be a valid JSON object",
                        code="INVALID_MESSAGE",
                    ),
                )
                continue

            current_subscriptions = await _handle_client_message(
                websocket,
                manager,
                runtime,
                message,
                current_subscriptions,
            )

    except WebSocketDisconnect:
        _debug(
            "WebSocket disconnect caught client_id={}",
            client_id,
        )

        logger.info(
            "EMA WebSocket disconnected client_id=%s",
            client_id,
        )

    except asyncio.CancelledError:
        logger.info(
            "EMA WebSocket task cancelled client_id=%s",
            client_id,
        )
        raise

    except Exception:
        logger.exception(
            "EMA WebSocket connection failed client_id=%s",
            client_id,
        )

        with suppress(Exception):
            await _send_json(
                websocket,
                _error_response(
                    "Internal WebSocket error",
                    code="INTERNAL_WEBSOCKET_ERROR",
                ),
            )

    finally:
        if manager is not None:
            with suppress(Exception):
                await _unregister_client(
                    manager,
                    websocket,
                )

        _debug(
            "WebSocket cleanup completed client_id={}",
            client_id,
        )

        logger.info(
            "EMA WebSocket cleanup completed client_id=%s",
            client_id,
        )


@router.websocket(f"{FULL_WEBSOCKET_PATH}/{{instrument_key:path}}")
async def ema_instrument_websocket(
    websocket: WebSocket,
    instrument_key: str,
) -> None:
    """
    Convenience WebSocket endpoint for one instrument.

    Example:
        /ws/ema/NSE_INDEX%7CNifty%2050
    """

    await websocket.accept()

    runtime = _get_runtime(websocket)
    manager = _get_websocket_manager(websocket)

    client_id = f"{id(websocket)}"

    instrument_key = instrument_key.strip()

    _debug(
        "Instrument WebSocket connected client_id={} instrument={}",
        client_id,
        instrument_key,
    )

    logger.info(
        "Instrument WebSocket connected " "client_id=%s instrument=%s",
        client_id,
        instrument_key,
    )

    try:
        available_keys = set(_get_selected_instrument_keys(runtime))

        if instrument_key not in available_keys:
            await _send_json(
                websocket,
                _error_response(
                    "Instrument is not currently selected",
                    code="UNKNOWN_INSTRUMENT",
                    instrument_key=instrument_key,
                    available_instruments=sorted(available_keys),
                ),
            )

            await websocket.close(code=1008)
            return

        if manager is not None:
            await _register_client(
                manager,
                websocket,
                [instrument_key],
            )

        await _send_json(
            websocket,
            {
                "event": "connected",
                "status": "success",
                "client_id": client_id,
                "instrument_key": instrument_key,
                "timestamp": datetime.now().isoformat(),
            },
        )

        await _send_json(
            websocket,
            _build_initial_state_payload(
                runtime,
                [instrument_key],
            ),
        )

        while True:
            raw_message = await websocket.receive()

            if raw_message.get("type") == "websocket.disconnect":
                break

            text_data = raw_message.get("text")

            if text_data is None:
                bytes_data = raw_message.get("bytes")

                if bytes_data is not None:
                    with suppress(UnicodeDecodeError):
                        text_data = bytes_data.decode("utf-8")

            message = _parse_json_message(text_data)

            if not message:
                await _send_json(
                    websocket,
                    _error_response(
                        "Message must be a valid JSON object",
                        code="INVALID_MESSAGE",
                    ),
                )
                continue

            action = (
                str(
                    message.get(
                        "action",
                        message.get("type", ""),
                    )
                )
                .strip()
                .lower()
            )

            _debug(
                "Instrument action='{}' instrument={}",
                action,
                instrument_key,
            )

            if action in {
                "ping",
                "heartbeat",
            }:
                await _handle_ping_message(websocket)

            elif action in {
                "snapshot",
                "state",
                "get_state",
            }:
                await _send_json(
                    websocket,
                    _build_initial_state_payload(
                        runtime,
                        [instrument_key],
                    ),
                )

            elif action in {
                "close",
                "disconnect",
            }:
                break

            else:
                await _send_json(
                    websocket,
                    _error_response(
                        "This endpoint is dedicated to one instrument",
                        code="UNSUPPORTED_ACTION",
                        supported_actions=[
                            "snapshot",
                            "ping",
                            "close",
                        ],
                    ),
                )

    except WebSocketDisconnect:
        logger.info(
            "Instrument WebSocket disconnected " "client_id=%s instrument=%s",
            client_id,
            instrument_key,
        )

    except asyncio.CancelledError:
        raise

    except Exception:
        logger.exception(
            "Instrument WebSocket failed " "client_id=%s instrument=%s",
            client_id,
            instrument_key,
        )

    finally:
        if manager is not None:
            with suppress(Exception):
                await _unregister_client(
                    manager,
                    websocket,
                )

        _debug(
            "Instrument WebSocket cleanup completed client_id={} instrument={}",
            client_id,
            instrument_key,
        )

        logger.info(
            "Instrument WebSocket cleanup completed " "client_id=%s instrument=%s",
            client_id,
            instrument_key,
        )


__all__ = [
    "router",
    "ema_websocket",
    "ema_instrument_websocket",
]
