from __future__ import annotations

import asyncio
import inspect
import json
from contextlib import suppress
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, unquote

from fastapi import (
    APIRouter,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

DEBUG_MODE = False

WEBSOCKET_PREFIX = str(
    getattr(
        config,
        "EMA_WEBSOCKET_PREFIX",
        "/ws",
    )
).strip()

WEBSOCKET_PATH = str(
    getattr(
        config,
        "EMA_WEBSOCKET_PATH",
        "/ema",
    )
).strip()


def _build_websocket_path(
    prefix: str,
    path: str,
) -> str:
    prefix_value = prefix.strip("/")
    path_value = path.strip("/")

    normalized_prefix = (
        f"/{prefix_value}"
        if prefix_value
        else ""
    )

    normalized_path = (
        f"/{path_value}"
        if path_value
        else ""
    )

    if not normalized_prefix:
        return normalized_path or "/ws/ema"

    if not normalized_path:
        return normalized_prefix

    if normalized_path == normalized_prefix:
        return normalized_prefix

    if normalized_path.startswith(
        f"{normalized_prefix}/"
    ):
        return normalized_path

    return (
        f"{normalized_prefix}"
        f"{normalized_path}"
    )


FULL_WEBSOCKET_PATH = _build_websocket_path(
    WEBSOCKET_PREFIX,
    WEBSOCKET_PATH,
)

ALL_WEBSOCKET_PATH = (
    f"{FULL_WEBSOCKET_PATH}/all"
)

MULTIPLE_WEBSOCKET_PATH = (
    f"{FULL_WEBSOCKET_PATH}/multiple"
)


def _now_iso() -> str:
    return datetime.now(
        config.MARKET_TIMEZONE
    ).isoformat()


def _debug(
    message: str,
    *args: Any,
) -> None:
    if not DEBUG_MODE:
        return

    if args:
        message = message.format(*args)

    print(f"[EMA WS DEBUG] {message}")


def _json_safe(
    value: Any,
) -> Any:
    if value is None:
        return None

    if isinstance(
        value,
        (str, int, float, bool),
    ):
        return value

    if isinstance(
        value,
        (datetime, date),
    ):
        return value.isoformat()

    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
        }

    if isinstance(
        value,
        (list, tuple, set),
    ):
        return [
            _json_safe(item)
            for item in value
        ]

    if hasattr(value, "model_dump"):
        try:
            return _json_safe(
                value.model_dump(
                    mode="json"
                )
            )
        except TypeError:
            return _json_safe(
                value.model_dump()
            )

    if hasattr(value, "dict"):
        return _json_safe(
            value.dict()
        )

    if hasattr(value, "to_dict"):
        return _json_safe(
            value.to_dict()
        )

    if hasattr(value, "__dict__"):
        return _json_safe(
            vars(value)
        )

    return str(value)


def _parse_json_message(
    message: Any,
) -> dict[str, Any]:
    if isinstance(message, dict):
        return message

    if not isinstance(message, str):
        return {}

    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        return {}

    return (
        parsed
        if isinstance(parsed, dict)
        else {}
    )


def _normalise_instrument_keys(
    value: Any,
) -> list:
    if value is None:
        return []

    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(
        value,
        (list, tuple, set),
    ):
        values = value
    else:
        return []

    result: list[str] = []

    for item in values:
        if not isinstance(item, str):
            continue

        instrument_key = unquote(
            item
        ).strip()

        if (
            instrument_key
            and instrument_key not in result
        ):
            result.append(
                instrument_key
            )

    return result


def _get_application_runtime(
    application: Any,
) -> Any:
    state = getattr(
        application,
        "state",
        None,
    )

    if state is None:
        return None

    runtime = getattr(
        state,
        "ema_runtime",
        None,
    )

    if runtime is not None:
        return runtime

    return getattr(
        state,
        "runtime",
        None,
    )


def _get_runtime(
    websocket: WebSocket,
) -> Any:
    return _get_application_runtime(
        websocket.app
    )


def _get_runtime_from_request(
    request: Request,
) -> Any:
    return _get_application_runtime(
        request.app
    )


def _get_application_manager(
    application: Any,
) -> Any:
    state = getattr(
        application,
        "state",
        None,
    )

    if state is None:
        return None

    manager = getattr(
        state,
        "ema_websocket_manager",
        None,
    )

    if manager is not None:
        return manager

    return getattr(
        state,
        "websocket_manager",
        None,
    )


def _get_websocket_manager(
    websocket: WebSocket,
) -> Any:
    return _get_application_manager(
        websocket.app
    )


def _get_manager_from_request(
    request: Request,
) -> Any:
    return _get_application_manager(
        request.app
    )


def _get_runtime_contracts(
    runtime: Any,
) -> list[dict[str, Any]]:
    if runtime is None:
        return []

    contracts = getattr(
        runtime,
        "contracts",
        [],
    )

    if not isinstance(
        contracts,
        (list, tuple),
    ):
        return []

    return [
        contract
        for contract in contracts
        if isinstance(contract, dict)
    ]


def _get_runtime_states(
    runtime: Any,
) -> dict[str, Any]:
    if runtime is None:
        return {}

    states = getattr(
        runtime,
        "states",
        {},
    )

    return (
        states
        if isinstance(states, dict)
        else {}
    )


def _get_contract_by_instrument_key(
    runtime: Any,
    instrument_key: str,
) -> dict[str, Any] | None:
    for contract in _get_runtime_contracts(
        runtime
    ):
        if (
            str(
                contract.get(
                    "instrument_key"
                )
                or ""
            ).strip()
            == instrument_key
        ):
            return contract

    return None


def _get_selected_instrument_keys(
    runtime: Any,
) -> list:
    result: list[str] = []

    for contract in _get_runtime_contracts(
        runtime
    ):
        instrument_key = str(
            contract.get(
                "instrument_key"
            )
            or ""
        ).strip()

        if (
            instrument_key
            and instrument_key not in result
        ):
            result.append(
                instrument_key
            )

    return result


def _get_websocket_base_url(
    request: Request,
) -> str:
    forwarded_proto = (
        request.headers
        .get("x-forwarded-proto", "")
        .split(",")[0]
        .strip()
        .lower()
    )

    if forwarded_proto in {
        "https",
        "wss",
    }:
        websocket_scheme = "wss"

    elif forwarded_proto in {
        "http",
        "ws",
    }:
        websocket_scheme = "ws"

    else:
        websocket_scheme = (
            "wss"
            if request.url.scheme == "https"
            else "ws"
        )

    forwarded_host = (
        request.headers
        .get("x-forwarded-host", "")
        .split(",")[0]
        .strip()
    )

    host = (
        forwarded_host
        or request.headers.get("host")
        or request.url.netloc
    )

    return (
        f"{websocket_scheme}://"
        f"{host}"
        f"{FULL_WEBSOCKET_PATH}"
    )


def _get_auto_subscribe_setting() -> bool:
    value = getattr(
        config,
        "EMA_WEBSOCKET_AUTO_SUBSCRIBE",
        False,
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


async def _call_first_available(
    target: Any,
    method_names: list[str],
    *args: Any,
    **kwargs: Any,
) -> Any:
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

        result = method(
            *args,
            **kwargs,
        )

        if inspect.isawaitable(result):
            result = await result

        return result

    logger.warning(
        "WebSocket manager method unavailable "
        "methods=%s",
        method_names,
    )

    return None


async def _send_json(
    websocket: WebSocket,
    payload: dict[str, Any],
) -> bool:
    try:
        await websocket.send_json(
            _json_safe(payload)
        )

        return True

    except WebSocketDisconnect:
        return False

    except RuntimeError:
        logger.debug(
            "WebSocket message skipped because "
            "connection is closed"
        )

        return False

    except Exception:
        logger.exception(
            "WebSocket message send failed"
        )

        return False


async def _accept_and_reject(
    websocket: WebSocket,
    payload: dict[str, Any],
    *,
    close_code: int,
) -> None:
    try:
        await websocket.accept()

        await _send_json(
            websocket,
            payload,
        )

    finally:
        with suppress(Exception):
            await websocket.close(
                code=close_code
            )


def _success_response(
    message: str,
    **data: Any,
) -> dict[str, Any]:
    return {
        "status": "success",
        "message": message,
        "timestamp": _now_iso(),
        "data": data,
    }


def _error_response(
    message: str,
    code: str = "WEBSOCKET_ERROR",
    **data: Any,
) -> dict[str, Any]:
    return {
        "status": "error",
        "code": code,
        "message": message,
        "timestamp": _now_iso(),
        "data": data,
    }


def _build_initial_state_payload(
    runtime: Any,
    instrument_keys: list[str],
) -> dict[str, Any]:
    states = _get_runtime_states(
        runtime
    )

    instruments: list[
        dict[str, Any]
    ] = []

    for instrument_key in instrument_keys:
        contract = (
            _get_contract_by_instrument_key(
                runtime,
                instrument_key,
            )
        )

        state = states.get(
            instrument_key
        )

        symbol = None

        if isinstance(contract, dict):
            symbol = (
                contract.get(
                    "trading_symbol"
                )
                or contract.get("symbol")
                or contract.get(
                    "tradingsymbol"
                )
                or contract.get(
                    "underlying_symbol"
                )
                or instrument_key
            )

        instruments.append(
            {
                "instrument_key": (
                    instrument_key
                ),
                "symbol": symbol,
                "contract": _json_safe(
                    contract
                ),
                "state": _json_safe(
                    state
                ),
            }
        )

    return {
        "event": "initial_state",
        "timestamp": _now_iso(),
        "instrument_count": len(
            instruments
        ),
        "instruments": instruments,
    }


async def _register_client(
    manager: Any,
    websocket: WebSocket,
    client_id: str,
) -> Any:
    if manager is None:
        return None

    return await _call_first_available(
        manager,
        [
            "connect",
            "register",
            "add_client",
        ],
        websocket,
        client_id=client_id,
    )


async def _unregister_client(
    manager: Any,
    websocket: WebSocket,
) -> Any:
    if manager is None:
        return None

    return await _call_first_available(
        manager,
        [
            "disconnect",
            "unregister",
            "remove_client",
        ],
        websocket,
    )


async def _replace_subscription(
    manager: Any,
    websocket: WebSocket,
    instrument_keys: list[str],
) -> Any:
    if manager is None:
        return None

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


async def _subscribe_all(
    manager: Any,
    websocket: WebSocket,
    instrument_keys: list[str],
) -> Any:
    if manager is None:
        return None

    subscribe_all_method = getattr(
        manager,
        "subscribe_all",
        None,
    )

    if callable(subscribe_all_method):
        result = subscribe_all_method(
            websocket
        )

        if inspect.isawaitable(result):
            result = await result

        return result

    return await _replace_subscription(
        manager,
        websocket,
        instrument_keys,
    )


async def _remove_subscription(
    manager: Any,
    websocket: WebSocket,
    instrument_keys: list[str],
) -> Any:
    if manager is None:
        return None

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


async def _remove_all_subscriptions(
    manager: Any,
    websocket: WebSocket,
    instrument_keys: list[str],
) -> Any:
    if manager is None:
        return None

    unsubscribe_all_method = getattr(
        manager,
        "unsubscribe_all",
        None,
    )

    if callable(unsubscribe_all_method):
        result = unsubscribe_all_method(
            websocket
        )

        if inspect.isawaitable(result):
            result = await result

        return result

    return await _remove_subscription(
        manager,
        websocket,
        instrument_keys,
    )


async def _handle_subscribe_message(
    websocket: WebSocket,
    manager: Any,
    runtime: Any,
    message: dict[str, Any],
    current_subscriptions: set[str],
) -> set:
    available_keys = set(
        _get_selected_instrument_keys(
            runtime
        )
    )

    subscribe_all = bool(
        message.get("all")
        or message.get("subscribe_all")
    )

    if subscribe_all:
        requested_keys = sorted(
            available_keys
        )

        if not requested_keys:
            await _send_json(
                websocket,
                _error_response(
                    "No instruments are available",
                    code=(
                        "NO_AVAILABLE_INSTRUMENTS"
                    ),
                ),
            )

            return current_subscriptions

        if manager is not None:
            await _subscribe_all(
                manager,
                websocket,
                requested_keys,
            )

        updated_subscriptions = set(
            requested_keys
        )

        await _send_json(
            websocket,
            _success_response(
                "Subscribed to all instruments",
                subscription_mode="all",
                subscribed_instruments=(
                    requested_keys
                ),
                subscription_count=len(
                    requested_keys
                ),
            ),
        )

        await _send_json(
            websocket,
            _build_initial_state_payload(
                runtime,
                requested_keys,
            ),
        )

        return updated_subscriptions

    requested_keys = (
        _normalise_instrument_keys(
            message.get(
                "instrument_keys",
                message.get(
                    "instrument_key"
                ),
            )
        )
    )

    if not requested_keys:
        await _send_json(
            websocket,
            _error_response(
                "No valid instrument keys provided",
                code=(
                    "INVALID_SUBSCRIPTION"
                ),
            ),
        )

        return current_subscriptions

    unknown_keys = [
        key
        for key in requested_keys
        if key not in available_keys
    ]

    valid_keys = [
        key
        for key in requested_keys
        if key in available_keys
    ]

    if unknown_keys:
        await _send_json(
            websocket,
            _error_response(
                "One or more instruments are not selected",
                code="UNKNOWN_INSTRUMENT",
                unknown_instruments=(
                    unknown_keys
                ),
                available_instruments=sorted(
                    available_keys
                ),
            ),
        )

    if not valid_keys:
        return current_subscriptions

    updated_subscriptions = (
        current_subscriptions
        | set(valid_keys)
    )

    if manager is not None:
        await _replace_subscription(
            manager,
            websocket,
            sorted(
                updated_subscriptions
            ),
        )

    await _send_json(
        websocket,
        _success_response(
            "Subscription updated",
            subscription_mode=(
                "single"
                if len(
                    updated_subscriptions
                )
                == 1
                else "multiple"
            ),
            subscribed_instruments=sorted(
                updated_subscriptions
            ),
            subscription_count=len(
                updated_subscriptions
            ),
        ),
    )

    await _send_json(
        websocket,
        _build_initial_state_payload(
            runtime,
            sorted(
                updated_subscriptions
            ),
        ),
    )

    logger.info(
        "WebSocket subscription updated "
        "instruments=%s",
        sorted(
            updated_subscriptions
        ),
    )

    return updated_subscriptions


async def _handle_unsubscribe_message(
    websocket: WebSocket,
    manager: Any,
    message: dict[str, Any],
    current_subscriptions: set[str],
) -> set:
    unsubscribe_all = bool(
        message.get("all")
        or message.get("unsubscribe_all")
    )

    if unsubscribe_all:
        requested_keys = sorted(
            current_subscriptions
        )

        if manager is not None:
            await _remove_all_subscriptions(
                manager,
                websocket,
                requested_keys,
            )

        await _send_json(
            websocket,
            _success_response(
                "All subscriptions removed",
                unsubscribed_instruments=(
                    requested_keys
                ),
                subscribed_instruments=[],
                subscription_count=0,
            ),
        )

        return set()

    requested_keys = (
        _normalise_instrument_keys(
            message.get(
                "instrument_keys",
                message.get(
                    "instrument_key"
                ),
            )
        )
    )

    if not requested_keys:
        await _send_json(
            websocket,
            _error_response(
                "No valid instrument keys provided",
                code=(
                    "INVALID_UNSUBSCRIPTION"
                ),
            ),
        )

        return current_subscriptions

    matching_keys = [
        key
        for key in requested_keys
        if key in current_subscriptions
    ]

    if not matching_keys:
        await _send_json(
            websocket,
            _success_response(
                "No matching subscriptions found",
                subscribed_instruments=sorted(
                    current_subscriptions
                ),
                subscription_count=len(
                    current_subscriptions
                ),
            ),
        )

        return current_subscriptions

    if manager is not None:
        await _remove_subscription(
            manager,
            websocket,
            matching_keys,
        )

    updated_subscriptions = (
        current_subscriptions
        - set(matching_keys)
    )

    await _send_json(
        websocket,
        _success_response(
            "Subscription removed",
            unsubscribed_instruments=(
                matching_keys
            ),
            subscribed_instruments=sorted(
                updated_subscriptions
            ),
            subscription_count=len(
                updated_subscriptions
            ),
        ),
    )

    return updated_subscriptions


async def _handle_snapshot_message(
    websocket: WebSocket,
    runtime: Any,
    current_subscriptions: set[str],
) -> None:
    instrument_keys = sorted(
        current_subscriptions
    )

    if not instrument_keys:
        await _send_json(
            websocket,
            _error_response(
                "No instruments are currently subscribed",
                code="NO_SUBSCRIPTIONS",
                available_instruments=(
                    _get_selected_instrument_keys(
                        runtime
                    )
                ),
            ),
        )

        return

    await _send_json(
        websocket,
        _build_initial_state_payload(
            runtime,
            instrument_keys,
        ),
    )


async def _handle_ping_message(
    websocket: WebSocket,
) -> None:
    await _send_json(
        websocket,
        {
            "event": "pong",
            "timestamp": _now_iso(),
        },
    )


async def _handle_client_message(
    websocket: WebSocket,
    manager: Any,
    runtime: Any,
    message: dict[str, Any],
    current_subscriptions: set[str],
) -> set:
    action = str(
        message.get(
            "action",
            message.get("type", ""),
        )
    ).strip().lower()

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
        await _handle_ping_message(
            websocket
        )

        return current_subscriptions

    if action in {
        "disconnect",
        "close",
    }:
        await _send_json(
            websocket,
            _success_response(
                "Disconnect requested"
            ),
        )

        with suppress(Exception):
            await websocket.close(
                code=1000
            )

        raise WebSocketDisconnect

    await _send_json(
        websocket,
        _error_response(
            (
                "Unsupported WebSocket action: "
                f"{action or '(empty)'}"
            ),
            code="UNSUPPORTED_ACTION",
            supported_actions=[
                "subscribe",
                "unsubscribe",
                "snapshot",
                "ping",
                "disconnect",
            ],
        ),
    )

    return current_subscriptions


async def _receive_json_message(
    websocket: WebSocket,
) -> dict[str, Any] | None:
    raw_message = (
        await websocket.receive()
    )

    if (
        raw_message.get("type")
        == "websocket.disconnect"
    ):
        return None

    text_data = raw_message.get(
        "text"
    )

    if text_data is None:
        bytes_data = raw_message.get(
            "bytes"
        )

        if bytes_data is not None:
            try:
                text_data = bytes_data.decode(
                    "utf-8"
                )
            except UnicodeDecodeError:
                text_data = None

    return _parse_json_message(
        text_data
    )


async def _connect_client(
    websocket: WebSocket,
    manager: Any,
    client_id: str,
) -> bool:
    if manager is None:
        await _accept_and_reject(
            websocket,
            _error_response(
                "EMA WebSocket manager is not initialized",
                code="MANAGER_UNAVAILABLE",
            ),
            close_code=1013,
        )

        return False

    try:
        await _register_client(
            manager,
            websocket,
            client_id,
        )

        return True

    except RuntimeError as ex:
        await _accept_and_reject(
            websocket,
            _error_response(
                str(ex),
                code=(
                    "CONNECTION_REJECTED"
                ),
            ),
            close_code=1013,
        )

        return False


def _connected_payload(
    *,
    client_id: str,
    mode: str,
    subscriptions: list[str],
) -> dict[str, Any]:
    return {
        "event": "connected",
        "status": "success",
        "client_id": client_id,
        "timestamp": _now_iso(),
        "websocket_path": (
            FULL_WEBSOCKET_PATH
        ),
        "connection_mode": mode,
        "subscribed_instruments": (
            subscriptions
        ),
        "subscription_count": len(
            subscriptions
        ),
        "available_actions": [
            "subscribe",
            "unsubscribe",
            "snapshot",
            "ping",
            "disconnect",
        ],
    }


router = APIRouter()


@router.get(
    f"{FULL_WEBSOCKET_PATH}/available",
    tags=["WebSocket"],
    name="list_available_websockets",
)
async def list_available_websockets(
    request: Request,
) -> JSONResponse:
    runtime = _get_runtime_from_request(
        request
    )

    manager = _get_manager_from_request(
        request
    )

    websocket_url = (
        _get_websocket_base_url(
            request
        )
    )

    if runtime is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "message": (
                    "EMA runtime is not initialized"
                ),
                "timestamp": _now_iso(),
                "websocket_path": (
                    FULL_WEBSOCKET_PATH
                ),
                "websocket_url": (
                    websocket_url
                ),
                "available_instrument_count": 0,
                "available_instrument_keys": [],
                "instruments": [],
            },
        )

    contracts = _get_runtime_contracts(
        runtime
    )

    states = _get_runtime_states(
        runtime
    )

    instruments: list[
        dict[str, Any]
    ] = []

    for contract in contracts:
        instrument_key = str(
            contract.get(
                "instrument_key"
            )
            or ""
        ).strip()

        if not instrument_key:
            continue

        state = states.get(
            instrument_key
        )

        if not isinstance(state, dict):
            state = {}

        encoded_key = quote(
            instrument_key,
            safe="",
        )

        symbol = (
            contract.get("trading_symbol")
            or contract.get("symbol")
            or contract.get("tradingsymbol")
            or contract.get(
                "underlying_symbol"
            )
            or contract.get("underlying")
            or instrument_key
        )

        instruments.append(
            {
                "instrument_key": (
                    instrument_key
                ),
                "encoded_instrument_key": (
                    encoded_key
                ),
                "symbol": symbol,
                "trading_symbol": symbol,
                "strike_price": contract.get(
                    "strike_price"
                ),
                "option_type": contract.get(
                    "option_type"
                ),
                "expiry": (
                    contract.get("expiry")
                    or contract.get(
                        "expiry_date"
                    )
                ),
                "state_available": bool(
                    state
                ),
                "last_processed_timestamp": (
                    state.get(
                        "last_processed_timestamp"
                    )
                ),
                "ema_9": state.get(
                    "ema_9"
                ),
                "ema_21": state.get(
                    "ema_21"
                ),
                "single_websocket_url": (
                    f"{websocket_url}/"
                    f"{encoded_key}"
                ),
                "contract": _json_safe(
                    contract
                ),
            }
        )

    instruments.sort(
        key=lambda item: (
            float(
                item.get("strike_price")
                or 0
            ),
            str(
                item.get("option_type")
                or ""
            ),
        )
    )

    instrument_keys = [
        item["instrument_key"]
        for item in instruments
    ]

    encoded_query = quote(
        ",".join(instrument_keys),
        safe=",",
    )

    manager_status = None

    if manager is not None:
        status_method = getattr(
            manager,
            "get_status",
            None,
        )

        if callable(status_method):
            manager_status = (
                status_method()
            )

    payload = {
        "status": "success",
        "timestamp": _now_iso(),
        "discovery_path": (
            f"{FULL_WEBSOCKET_PATH}/available"
        ),
        "websocket_path": (
            FULL_WEBSOCKET_PATH
        ),
        "websocket_url": websocket_url,
        "all_websocket_url": (
            f"{websocket_url}/all"
        ),
        "multiple_websocket_url": (
            f"{websocket_url}/multiple"
            f"?instrument_keys={encoded_query}"
        ),
        "available_instrument_count": len(
            instruments
        ),
        "available_instrument_keys": (
            instrument_keys
        ),
        "instruments": instruments,
        "manager": _json_safe(
            manager_status
        ),
        "connection_options": {
            "dynamic": {
                "websocket_url": (
                    websocket_url
                ),
                "subscribe_message": {
                    "action": "subscribe",
                    "instrument_keys": [
                        "NSE_FO|instrument-key"
                    ],
                },
            },
            "all_instruments": {
                "websocket_url": (
                    f"{websocket_url}/all"
                ),
                "subscribe_required": False,
            },
            "multiple_instruments": {
                "url_template": (
                    f"{websocket_url}/multiple"
                    "?instrument_keys="
                    "NSE_FO%7C123,NSE_FO%7C456"
                ),
                "subscribe_required": False,
            },
            "single_instrument": {
                "url_template": (
                    f"{websocket_url}/"
                    "{encoded_instrument_key}"
                ),
                "subscribe_required": False,
            },
        },
        "supported_actions": [
            "subscribe",
            "unsubscribe",
            "snapshot",
            "ping",
            "disconnect",
        ],
        "live_event_shape": {
            "event": {
                "timestamp": (
                    "2026-09-22T09:17:00+05:30"
                ),
                "date": "2026-09-22",
                "open": 441.35,
                "high": 441.5,
                "low": 433.6,
                "close": 437.6,
                "volume": 11700,
                "open_interest": 429520,
                "source": "intraday",
                "ema_9": 446.499441,
                "ema_21": 447.370389,
                "ema_difference": -0.870948,
                "previous_ema_difference": (
                    0.376873
                ),
                "cross_type": "bearish",
                "is_bullish_cross": False,
                "is_bearish_cross": True,
            },
            "instrument_key": (
                "NSE_FO|instrument-key"
            ),
            "symbol": "NIFTY OPTION",
            "strike_price": 23000.0,
            "option_type": "CE",
        },
    }

    return JSONResponse(
        status_code=200,
        content=_json_safe(payload),
    )


@router.websocket(
    FULL_WEBSOCKET_PATH,
    name="ema_websocket",
)
async def ema_websocket(
    websocket: WebSocket,
) -> None:
    runtime = _get_runtime(
        websocket
    )

    manager = _get_websocket_manager(
        websocket
    )

    client_id = str(
        id(websocket)
    )

    current_subscriptions: set[str] = set()
    client_registered = False

    try:
        if runtime is None:
            await _accept_and_reject(
                websocket,
                _error_response(
                    "EMA runtime is not initialized",
                    code="RUNTIME_UNAVAILABLE",
                ),
                close_code=1013,
            )

            return

        client_registered = (
            await _connect_client(
                websocket,
                manager,
                client_id,
            )
        )

        if not client_registered:
            return

        await _send_json(
            websocket,
            _connected_payload(
                client_id=client_id,
                mode="dynamic",
                subscriptions=[],
            ),
        )

        await _send_json(
            websocket,
            {
                "event": (
                    "available_instruments"
                ),
                "timestamp": _now_iso(),
                "instrument_keys": (
                    _get_selected_instrument_keys(
                        runtime
                    )
                ),
            },
        )

        if _get_auto_subscribe_setting():
            current_subscriptions = set(
                _get_selected_instrument_keys(
                    runtime
                )
            )

            if current_subscriptions:
                await _subscribe_all(
                    manager,
                    websocket,
                    sorted(
                        current_subscriptions
                    ),
                )

                await _send_json(
                    websocket,
                    _success_response(
                        (
                            "Automatically subscribed "
                            "to all instruments"
                        ),
                        subscription_mode="all",
                        subscribed_instruments=sorted(
                            current_subscriptions
                        ),
                    ),
                )

                await _send_json(
                    websocket,
                    _build_initial_state_payload(
                        runtime,
                        sorted(
                            current_subscriptions
                        ),
                    ),
                )

        while True:
            message = await _receive_json_message(
                websocket
            )

            if message is None:
                break

            if not message:
                await _send_json(
                    websocket,
                    _error_response(
                        (
                            "Message must be a valid "
                            "JSON object"
                        ),
                        code="INVALID_MESSAGE",
                    ),
                )

                continue

            current_subscriptions = (
                await _handle_client_message(
                    websocket,
                    manager,
                    runtime,
                    message,
                    current_subscriptions,
                )
            )

    except WebSocketDisconnect:
        logger.info(
            "EMA WebSocket disconnected "
            "client_id=%s",
            client_id,
        )

    except asyncio.CancelledError:
        raise

    except Exception:
        logger.exception(
            "EMA WebSocket connection failed "
            "client_id=%s",
            client_id,
        )

        with suppress(Exception):
            await _send_json(
                websocket,
                _error_response(
                    "Internal WebSocket error",
                    code=(
                        "INTERNAL_WEBSOCKET_ERROR"
                    ),
                ),
            )

        with suppress(Exception):
            await websocket.close(
                code=1011
            )

    finally:
        if client_registered:
            with suppress(Exception):
                await _unregister_client(
                    manager,
                    websocket,
                )


@router.websocket(
    ALL_WEBSOCKET_PATH,
    name="ema_all_instruments_websocket",
)
async def ema_all_instruments_websocket(
    websocket: WebSocket,
) -> None:
    runtime = _get_runtime(
        websocket
    )

    manager = _get_websocket_manager(
        websocket
    )

    client_id = str(
        id(websocket)
    )

    client_registered = False
    instrument_keys: list[str] = []

    try:
        if runtime is None:
            await _accept_and_reject(
                websocket,
                _error_response(
                    "EMA runtime is not initialized",
                    code="RUNTIME_UNAVAILABLE",
                ),
                close_code=1013,
            )

            return

        instrument_keys = (
            _get_selected_instrument_keys(
                runtime
            )
        )

        if not instrument_keys:
            await _accept_and_reject(
                websocket,
                _error_response(
                    "No instruments are available",
                    code=(
                        "NO_AVAILABLE_INSTRUMENTS"
                    ),
                ),
                close_code=1013,
            )

            return

        client_registered = (
            await _connect_client(
                websocket,
                manager,
                client_id,
            )
        )

        if not client_registered:
            return

        await _subscribe_all(
            manager,
            websocket,
            instrument_keys,
        )

        await _send_json(
            websocket,
            _connected_payload(
                client_id=client_id,
                mode="all",
                subscriptions=instrument_keys,
            ),
        )

        await _send_json(
            websocket,
            _build_initial_state_payload(
                runtime,
                instrument_keys,
            ),
        )

        while True:
            message = await _receive_json_message(
                websocket
            )

            if message is None:
                break

            if not message:
                await _send_json(
                    websocket,
                    _error_response(
                        (
                            "Message must be a valid "
                            "JSON object"
                        ),
                        code="INVALID_MESSAGE",
                    ),
                )

                continue

            action = str(
                message.get(
                    "action",
                    message.get("type", ""),
                )
            ).strip().lower()

            if action in {
                "ping",
                "heartbeat",
            }:
                await _handle_ping_message(
                    websocket
                )

            elif action in {
                "snapshot",
                "state",
                "get_state",
            }:
                await _send_json(
                    websocket,
                    _build_initial_state_payload(
                        runtime,
                        instrument_keys,
                    ),
                )

            elif action in {
                "close",
                "disconnect",
            }:
                await _send_json(
                    websocket,
                    _success_response(
                        "Disconnect requested"
                    ),
                )

                with suppress(Exception):
                    await websocket.close(
                        code=1000
                    )

                break

            else:
                await _send_json(
                    websocket,
                    _error_response(
                        (
                            "This endpoint is subscribed "
                            "to all instruments"
                        ),
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
            "All-instrument WebSocket "
            "disconnected client_id=%s",
            client_id,
        )

    except asyncio.CancelledError:
        raise

    except Exception:
        logger.exception(
            "All-instrument WebSocket failed "
            "client_id=%s",
            client_id,
        )

        with suppress(Exception):
            await websocket.close(
                code=1011
            )

    finally:
        if client_registered:
            with suppress(Exception):
                await _unregister_client(
                    manager,
                    websocket,
                )


@router.websocket(
    MULTIPLE_WEBSOCKET_PATH,
    name="ema_multiple_instruments_websocket",
)
async def ema_multiple_instruments_websocket(
    websocket: WebSocket,
) -> None:
    runtime = _get_runtime(
        websocket
    )

    manager = _get_websocket_manager(
        websocket
    )

    client_id = str(
        id(websocket)
    )

    client_registered = False

    requested_keys = (
        _normalise_instrument_keys(
            websocket.query_params.get(
                "instrument_keys"
            )
        )
    )

    try:
        if runtime is None:
            await _accept_and_reject(
                websocket,
                _error_response(
                    "EMA runtime is not initialized",
                    code="RUNTIME_UNAVAILABLE",
                ),
                close_code=1013,
            )

            return

        available_keys = set(
            _get_selected_instrument_keys(
                runtime
            )
        )

        unknown_keys = [
            key
            for key in requested_keys
            if key not in available_keys
        ]

        valid_keys = [
            key
            for key in requested_keys
            if key in available_keys
        ]

        if not valid_keys:
            await _accept_and_reject(
                websocket,
                _error_response(
                    (
                        "No valid instrument keys "
                        "were supplied"
                    ),
                    code=(
                        "INVALID_SUBSCRIPTION"
                    ),
                    unknown_instruments=(
                        unknown_keys
                    ),
                    available_instruments=sorted(
                        available_keys
                    ),
                ),
                close_code=1008,
            )

            return

        client_registered = (
            await _connect_client(
                websocket,
                manager,
                client_id,
            )
        )

        if not client_registered:
            return

        await _replace_subscription(
            manager,
            websocket,
            valid_keys,
        )

        await _send_json(
            websocket,
            _connected_payload(
                client_id=client_id,
                mode="multiple",
                subscriptions=valid_keys,
            ),
        )

        if unknown_keys:
            await _send_json(
                websocket,
                _error_response(
                    (
                        "Some requested instruments "
                        "were ignored"
                    ),
                    code="UNKNOWN_INSTRUMENT",
                    unknown_instruments=(
                        unknown_keys
                    ),
                    subscribed_instruments=(
                        valid_keys
                    ),
                ),
            )

        await _send_json(
            websocket,
            _build_initial_state_payload(
                runtime,
                valid_keys,
            ),
        )

        current_subscriptions = set(
            valid_keys
        )

        while True:
            message = await _receive_json_message(
                websocket
            )

            if message is None:
                break

            if not message:
                await _send_json(
                    websocket,
                    _error_response(
                        (
                            "Message must be a valid "
                            "JSON object"
                        ),
                        code="INVALID_MESSAGE",
                    ),
                )

                continue

            current_subscriptions = (
                await _handle_client_message(
                    websocket,
                    manager,
                    runtime,
                    message,
                    current_subscriptions,
                )
            )

    except WebSocketDisconnect:
        logger.info(
            "Multiple-instrument WebSocket "
            "disconnected client_id=%s",
            client_id,
        )

    except asyncio.CancelledError:
        raise

    except Exception:
        logger.exception(
            "Multiple-instrument WebSocket failed "
            "client_id=%s",
            client_id,
        )

        with suppress(Exception):
            await websocket.close(
                code=1011
            )

    finally:
        if client_registered:
            with suppress(Exception):
                await _unregister_client(
                    manager,
                    websocket,
                )


@router.websocket(
    (
        f"{FULL_WEBSOCKET_PATH}/"
        "{instrument_key:path}"
    ),
    name="ema_instrument_websocket",
)
async def ema_instrument_websocket(
    websocket: WebSocket,
    instrument_key: str,
) -> None:
    runtime = _get_runtime(
        websocket
    )

    manager = _get_websocket_manager(
        websocket
    )

    client_id = str(
        id(websocket)
    )

    client_registered = False

    instrument_key = unquote(
        instrument_key
    ).strip()

    try:
        if runtime is None:
            await _accept_and_reject(
                websocket,
                _error_response(
                    "EMA runtime is not initialized",
                    code="RUNTIME_UNAVAILABLE",
                ),
                close_code=1013,
            )

            return

        available_keys = set(
            _get_selected_instrument_keys(
                runtime
            )
        )

        if instrument_key not in available_keys:
            await _accept_and_reject(
                websocket,
                _error_response(
                    (
                        "Instrument is not "
                        "currently selected"
                    ),
                    code="UNKNOWN_INSTRUMENT",
                    instrument_key=instrument_key,
                    available_instruments=sorted(
                        available_keys
                    ),
                ),
                close_code=1008,
            )

            return

        client_registered = (
            await _connect_client(
                websocket,
                manager,
                client_id,
            )
        )

        if not client_registered:
            return

        await _replace_subscription(
            manager,
            websocket,
            [instrument_key],
        )

        contract = (
            _get_contract_by_instrument_key(
                runtime,
                instrument_key,
            )
        )

        symbol = (
            contract.get("trading_symbol")
            or contract.get("symbol")
            or contract.get("tradingsymbol")
            or instrument_key
            if isinstance(contract, dict)
            else instrument_key
        )

        connected_payload = (
            _connected_payload(
                client_id=client_id,
                mode="single",
                subscriptions=[
                    instrument_key
                ],
            )
        )

        connected_payload[
            "instrument_key"
        ] = instrument_key

        connected_payload[
            "symbol"
        ] = symbol

        await _send_json(
            websocket,
            connected_payload,
        )

        await _send_json(
            websocket,
            _build_initial_state_payload(
                runtime,
                [instrument_key],
            ),
        )

        while True:
            message = await _receive_json_message(
                websocket
            )

            if message is None:
                break

            if not message:
                await _send_json(
                    websocket,
                    _error_response(
                        (
                            "Message must be a valid "
                            "JSON object"
                        ),
                        code="INVALID_MESSAGE",
                    ),
                )

                continue

            action = str(
                message.get(
                    "action",
                    message.get("type", ""),
                )
            ).strip().lower()

            if action in {
                "ping",
                "heartbeat",
            }:
                await _handle_ping_message(
                    websocket
                )

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
                await _send_json(
                    websocket,
                    _success_response(
                        "Disconnect requested"
                    ),
                )

                with suppress(Exception):
                    await websocket.close(
                        code=1000
                    )

                break

            else:
                await _send_json(
                    websocket,
                    _error_response(
                        (
                            "This endpoint is dedicated "
                            "to one instrument"
                        ),
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
            "Instrument WebSocket disconnected "
            "client_id=%s instrument=%s",
            client_id,
            instrument_key,
        )

    except asyncio.CancelledError:
        raise

    except Exception:
        logger.exception(
            "Instrument WebSocket failed "
            "client_id=%s instrument=%s",
            client_id,
            instrument_key,
        )

        with suppress(Exception):
            await websocket.close(
                code=1011
            )

    finally:
        if client_registered:
            with suppress(Exception):
                await _unregister_client(
                    manager,
                    websocket,
                )


logger.info(
    "WebSocket routes configured "
    "discovery=%s dynamic=%s all=%s "
    "multiple=%s single=%s",
    f"{FULL_WEBSOCKET_PATH}/available",
    FULL_WEBSOCKET_PATH,
    ALL_WEBSOCKET_PATH,
    MULTIPLE_WEBSOCKET_PATH,
    (
        f"{FULL_WEBSOCKET_PATH}/"
        "{instrument_key:path}"
    ),
)


__all__ = [
    "FULL_WEBSOCKET_PATH",
    "ALL_WEBSOCKET_PATH",
    "MULTIPLE_WEBSOCKET_PATH",
    "router",
    "list_available_websockets",
    "ema_websocket",
    "ema_all_instruments_websocket",
    "ema_multiple_instruments_websocket",
    "ema_instrument_websocket",
]