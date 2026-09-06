import logging
from typing import Any

import upstox_client
from upstox_client.rest import ApiException

from core import config
from services.token_service import token_service

logger = logging.getLogger(__name__)


def _get_config_value(name: str, default: Any = None) -> Any:
    return getattr(config, name, default)


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return bool(value)

    if isinstance(value, str):
        return value.strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }

    return default


def _to_int(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_api_response(response: Any) -> Any:
    """
    Convert an Upstox SDK response into a MongoDB/JSON-friendly value.

    No access token is ever included here.
    """
    if response is None:
        return None

    if isinstance(response, (str, int, float, bool)):
        return response

    if isinstance(response, dict):
        return {str(key): _clean_api_response(value) for key, value in response.items()}

    if isinstance(response, (list, tuple)):
        return [_clean_api_response(value) for value in response]

    for method_name in ("to_dict", "to_dict_recursive"):
        method = getattr(response, method_name, None)

        if callable(method):
            try:
                return _clean_api_response(method())
            except Exception:
                pass

    if hasattr(response, "__dict__"):
        try:
            return {
                str(key): _clean_api_response(value)
                for key, value in vars(response).items()
                if not str(key).startswith("_")
            }
        except Exception:
            pass

    return str(response)


def _get_access_token() -> str | None:
    """
    Retrieve the current access token from the existing project token service.

    The token itself is intentionally never logged or returned in execution
    results.
    """
    try:
        access_token = token_service.get_access_token()

        if not access_token:
            logger.error("Upstox access token is not available.")

            return None

        return access_token

    except Exception as exc:
        logger.exception(
            "Failed to retrieve Upstox access token: %s",
            exc,
        )

        return None


def _create_order_api(access_token: str):
    """
    Create the Upstox OrderApi using the project's cached access token.
    """
    configuration = upstox_client.Configuration()
    configuration.access_token = access_token

    api_client = upstox_client.ApiClient(configuration)

    return upstox_client.OrderApi(api_client)


def _get_instrument_key(instrument: dict[str, Any]) -> str | None:
    """
    Extract the Upstox instrument key from the selected instrument.

    The selected instrument is expected to come directly from the producer's
    budget-filtered instrument list.
    """
    value = instrument.get("instrument_key")

    if value is None:
        value = instrument.get("instrument_token")

    if value is None:
        return None

    value = str(value).strip()

    return value or None


def _get_lot_size(instrument: dict[str, Any]) -> int | None:
    """
    Use the selected instrument's lot_size.

    Quantity is intentionally not hardcoded to 65.
    """
    quantity = _to_int(instrument.get("lot_size"))

    if quantity is None:
        quantity = _to_int(instrument.get("lotSize"))

    if quantity is None:
        quantity = _to_int(instrument.get("quantity"))

    if quantity is None or quantity <= 0:
        return None

    return quantity


def _build_place_order_request(
    instrument_key: str,
    quantity: int,
    transaction_type: str,
    product: str,
    order_type: str,
    price: float,
    trigger_price: float,
    validity: str,
    disclosed_quantity: int,
    is_amo: bool,
    tag: str,
):
    """
    Build the Upstox PlaceOrderRequest.

    This is kept in the execution service so that instrument selection remains
    independent from broker order placement.
    """
    return upstox_client.PlaceOrderRequest(
        quantity=quantity,
        product=product,
        validity=validity,
        price=price,
        tag=tag,
        instrument_token=instrument_key,
        order_type=order_type,
        transaction_type=transaction_type,
        disclosed_quantity=disclosed_quantity,
        trigger_price=trigger_price,
        is_amo=is_amo,
    )


def exit_all_positions(order_api=None) -> dict[str, Any]:
    """
    Exit all currently open Upstox positions.

    A successful call is required before a new order is placed.

    Returns a structured result instead of raising broker exceptions so the
    caller can make an explicit decision about whether to continue.
    """
    result: dict[str, Any] = {
        "attempted": False,
        "success": False,
        "response": None,
        "error": None,
    }

    access_token = _get_access_token()

    if not access_token:
        result["error"] = "Upstox access token is not available."

        return result

    try:
        if order_api is None:
            order_api = _create_order_api(access_token)

        result["attempted"] = True

        logger.info("Exiting all previous Upstox positions before new order.")

        api_response = order_api.exit_positions()

        result["success"] = True
        result["response"] = _clean_api_response(api_response)

        logger.info("Successfully requested exit of all previous Upstox positions.")

        return result

    except ApiException as exc:
        error_body = getattr(exc, "body", None)

        if error_body:
            error_message = str(error_body)
        else:
            error_message = str(exc)

        result["error"] = error_message

        logger.error(
            "Upstox exit_positions() failed: %s",
            error_message,
        )

        return result

    except Exception as exc:
        result["error"] = str(exc)

        logger.exception(
            "Unexpected error while exiting previous positions: %s",
            exc,
        )

        return result


def place_selected_order(
    selected_instrument: dict[str, Any],
    transaction_type: str | None = None,
    product: str | None = None,
    order_type: str | None = None,
    price: float | None = None,
    trigger_price: float | None = None,
    validity: str | None = None,
    disclosed_quantity: int | None = None,
    is_amo: bool | None = None,
    tag: str | None = None,
    order_api=None,
) -> dict[str, Any]:
    """
    Place a new order for the already-selected instrument.

    IMPORTANT:
    This function does NOT perform instrument selection.

    The caller must provide the exact instrument selected by the selection
    service.
    """
    result: dict[str, Any] = {
        "attempted": False,
        "success": False,
        "instrument_key": None,
        "trading_symbol": None,
        "transaction_type": None,
        "quantity": None,
        "order_type": None,
        "product": None,
        "validity": None,
        "response": None,
        "error": None,
    }

    if not isinstance(selected_instrument, dict):
        result["error"] = "Selected instrument must be a dictionary."

        return result

    instrument_key = _get_instrument_key(selected_instrument)
    quantity = _get_lot_size(selected_instrument)

    result["instrument_key"] = instrument_key
    result["trading_symbol"] = selected_instrument.get("trading_symbol")

    if not instrument_key:
        result["error"] = "Selected instrument does not contain a valid instrument_key."

        logger.error(result["error"])

        return result

    if quantity is None:
        result["error"] = (
            "Selected instrument does not contain a valid positive lot_size."
        )

        logger.error(
            "Cannot place order for %s: lot_size is missing or invalid.",
            instrument_key,
        )

        return result

    transaction_type = transaction_type or _get_config_value(
        "ORDER_TRANSACTION_TYPE",
        "BUY",
    )

    product = product or _get_config_value(
        "ORDER_PRODUCT",
        "I",
    )

    order_type = order_type or _get_config_value(
        "ORDER_TYPE",
        "MARKET",
    )

    price = (
        _to_float(price)
        if price is not None
        else _to_float(
            _get_config_value(
                "ORDER_PRICE",
                0.0,
            )
        )
    )

    trigger_price = (
        _to_float(trigger_price)
        if trigger_price is not None
        else _to_float(
            _get_config_value(
                "ORDER_TRIGGER_PRICE",
                0.0,
            )
        )
    )

    validity = validity or _get_config_value(
        "ORDER_VALIDITY",
        "DAY",
    )

    disclosed_quantity = (
        _to_int(disclosed_quantity, 0)
        if disclosed_quantity is not None
        else _to_int(
            _get_config_value(
                "ORDER_DISCLOSED_QUANTITY",
                0,
            ),
            0,
        )
    )

    is_amo = (
        bool(is_amo)
        if is_amo is not None
        else _to_bool(
            _get_config_value(
                "ORDER_IS_AMO",
                False,
            ),
            False,
        )
    )

    tag = tag or _get_config_value(
        "ORDER_TAG",
        "EMA_ALGO",
    )

    result["transaction_type"] = transaction_type
    result["quantity"] = quantity
    result["order_type"] = order_type
    result["product"] = product
    result["validity"] = validity

    access_token = _get_access_token()

    if not access_token:
        result["error"] = "Upstox access token is not available."

        return result

    try:
        if order_api is None:
            order_api = _create_order_api(access_token)

        body = _build_place_order_request(
            instrument_key=instrument_key,
            quantity=quantity,
            transaction_type=transaction_type,
            product=product,
            order_type=order_type,
            price=price,
            trigger_price=trigger_price,
            validity=validity,
            disclosed_quantity=disclosed_quantity or 0,
            is_amo=is_amo,
            tag=tag,
        )

        result["attempted"] = True

        logger.info(
            "Placing Upstox order: instrument=%s symbol=%s "
            "transaction_type=%s quantity=%s order_type=%s "
            "product=%s validity=%s",
            instrument_key,
            selected_instrument.get("trading_symbol"),
            transaction_type,
            quantity,
            order_type,
            product,
            validity,
        )

        api_response = order_api.place_order(
            body=body,
            api_version="2.0",
        )

        result["success"] = True
        result["response"] = _clean_api_response(api_response)

        logger.info(
            "Upstox order placed successfully: instrument=%s quantity=%s",
            instrument_key,
            quantity,
        )

        return result

    except ApiException as exc:
        error_body = getattr(exc, "body", None)

        if error_body:
            error_message = str(error_body)
        else:
            error_message = str(exc)

        result["error"] = error_message

        logger.error(
            "Upstox place_order() failed for instrument=%s: %s",
            instrument_key,
            error_message,
        )

        return result

    except Exception as exc:
        result["error"] = str(exc)

        logger.exception(
            "Unexpected error while placing order for instrument=%s: %s",
            instrument_key,
            exc,
        )

        return result


def execute_selected_order(
    selected_instrument: dict[str, Any],
    transaction_type: str | None = None,
    product: str | None = None,
    order_type: str | None = None,
    price: float | None = None,
    trigger_price: float | None = None,
    validity: str | None = None,
    disclosed_quantity: int | None = None,
    is_amo: bool | None = None,
    tag: str | None = None,
) -> dict[str, Any]:
    """
    Execute the complete broker-side workflow.

    Workflow:

        selected instrument
            |
            v
        check execution enabled
            |
            v
        get Upstox token
            |
            v
        exit all previous positions
            |
            +---- failure ---> STOP
            |
            v
        place new selected order
            |
            v
        return structured execution result

    The new order is NEVER attempted when exit_positions() fails.
    """
    result: dict[str, Any] = {
        "enabled": False,
        "attempted": False,
        "success": False,
        "previous_positions_exit": None,
        "new_order": None,
        "error": None,
    }

    execution_enabled = _to_bool(
        _get_config_value(
            "ORDER_PLACEMENT_ENABLED",
            False,
        ),
        False,
    )

    result["enabled"] = execution_enabled

    if not execution_enabled:
        result["error"] = "Order placement is disabled by configuration."

        logger.info("Order execution skipped because ORDER_PLACEMENT_ENABLED=false.")

        return result

    if not isinstance(selected_instrument, dict):
        result["error"] = "No valid selected instrument was supplied."

        logger.error(result["error"])

        return result

    instrument_key = _get_instrument_key(selected_instrument)

    if not instrument_key:
        result["error"] = "Selected instrument does not contain a valid instrument_key."

        logger.error(result["error"])

        return result

    access_token = _get_access_token()

    if not access_token:
        result["error"] = "Upstox access token is not available."

        return result

    try:
        order_api = _create_order_api(access_token)
    except Exception as exc:
        result["error"] = str(exc)

        logger.exception(
            "Failed to create Upstox OrderApi: %s",
            exc,
        )

        return result

    exit_previous_positions = _to_bool(
        _get_config_value(
            "ORDER_EXIT_PREVIOUS_POSITIONS",
            True,
        ),
        True,
    )

    if exit_previous_positions:
        exit_result = exit_all_positions(order_api=order_api)

        result["previous_positions_exit"] = exit_result

        if not exit_result.get("success"):
            result["error"] = (
                "Previous position exit failed. " "New order was NOT placed."
            )

            logger.error(
                "Aborting new order for instrument=%s because "
                "exit_positions() failed.",
                instrument_key,
            )

            return result

    else:
        logger.warning(
            "ORDER_EXIT_PREVIOUS_POSITIONS=false. "
            "Previous positions will NOT be exited."
        )

        result["previous_positions_exit"] = {
            "attempted": False,
            "success": True,
            "skipped": True,
            "response": None,
            "error": None,
        }

    new_order_result = place_selected_order(
        selected_instrument=selected_instrument,
        transaction_type=transaction_type,
        product=product,
        order_type=order_type,
        price=price,
        trigger_price=trigger_price,
        validity=validity,
        disclosed_quantity=disclosed_quantity,
        is_amo=is_amo,
        tag=tag,
        order_api=order_api,
    )

    result["new_order"] = new_order_result
    result["attempted"] = new_order_result.get("attempted", False)
    result["success"] = new_order_result.get("success", False)

    if not result["success"]:
        result["error"] = new_order_result.get("error") or "New order placement failed."

        return result

    logger.info(
        "Order execution workflow completed successfully: " "instrument=%s",
        instrument_key,
    )

    return result
