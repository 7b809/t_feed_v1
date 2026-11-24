from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import upstox_client
from upstox_client.rest import ApiException

from core.config import (
    ORDERS_COLLECTION,
    UPSTOX_HTTP_TIMEOUT_SECONDS,
    UPSTOX_SANDBOX,
)
from core.logger import get_logger
from core.token_service import fetch_access_token

logger = get_logger(__file__)


def _get_upstox_api_client() -> upstox_client.ApiClient:
    """Builds an authenticated Upstox ApiClient configured with sandbox & timeout settings."""
    access_token = fetch_access_token()
    if not access_token:
        raise RuntimeError("Failed to retrieve a valid Upstox access token.")

    configuration = upstox_client.Configuration(sandbox=UPSTOX_SANDBOX)
    configuration.access_token = access_token

    api_client = upstox_client.ApiClient(configuration)
    if hasattr(api_client, "rest_client") and hasattr(api_client.rest_client, "pool_manager"):
        api_client.rest_client.pool_manager.connection_pool_kw["timeout"] = UPSTOX_HTTP_TIMEOUT_SECONDS

    return api_client


def _serialize_sdk_object(obj: Any) -> Any:
    """Safely converts Upstox SDK model objects into MongoDB-serializable dicts."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, dict):
        return {k: _serialize_sdk_object(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize_sdk_object(item) for item in obj]
    return obj


def _fetch_existing_gtt_state(
    order_api: upstox_client.OrderApiV3,
    gtt_order_id: str,
) -> Dict[str, Any]:
    """
    Fetches existing quantity and rules for a GTT order directly from Upstox API,
    with fallback to local MongoDB ORDERS_COLLECTION if needed.
    """
    try:
        details_resp = order_api.get_gtt_order_details(gtt_order_id=gtt_order_id)
        resp_dict = _serialize_sdk_object(details_resp)
        data = resp_dict.get("data") or resp_dict

        # Handle case where Upstox returns a list of 1 order or a direct dict
        if isinstance(data, list) and data:
            data = data[0]

        if data and "rules" in data and "quantity" in data:
            return {
                "quantity": int(data["quantity"]),
                "rules": data["rules"],
                "transaction_type": data.get("transaction_type", "BUY"),
            }
    except Exception as api_err:
        logger.warning(
            f"Live get_gtt_order_details lookup failed for {gtt_order_id}, checking MongoDB fallback: {api_err}"
        )

    # Fallback: lookup stored request_data in ORDERS_COLLECTION
    doc = ORDERS_COLLECTION.find_one(
        {"orders.response_data.data.gtt_order_ids": gtt_order_id},
        {"orders.$": 1},
    )
    if doc and doc.get("orders"):
        sdk_payload = doc["orders"][0].get("request_data", {}).get("sdk_payload", {})
        if "rules" in sdk_payload and "quantity" in sdk_payload:
            return {
                "quantity": int(sdk_payload["quantity"]),
                "rules": sdk_payload["rules"],
                "transaction_type": sdk_payload.get("transaction_type", "BUY"),
            }

    raise ValueError(f"Unable to locate existing rules and quantity for gtt_order_id='{gtt_order_id}'.")


def _persist_modification_to_daily_doc(
    gtt_order_id: str,
    request_payload: Dict[str, Any],
    response_payload: Dict[str, Any],
    status: str = "MODIFIED",
) -> None:
    """Appends the GTT modification audit record into today's document in ORDERS_COLLECTION."""
    now = datetime.now(timezone.utc)
    date_id = now.strftime("%Y-%m-%d")

    mod_entry = {
        "action": "MODIFY_GTT",
        "gtt_order_id": gtt_order_id,
        "timestamp": now,
        "status": status,
        "request_data": request_payload,
        "response_data": response_payload,
    }

    try:
        ORDERS_COLLECTION.update_one(
            {"_id": date_id},
            {
                "$setOnInsert": {
                    "date": date_id,
                    "created_at": now,
                },
                "$set": {
                    "last_updated_at": now,
                },
                "$push": {
                    "orders": mod_entry,
                },
            },
            upsert=True,
        )
        logger.info(f"Logged GTT modification for '{gtt_order_id}' in doc '{date_id}'.")
    except Exception as db_err:
        logger.exception(f"Failed to persist GTT modification to MongoDB doc '{date_id}': {db_err}")
        raise


def modify_gtt_order(
    gtt_order_id: str,
    sl_price: Optional[float] = None,
    target_price: Optional[float] = None,
    is_offset: bool = False,
) -> Dict[str, Any]:
    """
    Modifies only the Stop-Loss, only the Target, or both on an existing MULTIPLE GTT order.
    Automatically preserves existing quantity, ENTRY rule, and any unedited leg.

    Args:
        gtt_order_id: Existing Upstox GTT Order ID (e.g., 'GTT-C25280200137522').
        sl_price: New Stop-Loss trigger price (or point offset from entry if is_offset=True).
        target_price: New Target trigger price (or point offset from entry if is_offset=True).
        is_offset: If False (default), treats sl_price/target_price as exact trigger prices.
                   If True, computes them relative to the existing ENTRY trigger price.

    Returns:
        Dict containing serialized request data and Upstox official response data.
    """
    if sl_price is None and target_price is None:
        raise ValueError("Provide at least one of 'sl_price' or 'target_price' to modify.")

    clean_order_id = gtt_order_id.strip()
    logger.info(
        f"Initiating GTT modify | gtt_order_id={clean_order_id} | "
        f"new_sl={sl_price} | new_target={target_price} | is_offset={is_offset}"
    )

    api_client = _get_upstox_api_client()
    order_api = upstox_client.OrderApiV3(api_client)

    # 1. Fetch existing order state (quantity + current rules)
    existing_state = _fetch_existing_gtt_state(order_api, clean_order_id)
    quantity = existing_state["quantity"]
    existing_rules = existing_state["rules"]
    tx_type = existing_state.get("transaction_type", "BUY").upper()

    # Locate existing ENTRY price in case is_offset=True is requested
    entry_trigger_price: Optional[float] = None
    for rule in existing_rules:
        if str(rule.get("strategy", "")).upper() == "ENTRY":
            entry_trigger_price = float(rule["trigger_price"])
            break

    # 2. Compute final SL and Target prices if offsets were supplied
    final_sl = float(sl_price) if sl_price is not None else None
    final_target = float(target_price) if target_price is not None else None

    if is_offset:
        if entry_trigger_price is None:
            raise ValueError(f"Cannot compute offset: ENTRY rule not found in GTT order '{clean_order_id}'.")
        if final_sl is not None:
            final_sl = (
                round(entry_trigger_price - final_sl, 2)
                if tx_type == "BUY"
                else round(entry_trigger_price + final_sl, 2)
            )
        if final_target is not None:
            final_target = (
                round(entry_trigger_price + final_target, 2)
                if tx_type == "BUY"
                else round(entry_trigger_price - final_target, 2)
            )

    if (final_sl is not None and final_sl <= 0) or (final_target is not None and final_target <= 0):
        raise ValueError(f"Trigger prices must be positive (sl={final_sl}, target={final_target}).")

    # 3. Rebuild full MULTIPLE rules list, mutating only requested legs
    updated_rules: List[upstox_client.GttRule] = []
    for rule in existing_rules:
        strategy = str(rule.get("strategy", "")).upper()
        trigger_type = rule.get("trigger_type", "IMMEDIATE")
        trigger_price = float(rule["trigger_price"])

        if strategy == "STOPLOSS" and final_sl is not None:
            trigger_price = round(final_sl, 2)
        elif strategy == "TARGET" and final_target is not None:
            trigger_price = round(final_target, 2)

        rule_kwargs: Dict[str, Any] = {
            "strategy": strategy,
            "trigger_type": trigger_type,
            "trigger_price": trigger_price,
        }
        # Preserve trailing_gap if the existing rule had one configured
        if rule.get("trailing_gap") is not None:
            rule_kwargs["trailing_gap"] = rule["trailing_gap"]

        updated_rules.append(upstox_client.GttRule(**rule_kwargs))

    body = upstox_client.GttModifyOrderRequest(
        type="MULTIPLE",
        gtt_order_id=clean_order_id,
        rules=updated_rules,
        quantity=quantity,
    )

    serialized_request = {
        "gtt_order_id": clean_order_id,
        "updated_sl_price": final_sl,
        "updated_target_price": final_target,
        "is_offset": is_offset,
        "sdk_payload": _serialize_sdk_object(body),
    }

    # 4. Call Upstox modify_gtt_order & persist result
    try:
        api_response = order_api.modify_gtt_order(body=body)
        serialized_response = _serialize_sdk_object(api_response)

        logger.info(
            f"GTT order '{clean_order_id}' modified successfully | "
            f"SL={final_sl} | Target={final_target} | Response={serialized_response}"
        )

        _persist_modification_to_daily_doc(
            gtt_order_id=clean_order_id,
            request_payload=serialized_request,
            response_payload=serialized_response,
            status="MODIFIED",
        )

        return {
            "status": "MODIFIED",
            "request": serialized_request,
            "response": serialized_response,
        }

    except ApiException as e:
        error_payload = {
            "status_code": getattr(e, "status", None),
            "reason": getattr(e, "reason", str(e)),
            "body": getattr(e, "body", None),
        }
        logger.error(f"Upstox ApiException in modify_gtt_order for '{clean_order_id}': {error_payload}")

        _persist_modification_to_daily_doc(
            gtt_order_id=clean_order_id,
            request_payload=serialized_request,
            response_payload=error_payload,
            status="MODIFY_API_ERROR",
        )
        raise

    except Exception as e:
        error_payload = {"error": str(e)}
        logger.exception(f"Unexpected failure in modify_gtt_order for '{clean_order_id}': {e}")

        _persist_modification_to_daily_doc(
            gtt_order_id=clean_order_id,
            request_payload=serialized_request,
            response_payload=error_payload,
            status="MODIFY_FAILED",
        )
        raise


# if __name__ == "__main__":

#     # 1. Modify ONLY Stop-Loss to exact price 225.0 (Target & Entry stay untouched)
#     modify_gtt_order("GTT-C25280200137522", sl_price=225.0)

#     # 2. Modify ONLY Target to exact price 310.0 (Stop-Loss & Entry stay untouched)
#     modify_gtt_order("GTT-C25280200137522", target_price=310.0)

#     # 3. Modify BOTH Stop-Loss and Target at once
#     modify_gtt_order("GTT-C25280200137522", sl_price=230.0, target_price=320.0)

#     # 4. Modify Stop-Loss using a point offset from the existing Entry price (e.g., Trail SL to 10 pts below entry)
#     modify_gtt_order("GTT-C25280200137522", sl_price=10, is_offset=True)
