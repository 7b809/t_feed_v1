"""
gtt_modify_service.py

Modifies an existing MULTIPLE-leg GTT order on Upstox — changing only the
STOPLOSS, only the TARGET, or both. Preserves quantity, ENTRY rule, and any
untouched leg.

Exception model
---------------
All failures return a structured payload (no raw exceptions leak to the caller):

    {
        "status": "FAILED",
        "step":   "FETCH_STATE" | "MODIFY_PLACEMENT" | ...,
        "error":  {
            "error_code"   : "UDAPI...",
            "message"      : "...",
            "property_path": null,
            "invalid_value": null,
            "http_status"  : 400,
            "raw"          : "<full exception string>"
        },
        "context": { ... whatever was built before the failure ... }
    }

Steps (isolated):
    VALIDATION → API_CLIENT_INIT → FETCH_STATE → OFFSET_RESOLUTION
        → REQUEST_BUILD → MODIFY_PLACEMENT → PERSISTENCE

Return on success
-----------------
{
    "status": "MODIFIED",
    "gtt_order_id": "GTT-...",
    "updated_sl_price": <float|null>,
    "updated_target_price": <float|null>,
    "request": { ...serialized body... },
    "response": { ...serialized Upstox response... }
}
"""

import json
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

# Step labels
STEP_VALIDATION = "VALIDATION"
STEP_API_CLIENT = "API_CLIENT_INIT"
STEP_FETCH      = "FETCH_STATE"
STEP_OFFSET     = "OFFSET_RESOLUTION"
STEP_REQUEST    = "REQUEST_BUILD"
STEP_MODIFY     = "MODIFY_PLACEMENT"
STEP_PERSIST    = "PERSISTENCE"


# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------
def _get_upstox_api_client() -> upstox_client.ApiClient:
    """Builds an authenticated Upstox ApiClient (sandbox + timeout aware)."""
    access_token = fetch_access_token()
    if not access_token:
        raise RuntimeError("Failed to retrieve a valid Upstox access token.")

    configuration = upstox_client.Configuration(sandbox=UPSTOX_SANDBOX)
    configuration.access_token = access_token

    api_client = upstox_client.ApiClient(configuration)
    if hasattr(api_client, "rest_client") and hasattr(api_client.rest_client, "pool_manager"):
        api_client.rest_client.pool_manager.connection_pool_kw["timeout"] = (
            UPSTOX_HTTP_TIMEOUT_SECONDS
        )
    return api_client


def _serialize_sdk_object(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, dict):
        return {k: _serialize_sdk_object(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize_sdk_object(item) for item in obj]
    return obj


def _persist_modification_to_daily_doc(
    gtt_order_id: str,
    request_payload: Dict[str, Any],
    response_payload: Dict[str, Any],
    status: str = "MODIFIED",
) -> None:
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
                "$setOnInsert": {"date": date_id, "created_at": now},
                "$set": {"last_updated_at": now},
                "$push": {"orders": mod_entry},
            },
            upsert=True,
        )
        logger.info(
            f"Logged GTT modification for '{gtt_order_id}' in doc '{date_id}'."
        )
    except Exception as db_err:
        logger.exception(
            f"Failed to persist GTT modification to MongoDB doc '{date_id}': {db_err}"
        )
        raise


def _safe_persist(
    gtt_order_id: str,
    request_payload: Dict[str, Any],
    response_payload: Dict[str, Any],
    status: str,
) -> None:
    """Best-effort persistence – never raises."""
    try:
        _persist_modification_to_daily_doc(
            gtt_order_id, request_payload, response_payload, status
        )
    except Exception:
        logger.exception("Failure persistence also failed (ignored)")


# ---------------------------------------------------------------------------
# Official Upstox error parsing
# ---------------------------------------------------------------------------
def _parse_api_exception(e: ApiException) -> Dict[str, Any]:
    """
    Decodes ApiException.body per official Upstox error schema:
        {
          "status": "error",
          "errors": [
            {
              "error_code": "string",
              "message": "string",
              "property_path": null,
              "invalid_value": null
            }
          ]
        }
    Falls back gracefully if body is missing/malformed.
    """
    http_status = getattr(e, "status", None)
    reason = getattr(e, "reason", str(e))
    raw_body = getattr(e, "body", None)

    parsed: Dict[str, Any] = {}
    if raw_body:
        try:
            decoded = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
            if isinstance(decoded, dict):
                errors = decoded.get("errors") or []
                if errors and isinstance(errors, list):
                    first = errors[0]
                    parsed = {
                        "error_code": first.get("error_code") or first.get("errorCode"),
                        "message": first.get("message"),
                        "property_path": first.get("property_path") or first.get("propertyPath"),
                        "invalid_value": first.get("invalid_value") or first.get("invalidValue"),
                    }
                elif decoded.get("error_code") or decoded.get("errorCode"):
                    parsed = {
                        "error_code": decoded.get("error_code") or decoded.get("errorCode"),
                        "message": decoded.get("message"),
                        "property_path": decoded.get("property_path") or decoded.get("propertyPath"),
                        "invalid_value": decoded.get("invalid_value") or decoded.get("invalidValue"),
                    }
        except Exception:
            pass

    return {
        "error_code": parsed.get("error_code") or "UNKNOWN",
        "message": parsed.get("message") or reason or "Unknown API error",
        "property_path": parsed.get("property_path"),
        "invalid_value": parsed.get("invalid_value"),
        "http_status": http_status,
        "raw": str(e),
    }


def _internal_error(
    code: str,
    message: str,
    path: Any = None,
    value: Any = None,
) -> Dict[str, Any]:
    """Generic non-API error payload."""
    return {
        "error_code": code,
        "message": message,
        "property_path": path,
        "invalid_value": value,
        "http_status": None,
        "raw": message,
    }


def _error_result(
    step: str,
    error: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"status": "FAILED", "step": step, "error": error}
    if context:
        result["context"] = context
    return result


# ---------------------------------------------------------------------------
# Existing-state resolution (live API → Mongo fallback)
# ---------------------------------------------------------------------------
def _fetch_existing_gtt_state(
    order_api: upstox_client.OrderApiV3,
    gtt_order_id: str,
) -> Dict[str, Any]:
    """
    Fetches existing quantity and rules for a GTT order directly from Upstox,
    with fallback to the local ORDERS_COLLECTION.

    Raises:
        ValueError : if neither the live API nor Mongo fallback has the data.
        ApiException : preserved so the caller can enrich with step info.
    """
    # --- Live lookup --------------------------------------------------------
    try:
        details_resp = order_api.get_gtt_order_details(gtt_order_id=gtt_order_id)
        resp_dict = _serialize_sdk_object(details_resp)
        data = resp_dict.get("data") or resp_dict

        if isinstance(data, list) and data:
            data = data[0]

        if data and "rules" in data and "quantity" in data:
            return {
                "quantity": int(data["quantity"]),
                "rules": data["rules"],
                "transaction_type": data.get("transaction_type", "BUY"),
                "source": "live_api",
            }
        logger.warning(
            f"Live get_gtt_order_details returned empty/incomplete data "
            f"for '{gtt_order_id}', falling back to MongoDB."
        )
    except ApiException as api_err:
        logger.warning(
            f"Live get_gtt_order_details raised ApiException for "
            f"'{gtt_order_id}', trying MongoDB fallback: {api_err}"
        )
        # Preserve exception if Mongo fallback also fails
        _last_api_exception: Optional[ApiException] = api_err
    except Exception as api_err:
        logger.warning(
            f"Live get_gtt_order_details unexpected error for "
            f"'{gtt_order_id}', trying MongoDB fallback: {api_err}"
        )
        _last_api_exception = None
    else:
        _last_api_exception = None

    # --- Mongo fallback -----------------------------------------------------
    doc = ORDERS_COLLECTION.find_one(
        {"orders.response_data.data.gtt_order_ids": gtt_order_id},
        {"orders.$": 1},
    )
    if doc and doc.get("orders"):
        sdk_payload = (
            doc["orders"][0].get("request_data", {}).get("sdk_payload", {})
        )
        if "rules" in sdk_payload and "quantity" in sdk_payload:
            return {
                "quantity": int(sdk_payload["quantity"]),
                "rules": sdk_payload["rules"],
                "transaction_type": sdk_payload.get("transaction_type", "BUY"),
                "source": "mongo_fallback",
            }

    # If we got here, both live and Mongo lookups failed
    if _last_api_exception is not None:
        raise _last_api_exception

    raise ValueError(
        f"Unable to locate existing rules and quantity for "
        f"gtt_order_id='{gtt_order_id}'."
    )


# ---------------------------------------------------------------------------
# Public service
# ---------------------------------------------------------------------------
def modify_gtt_order(
    gtt_order_id: str,
    sl_price: Optional[float] = None,
    target_price: Optional[float] = None,
    is_offset: bool = False,
) -> Dict[str, Any]:
    """
    Modifies SL and/or Target on an existing MULTIPLE GTT order.

    Args:
        gtt_order_id : Existing Upstox GTT Order ID ('GTT-...').
        sl_price     : New SL trigger price (or point offset from ENTRY if is_offset=True).
        target_price : New target trigger price (or point offset from ENTRY if is_offset=True).
        is_offset    : When True, sl_price/target_price are interpreted as offsets from
                       the existing ENTRY trigger price. Default False (absolute).
    """
    # ------------------------------------------------------------------
    # STEP 1 – VALIDATION
    # ------------------------------------------------------------------
    try:
        if not gtt_order_id or not str(gtt_order_id).strip():
            raise ValueError("gtt_order_id cannot be empty.")
        if sl_price is None and target_price is None:
            raise ValueError(
                "Provide at least one of 'sl_price' or 'target_price' to modify."
            )
        if sl_price is not None and float(sl_price) <= 0:
            raise ValueError(f"sl_price must be > 0, got {sl_price}.")
        if target_price is not None and float(target_price) <= 0:
            raise ValueError(f"target_price must be > 0, got {target_price}.")

        clean_order_id = str(gtt_order_id).strip()
        logger.info(
            f"Initiating GTT modify | gtt_order_id={clean_order_id} | "
            f"new_sl={sl_price} | new_target={target_price} | is_offset={is_offset}"
        )
    except Exception as e:
        logger.exception("GTT modify validation failed")
        return _error_result(
            STEP_VALIDATION,
            _internal_error("VALIDATION_ERROR", str(e)),
            context={
                "input": {
                    "gtt_order_id": gtt_order_id,
                    "sl_price": sl_price,
                    "target_price": target_price,
                    "is_offset": is_offset,
                }
            },
        )

    # ------------------------------------------------------------------
    # STEP 2 – API CLIENT
    # ------------------------------------------------------------------
    try:
        api_client = _get_upstox_api_client()
        order_api = upstox_client.OrderApiV3(api_client)
    except Exception as e:
        logger.exception("API client init failed")
        err = (
            _parse_api_exception(e)
            if isinstance(e, ApiException)
            else _internal_error("CLIENT_INIT_ERROR", str(e))
        )
        return _error_result(
            STEP_API_CLIENT, err,
            context={"gtt_order_id": clean_order_id},
        )

    # ------------------------------------------------------------------
    # STEP 3 – FETCH EXISTING STATE
    # ------------------------------------------------------------------
    try:
        existing_state = _fetch_existing_gtt_state(order_api, clean_order_id)
        quantity = existing_state["quantity"]
        existing_rules = existing_state["rules"]
        tx_type = existing_state.get("transaction_type", "BUY").upper()
    except Exception as e:
        logger.exception(f"Fetching existing GTT state failed for '{clean_order_id}'")
        err = (
            _parse_api_exception(e)
            if isinstance(e, ApiException)
            else _internal_error("FETCH_STATE_ERROR", str(e), path="gtt_order_id", value=clean_order_id)
        )
        return _error_result(
            STEP_FETCH, err,
            context={"gtt_order_id": clean_order_id},
        )

    # ------------------------------------------------------------------
    # STEP 4 – OFFSET RESOLUTION
    # ------------------------------------------------------------------
    try:
        entry_trigger_price: Optional[float] = None
        for rule in existing_rules:
            if str(rule.get("strategy", "")).upper() == "ENTRY":
                entry_trigger_price = float(rule["trigger_price"])
                break

        final_sl: Optional[float] = (
            float(sl_price) if sl_price is not None else None
        )
        final_target: Optional[float] = (
            float(target_price) if target_price is not None else None
        )

        if is_offset:
            if entry_trigger_price is None:
                raise ValueError(
                    f"Cannot compute offset: ENTRY rule not found in "
                    f"GTT order '{clean_order_id}'."
                )
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

        if (final_sl is not None and final_sl <= 0) or (
            final_target is not None and final_target <= 0
        ):
            raise ValueError(
                f"Trigger prices must be positive "
                f"(sl={final_sl}, target={final_target})."
            )
    except Exception as e:
        logger.exception(
            f"Offset/price resolution failed for '{clean_order_id}'"
        )
        return _error_result(
            STEP_OFFSET,
            _internal_error(
                "OFFSET_RESOLUTION_ERROR", str(e),
                path="sl_price / target_price",
                value={"sl_price": sl_price, "target_price": target_price},
            ),
            context={
                "gtt_order_id": clean_order_id,
                "entry_trigger_price": entry_trigger_price,
                "transaction_type": tx_type,
                "is_offset": is_offset,
                "existing_rules_count": len(existing_rules),
            },
        )

    # ------------------------------------------------------------------
    # STEP 5 – REQUEST BUILD (mutate only requested legs)
    # ------------------------------------------------------------------
    try:
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
            "quantity": quantity,
            "transaction_type": tx_type,
            "entry_trigger_price": entry_trigger_price,
            "existing_rules_before": existing_rules,
            "sdk_payload": _serialize_sdk_object(body),
        }
    except Exception as e:
        logger.exception(
            f"Request build failed for GTT modify '{clean_order_id}'"
        )
        return _error_result(
            STEP_REQUEST,
            _internal_error("REQUEST_BUILD_ERROR", str(e)),
            context={
                "gtt_order_id": clean_order_id,
                "quantity": quantity,
                "final_sl": final_sl,
                "final_target": final_target,
            },
        )

    base_context: Dict[str, Any] = {
        "gtt_order_id": clean_order_id,
        "quantity": quantity,
        "transaction_type": tx_type,
        "entry_trigger_price": entry_trigger_price,
        "updated_sl_price": final_sl,
        "updated_target_price": final_target,
        "request": serialized_request,
    }

    # ------------------------------------------------------------------
    # STEP 6 – MODIFY PLACEMENT
    # ------------------------------------------------------------------
    try:
        api_response = order_api.modify_gtt_order(body=body)
        serialized_response = _serialize_sdk_object(api_response)

        logger.info(
            f"GTT order '{clean_order_id}' modified successfully | "
            f"SL={final_sl} | Target={final_target} | "
            f"Response={serialized_response}"
        )
    except ApiException as e:
        err = _parse_api_exception(e)
        logger.error(
            f"ApiException in modify_gtt_order for '{clean_order_id}': {err}"
        )
        _safe_persist(
            clean_order_id, serialized_request, err, "MODIFY_API_ERROR"
        )
        return _error_result(STEP_MODIFY, err, context=base_context)
    except Exception as e:
        err = _internal_error("MODIFY_PLACEMENT_ERROR", str(e))
        logger.exception(
            f"Unexpected failure in modify_gtt_order for '{clean_order_id}'"
        )
        _safe_persist(
            clean_order_id, serialized_request, err, "MODIFY_FAILED"
        )
        return _error_result(STEP_MODIFY, err, context=base_context)

    # ------------------------------------------------------------------
    # STEP 7 – PERSISTENCE
    # ------------------------------------------------------------------
    try:
        _persist_modification_to_daily_doc(
            gtt_order_id=clean_order_id,
            request_payload=serialized_request,
            response_payload=serialized_response,
            status="MODIFIED",
        )
    except Exception as e:
        # GTT is already modified — persistence failure is non-fatal.
        logger.exception(
            f"Persistence failed after successful GTT modify for '{clean_order_id}'"
        )
        base_context.update(
            {
                "status": "MODIFIED",
                "response": serialized_response,
                "persistence_warning": {
                    "step": STEP_PERSIST,
                    "error": _internal_error("PERSISTENCE_ERROR", str(e)),
                },
            }
        )
        return base_context

    base_context.update(
        {"status": "MODIFIED", "response": serialized_response}
    )
    return base_context


# ---------------------------------------------------------------------------
# Manual smoke-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # 1. Modify only SL to exact price 225.0
    print(modify_gtt_order("GTT-C25280200137522", sl_price=225.0))

    # 2. Modify only Target to exact price 310.0
    print(modify_gtt_order("GTT-C25280200137522", target_price=310.0))

    # 3. Modify both SL and Target
    print(modify_gtt_order(
        "GTT-C25280200137522", sl_price=230.0, target_price=320.0
    ))

    # 4. Modify SL using offset from ENTRY (trail SL to 10 pts below entry for BUY)
    print(modify_gtt_order(
        "GTT-C25280200137522", sl_price=10, is_offset=True
    ))