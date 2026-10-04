"""
Upstox order placement wrappers.

Ported from the reference `upstox_services/place_order.py`, adapted to
this project's token service (`token_tasks.service.token_service`) and
logger (`core.logger`).

Public functions:

    place_market_order(...)      -> place a single market order
    place_selected_instrument(...) -> place using a selected_instrument dict
    place_budget_instrument(...) -> place using a budget-filter instrument dict
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

import upstox_client
from upstox_client.rest import ApiException

from core.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Token + API client
# ---------------------------------------------------------------------------
def _get_access_token() -> str:
    try:
        from token_tasks.service import token_service  # type: ignore

        token = token_service.get_access_token()
        return str(token or "").strip()
    except Exception:
        logger.exception("Failed to fetch Upstox access token from token_service.")
        return ""


def _get_order_api() -> "upstox_client.OrderApi":
    access_token = _get_access_token()
    if not access_token:
        raise RuntimeError("Upstox access token is not available.")

    configuration = upstox_client.Configuration()
    configuration.access_token = access_token
    api_client = upstox_client.ApiClient(configuration)
    return upstox_client.OrderApi(api_client)


# ---------------------------------------------------------------------------
# SDK response serialization
# ---------------------------------------------------------------------------
def _serialize_upstox_response(value: Any) -> Any:
    """
    Convert Upstox SDK response objects into plain Python data so they can
    be JSON-encoded, stored in MongoDB, and copied safely.
    """
    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, dict):
        return {k: _serialize_upstox_response(v) for k, v in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [_serialize_upstox_response(item) for item in value]

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _serialize_upstox_response(to_dict())
        except Exception:
            logger.exception(
                "Failed to serialize SDK object using to_dict(); falling back."
            )

    if hasattr(value, "__dict__"):
        try:
            return {
                k: _serialize_upstox_response(v)
                for k, v in vars(value).items()
                if not k.startswith("_") or k in ("_data", "_status")
            }
        except Exception:
            logger.exception(
                "Failed to serialize SDK object using __dict__; falling back to str()."
            )

    return str(value)


# ---------------------------------------------------------------------------
# Public: place a single market order
# ---------------------------------------------------------------------------
def place_market_order(
    instrument_key: str,
    quantity: int,
    transaction_type: str = "BUY",
    product: str = "D",
    validity: str = "DAY",
    tag: str = "EMA_ISOLATED",
) -> Dict[str, Any]:
    """
    Place a market order for a given instrument.

    Returns a dict:
        {
            "success": bool,
            "placed_at": iso str,
            "request": {...},
            "response": {...} | None,
            "error": str | None,
            "instrument_key": str,
            "quantity": int,
            "transaction_type": str,
            "product": str,
            "validity": str,
            "tag": str,
        }
    """
    normalized_side = str(transaction_type or "BUY").strip().upper()
    normalized_key = str(instrument_key or "").strip()

    base: Dict[str, Any] = {
        "success": False,
        "placed_at": datetime.now().isoformat(),
        "request": None,
        "response": None,
        "error": None,
        "instrument_key": normalized_key,
        "quantity": int(quantity),
        "transaction_type": normalized_side,
        "product": product,
        "validity": validity,
        "tag": tag,
    }

    if not normalized_key:
        base["error"] = "instrument_key is empty."
        logger.error("place_market_order skipped: instrument_key is empty.")
        return base

    if int(quantity) <= 0:
        base["error"] = f"quantity must be positive (got {quantity})."
        logger.error("place_market_order skipped: invalid quantity=%s", quantity)
        return base

    order_request = {
        "instrument_key": normalized_key,
        "quantity": int(quantity),
        "transaction_type": normalized_side,
        "product": product,
        "validity": validity,
        "tag": tag,
        "order_type": "MARKET",
        "price": 0.0,
        "trigger_price": 0.0,
        "is_amo": False,
    }
    base["request"] = order_request

    try:
        api = _get_order_api()

        body = upstox_client.PlaceOrderRequest(
            quantity=int(quantity),
            product=product,
            validity=validity,
            price=0.0,
            tag=tag,
            instrument_token=normalized_key,
            order_type="MARKET",
            transaction_type=normalized_side,
            disclosed_quantity=0,
            trigger_price=0.0,
            is_amo=False,
        )

        response = api.place_order(body, "2.0")
        serialized = _serialize_upstox_response(response)

        base["success"] = True
        base["response"] = serialized

        logger.info(
            "Order placed | side=%s | key=%s | qty=%s | tag=%s",
            normalized_side, normalized_key, quantity, tag,
        )
        return base

    except ApiException as exc:
        error_message = getattr(exc, "body", None) or str(exc)
        base["error"] = str(error_message)
        logger.exception(
            "Upstox place_order failed | side=%s | key=%s", normalized_side, normalized_key
        )
        return base

    except Exception as exc:
        base["error"] = str(exc)
        logger.exception(
            "Unexpected error placing order | side=%s | key=%s",
            normalized_side, normalized_key,
        )
        return base


# ---------------------------------------------------------------------------
# Convenience: place using a "selected_instrument" dict (isolated)
# ---------------------------------------------------------------------------
def place_selected_instrument(
    selected_instrument: Dict[str, Any],
    transaction_type: str = "BUY",
    product: str = "D",
    validity: str = "DAY",
    tag: str = "EMA_ISOLATED",
    default_quantity: int = 65,
) -> Dict[str, Any]:
    """
    Place an order using a selected_instrument dict, e.g. the
    `instrument` block from the isolated EMA alert payload.
    """
    if not isinstance(selected_instrument, dict):
        return {"success": False, "error": "selected_instrument is invalid."}

    instrument_key = selected_instrument.get("instrument_key")
    lot_size = selected_instrument.get("lot_size")
    trading_symbol = selected_instrument.get("trading_symbol")

    if not instrument_key:
        return {"success": False, "error": "instrument_key not found."}

    quantity = int(lot_size) if lot_size else int(default_quantity)
    if quantity <= 0:
        quantity = int(default_quantity)

    result = place_market_order(
        instrument_key=str(instrument_key),
        quantity=quantity,
        transaction_type=transaction_type,
        product=product,
        validity=validity,
        tag=tag,
    )

    result["selected_instrument"] = {
        "instrument_key": instrument_key,
        "trading_symbol": trading_symbol,
        "lot_size": lot_size,
        "transaction_type": str(transaction_type).upper(),
    }
    return result


# ---------------------------------------------------------------------------
# Convenience: place using a budget-filter instrument dict
# ---------------------------------------------------------------------------
def place_budget_instrument(
    budget_instrument: Dict[str, Any],
    transaction_type: str = "BUY",
    product: str = "D",
    validity: str = "DAY",
    tag: str = "EMA_BUDGET",
    default_quantity: int = 65,
) -> Dict[str, Any]:
    """
    Place an order using a budget-filter instrument dict, e.g. one of the
    `order_suggestion.budget_filter.instruments` entries.
    """
    if not isinstance(budget_instrument, dict):
        return {"success": False, "error": "budget_instrument is invalid."}

    instrument_key = budget_instrument.get("instrument_key")
    lot_size = budget_instrument.get("lot_size")
    trading_symbol = budget_instrument.get("trading_symbol")

    if not instrument_key:
        return {"success": False, "error": "instrument_key not found."}

    quantity = int(lot_size) if lot_size else int(default_quantity)
    if quantity <= 0:
        quantity = int(default_quantity)

    result = place_market_order(
        instrument_key=str(instrument_key),
        quantity=quantity,
        transaction_type=transaction_type,
        product=product,
        validity=validity,
        tag=tag,
    )

    result["budget_instrument"] = {
        "instrument_key": instrument_key,
        "trading_symbol": trading_symbol,
        "lot_size": lot_size,
        "transaction_type": str(transaction_type).upper(),
    }
    return result


__all__ = [
    "place_market_order",
    "place_selected_instrument",
    "place_budget_instrument",
]