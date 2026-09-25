from datetime import datetime
from typing import Any

import upstox_client
from upstox_client.rest import ApiException

from core.logger import get_logger
from services.token_service import token_service

logger = get_logger("place_order")


def _serialize_upstox_response(value: Any) -> Any:
    """
    Converts Upstox SDK response objects into plain dicts / lists / primitives
    so they can be JSON-encoded, stored in MongoDB, and copied safely.

    This handles:
        - primitives (str, int, float, bool, None)
        - dicts / lists / tuples (recursively)
        - SDK objects that expose `.to_dict()`
        - SDK objects that expose `__dict__`
        - anything else -> str(value)
    """
    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, dict):
        return {
            key: _serialize_upstox_response(val)
            for key, val in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [
            _serialize_upstox_response(item)
            for item in value
        ]

    # SDK objects usually expose `.to_dict()` first.
    to_dict = getattr(value, "to_dict", None)

    if callable(to_dict):
        try:
            return _serialize_upstox_response(to_dict())

        except Exception:
            logger.exception(
                "Failed to serialize Upstox SDK object using to_dict(). "
                "Falling back to __dict__ / str()."
            )

    # Fallback: use __dict__ (public attributes only).
    if hasattr(value, "__dict__"):
        try:
            return {
                key: _serialize_upstox_response(val)
                for key, val in vars(value).items()
                if not key.startswith("_") or key == "_data" or key == "_status"
            }

        except Exception:
            logger.exception(
                "Failed to serialize Upstox SDK object using __dict__. "
                "Falling back to str()."
            )

    return str(value)


def _get_order_api() -> upstox_client.OrderApi:
    access_token = token_service.get_access_token()

    if not access_token:
        raise RuntimeError("Upstox access token is not available.")

    configuration = upstox_client.Configuration()
    configuration.access_token = access_token

    api_client = upstox_client.ApiClient(configuration)

    return upstox_client.OrderApi(api_client)


def place_market_order(
    instrument_key: str,
    quantity: int = 65,
    transaction_type: str = "BUY",
    product: str = "D",
    validity: str = "DAY",
    tag: str = "EMA_ALGO",
) -> dict[str, Any]:

    order_request = {
        "instrument_key": instrument_key,
        "quantity": quantity,
        "transaction_type": transaction_type,
        "product": product,
        "validity": validity,
        "tag": tag,
        "order_type": "MARKET",
        "price": 0.0,
        "trigger_price": 0.0,
        "is_amo": False,
    }

    try:
        api = _get_order_api()

        body = upstox_client.PlaceOrderRequest(
            quantity=quantity,
            product=product,
            validity=validity,
            price=0.0,
            tag=tag,
            instrument_token=instrument_key,
            order_type="MARKET",
            transaction_type=transaction_type,
            disclosed_quantity=0,
            trigger_price=0.0,
            is_amo=False,
        )

        response = api.place_order(
            body,
            "2.0",
        )

        # Convert the SDK response object into plain Python data
        # so it can be stored in MongoDB / copied / JSON-encoded.
        serialized_response = _serialize_upstox_response(response)

        logger.info(
            "Order placed successfully " "instrument=%s quantity=%s",
            instrument_key,
            quantity,
        )

        return {
            "success": True,
            "placed_at": datetime.now().isoformat(),
            "request": order_request,
            "response": serialized_response,
            "error": None,
        }

    except ApiException as exc:
        error_message = exc.body if getattr(exc, "body", None) else str(exc)

        logger.exception("Upstox place_order failed.")

        return {
            "success": False,
            "placed_at": datetime.now().isoformat(),
            "request": order_request,
            "response": None,
            "error": error_message,
        }

    except Exception as exc:
        logger.exception("Unexpected error while placing order.")

        return {
            "success": False,
            "placed_at": datetime.now().isoformat(),
            "request": order_request,
            "response": None,
            "error": str(exc),
        }


def place_selected_instrument(
    selected_instrument: dict[str, Any],
) -> dict[str, Any]:

    if not selected_instrument:
        return {
            "success": False,
            "error": "Selected instrument is empty.",
        }

    instrument_key = selected_instrument.get("instrument_key")

    lot_size = selected_instrument.get("lot_size")

    trading_symbol = selected_instrument.get("trading_symbol")

    if not instrument_key:
        return {
            "success": False,
            "error": "instrument_key not found.",
        }

    if not lot_size:
        return {
            "success": False,
            "error": "lot_size not found.",
        }

    result = place_market_order(
        instrument_key=instrument_key,
        quantity=int(lot_size),
    )

    result["selected_instrument"] = {
        "instrument_key": instrument_key,
        "trading_symbol": trading_symbol,
        "lot_size": lot_size,
    }

    return result