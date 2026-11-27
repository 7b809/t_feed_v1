"""
place_order_service.py

Places ONE regular Upstox order (MARKET / LIMIT / SL / SL-M) using the official
OrderApiV3 (v3/order/place) endpoint.

Key features
------------
* Step-scoped exception handling — every failure is tagged with the exact stage:
    VALIDATION, API_CLIENT_INIT, LOT_SIZE_RESOLUTION, LTP_FETCH,
    PRICE_RESOLUTION, REQUEST_BUILD, ORDER_PLACEMENT, PERSISTENCE
* Official Upstox error parsing — ApiException.body is decoded into the
  documented schema: {error_code, message, property_path, invalid_value}.
* Lot-size resolution delegated to ``sandbox_services.lot_size_resolver``:
      Tier 1  Upstox instruments master keyed by instrument_key  [authoritative]
      Tier 2  SYMBOL_LOT_SIZE_MAP                                [fallback]
      Tier 3  EXCHANGE_DEFAULT_LOT_MAP                           [last resort]
  ``lot_size_override`` (caller-supplied) always wins.
* Sandbox + timeout aware client.
* Default product = "D" (delivery). Default quantity = 1 lot.
* Live LTP used as the entry reference when ``entry`` is omitted.
* SL triggers auto-promotion to SL / SL-M.
* Slice parameter supported (v3 default True).

Return on failure
-----------------
{
    "status": "FAILED",
    "step":   "LTP_FETCH",
    "error": {
        "error_code"   : "UDAPI100050",
        "message"      : "Invalid token used to access API",
        "property_path": null,
        "invalid_value": null,
        "http_status"  : 401,
        "raw"          : "<full exception string>"
    },
    "context": { ...whatever was built before the failure... }
}
"""

import json
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import upstox_client
from upstox_client.rest import ApiException

from core.config import (
    ORDERS_COLLECTION,
    UPSTOX_HTTP_TIMEOUT_SECONDS,
    UPSTOX_SANDBOX,
)
from core.logger import get_logger
from core.token_service import fetch_access_token

# Shared resolver — Tier 1 (master) → Tier 2 (symbol) → Tier 3 (exchange)
from sandbox_services.lot_size_resolver import (
    EXCHANGE_DEFAULT_LOT_MAP,     # re-exported for backwards compat
    SYMBOL_LOT_SIZE_MAP,          # re-exported for backwards compat
    resolve_lot_size,
)

logger = get_logger(__file__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VALID_ORDER_TYPES = {"MARKET", "LIMIT", "SL", "SL-M"}
VALID_TX_TYPES = {"BUY", "SELL"}
VALID_PRODUCTS = {"D", "I"}
VALID_VALIDITY = {"DAY", "IOC"}

# Step names used for error reporting
STEP_VALIDATION  = "VALIDATION"
STEP_API_CLIENT  = "API_CLIENT_INIT"
STEP_LOT_SIZE    = "LOT_SIZE_RESOLUTION"
STEP_LTP         = "LTP_FETCH"
STEP_PRICE       = "PRICE_RESOLUTION"
STEP_REQUEST     = "REQUEST_BUILD"
STEP_ORDER       = "ORDER_PLACEMENT"
STEP_PERSIST     = "PERSISTENCE"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _normalize_instrument_token(instrument_token: str) -> str:
    return urllib.parse.unquote(instrument_token).strip()


def _get_upstox_api_client() -> upstox_client.ApiClient:
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


def _fetch_live_ltp(api_client: upstox_client.ApiClient, instrument_token: str) -> float:
    try:
        if hasattr(upstox_client, "MarketQuoteV3Api"):
            quote_api = upstox_client.MarketQuoteV3Api(api_client)
            response = quote_api.get_ltp(instrument_key=instrument_token)
        else:
            quote_api = upstox_client.MarketQuoteApi(api_client)
            response = quote_api.ltp(symbol=instrument_token, api_version="2.0")

        resp_dict = response.to_dict() if hasattr(response, "to_dict") else dict(response)
        data_map = resp_dict.get("data", {})

        quote_entry = data_map.get(instrument_token)
        if not quote_entry and data_map:
            quote_entry = next(iter(data_map.values()))

        if not quote_entry or "last_price" not in quote_entry:
            raise ValueError(f"LTP data missing for {instrument_token}: {resp_dict}")

        ltp = float(quote_entry["last_price"])
        logger.info(f"Fetched live LTP for {instrument_token}: {ltp}")
        return ltp

    except ApiException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected LTP fetch error for {instrument_token}: {e}")
        raise


def _serialize_sdk_object(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, dict):
        return {k: _serialize_sdk_object(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize_sdk_object(item) for item in obj]
    return obj


def _persist_order_to_daily_doc(
    instrument_token: str,
    request_payload: Dict[str, Any],
    response_payload: Dict[str, Any],
    status: str = "SUCCESS",
) -> None:
    now = datetime.now(timezone.utc)
    date_id = now.strftime("%Y-%m-%d")

    order_entry = {
        "timestamp": now,
        "source": "place_order_service",
        "instrument_token": instrument_token,
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
                "$push": {"orders": order_entry},
                "$inc": {"total_orders": 1},
            },
            upsert=True,
        )
        logger.info(f"Persisted order into doc '{date_id}' | token={instrument_token}")
    except Exception as db_err:
        logger.exception(f"Failed to persist order to MongoDB doc '{date_id}': {db_err}")
        raise


def _safe_persist(
    instrument_token: str,
    request: Dict[str, Any],
    error: Dict[str, Any],
    status: str,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort persistence for failures — never raises."""
    try:
        _persist_order_to_daily_doc(
            instrument_token=instrument_token,
            request_payload=request,
            response_payload=error,
            status=status,
        )
    except Exception:
        logger.exception("Failure persistence also failed (ignored)")


# ---------------------------------------------------------------------------
# Official Upstox error parsing
# ---------------------------------------------------------------------------
def _parse_api_exception(e: ApiException) -> Dict[str, Any]:
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
# Order-type / price resolution
# ---------------------------------------------------------------------------
def _auto_detect_order_type(
    explicit: Optional[str],
    entry: Optional[float],
    sl: Optional[float],
    sl_market: bool,
) -> str:
    if explicit is not None:
        ot = explicit.upper().strip()
        if ot not in VALID_ORDER_TYPES:
            raise ValueError(
                f"Invalid order_type '{explicit}'. Expected one of {VALID_ORDER_TYPES}."
            )
        return ot
    if sl is not None:
        return "SL-M" if sl_market else "SL"
    if entry is not None:
        return "LIMIT"
    return "MARKET"


def _resolve_price_and_trigger(
    order_type: str,
    entry: Optional[float],
    sl: Optional[float],
    sl_limit: Optional[float],
    is_offset: bool,
    ltp: float,
    tx_type: str,
) -> Dict[str, float]:
    if is_offset:
        price_abs = (
            (ltp - entry) if (entry is not None and tx_type == "BUY")
            else (ltp + entry) if (entry is not None)
            else ltp
        )
        trigger_abs = (
            (ltp + sl) if (sl is not None and tx_type == "BUY")
            else (ltp - sl) if (sl is not None)
            else 0.0
        )
    else:
        price_abs = float(entry) if entry is not None else float(ltp)
        trigger_abs = float(sl) if sl is not None else 0.0

    price_abs = round(float(price_abs), 2)
    trigger_abs = round(float(trigger_abs), 2)

    if order_type == "MARKET":
        return {"price": 0.0, "trigger_price": 0.0}

    if order_type == "LIMIT":
        return {"price": price_abs, "trigger_price": 0.0}

    if trigger_abs <= 0:
        raise ValueError(
            f"{order_type} requires a positive trigger (got {trigger_abs})."
        )

    if order_type == "SL":
        limit = (
            round(float(sl_limit), 2) if sl_limit is not None
            else price_abs if entry is not None
            else trigger_abs
        )
        return {"price": limit, "trigger_price": trigger_abs}

    # SL-M
    return {"price": 0.0, "trigger_price": trigger_abs}


# ---------------------------------------------------------------------------
# Public service
# ---------------------------------------------------------------------------
def place_order(
    instrument_token: str,
    order_type: Optional[str] = None,
    transaction_type: str = "BUY",
    product: str = "D",
    lot: int = 1,
    entry: Optional[float] = None,
    sl: Optional[float] = None,
    sl_market: bool = False,
    sl_limit: Optional[float] = None,
    symbol: Optional[str] = None,
    is_offset: bool = False,
    lot_size_override: Optional[int] = None,
    validity: str = "DAY",
    is_amo: bool = False,
    tag: Optional[str] = None,
    disclosed_quantity: int = 0,
    slice_order: bool = True,
    algo_name: Optional[str] = None,
    strict_lot_size: bool = False,
) -> Dict[str, Any]:
    """
    Places ONE regular order via OrderApiV3 and returns a structured result.

    New in this version
    -------------------
    * ``strict_lot_size`` — when True, raises at the LOT_SIZE_RESOLUTION step
      if the Upstox instruments master has no entry for this instrument. Set
      True for LIVE trading (safety) and False for sandbox/dev.
    * Lot-size resolution now goes through the shared
      ``sandbox_services.lot_size_resolver`` (Tier 1 master → Tier 2 symbol
      → Tier 3 exchange default). ``lot_size_override`` still wins.
    """
    # ------------------------------------------------------------------
    # STEP 1 — VALIDATION
    # ------------------------------------------------------------------
    try:
        clean_token = _normalize_instrument_token(instrument_token)
        tx_type = transaction_type.upper().strip()
        prod_code = product.upper().strip()
        valid_code = validity.upper().strip()

        if not clean_token:
            raise ValueError("instrument_token cannot be empty.")
        if tx_type not in VALID_TX_TYPES:
            raise ValueError(f"Invalid transaction_type '{tx_type}'. Expected BUY/SELL.")
        if prod_code not in VALID_PRODUCTS:
            raise ValueError(f"Invalid product '{prod_code}'. Expected 'D' or 'I'.")
        if valid_code not in VALID_VALIDITY:
            raise ValueError(f"Invalid validity '{valid_code}'. Expected DAY/IOC.")
        if lot < 1:
            raise ValueError(f"lot must be >= 1, got {lot}.")
        if disclosed_quantity < 0:
            raise ValueError(f"disclosed_quantity must be >= 0, got {disclosed_quantity}.")

        resolved_order_type = _auto_detect_order_type(order_type, entry, sl, sl_market)

        logger.info(
            f"Initiating ORDER | token={clean_token} | type={resolved_order_type} | "
            f"tx={tx_type} | product={prod_code} | lot={lot} | entry={entry} | "
            f"sl={sl} | sl_market={sl_market} | is_offset={is_offset}"
        )
    except Exception as e:
        logger.exception("Validation failed")
        return _error_result(
            STEP_VALIDATION,
            _internal_error("VALIDATION_ERROR", str(e)),
            context={"input": {
                "instrument_token": instrument_token,
                "order_type": order_type,
                "transaction_type": transaction_type,
                "product": product,
                "lot": lot,
            }},
        )

    # ------------------------------------------------------------------
    # STEP 2 — API CLIENT
    # ------------------------------------------------------------------
    try:
        api_client = _get_upstox_api_client()
        order_api = upstox_client.OrderApiV3(api_client)
    except Exception as e:
        logger.exception("API client init failed")
        err = (
            _parse_api_exception(e) if isinstance(e, ApiException)
            else _internal_error("CLIENT_INIT_ERROR", str(e))
        )
        return _error_result(STEP_API_CLIENT, err)

    # ------------------------------------------------------------------
    # STEP 3 — LOT SIZE (via shared resolver)
    # ------------------------------------------------------------------
    lot_size_source = "override" if lot_size_override is not None else "resolver"
    try:
        lot_size = (
            int(lot_size_override)
            if lot_size_override is not None
            else resolve_lot_size(clean_token, symbol=symbol, strict=strict_lot_size)
        )
        if lot_size < 1:
            raise ValueError(f"Resolved lot_size must be >= 1, got {lot_size}.")
        total_quantity = int(lot * lot_size)
        logger.info(
            f"Resolved quantity={total_quantity} | "
            f"lot={lot} × lot_size={lot_size} (source={lot_size_source})"
        )
    except Exception as e:
        logger.exception("Lot size resolution failed")
        return _error_result(
            STEP_LOT_SIZE,
            _internal_error(
                "LOT_SIZE_ERROR", str(e),
                path="symbol / lot_size_override", value=symbol,
            ),
            context={
                "instrument_token": clean_token,
                "symbol": symbol,
                "lot": lot,
                "lot_size_override": lot_size_override,
                "strict_lot_size": strict_lot_size,
            },
        )

    # ------------------------------------------------------------------
    # STEP 4 — LIVE LTP
    # ------------------------------------------------------------------
    try:
        live_ltp = _fetch_live_ltp(api_client, clean_token)
    except Exception as e:
        logger.exception("LTP fetch failed")
        err = (
            _parse_api_exception(e) if isinstance(e, ApiException)
            else _internal_error("LTP_FETCH_ERROR", str(e))
        )
        return _error_result(
            STEP_LTP, err,
            context={"instrument_token": clean_token},
        )

    # ------------------------------------------------------------------
    # STEP 5 — PRICE & TRIGGER
    # ------------------------------------------------------------------
    try:
        levels = _resolve_price_and_trigger(
            order_type=resolved_order_type,
            entry=entry,
            sl=sl,
            sl_limit=sl_limit,
            is_offset=is_offset,
            ltp=live_ltp,
            tx_type=tx_type,
        )
        final_price = levels["price"]
        final_trigger = levels["trigger_price"]
        logger.info(
            f"Resolved levels | type={resolved_order_type} | "
            f"price={final_price} | trigger={final_trigger} | ltp={live_ltp}"
        )
    except Exception as e:
        logger.exception("Price resolution failed")
        return _error_result(
            STEP_PRICE,
            _internal_error(
                "PRICE_RESOLUTION_ERROR", str(e),
                path="entry / sl / sl_limit",
            ),
            context={
                "live_ltp": live_ltp,
                "entry": entry, "sl": sl, "sl_limit": sl_limit,
                "is_offset": is_offset,
            },
        )

    # ------------------------------------------------------------------
    # STEP 6 — BUILD REQUEST BODY
    # ------------------------------------------------------------------
    try:
        body = upstox_client.PlaceOrderV3Request(
            quantity=total_quantity,
            product=prod_code,
            validity=valid_code,
            price=final_price,
            tag=tag or "string",
            instrument_token=clean_token,
            order_type=resolved_order_type,
            transaction_type=tx_type,
            disclosed_quantity=int(disclosed_quantity),
            trigger_price=final_trigger,
            is_amo=is_amo,
            slice=slice_order,
        )
    except Exception as e:
        logger.exception("SDK request body construction failed")
        return _error_result(
            STEP_REQUEST,
            _internal_error("REQUEST_BUILD_ERROR", str(e)),
        )

    serialized_request = {
        "instrument_token": clean_token,
        "symbol": symbol,
        "lot": lot,
        "lot_size": lot_size,
        "lot_size_source": lot_size_source,
        "quantity": total_quantity,
        "product": prod_code,
        "validity": valid_code,
        "transaction_type": tx_type,
        "order_type": resolved_order_type,
        "price": final_price,
        "trigger_price": final_trigger,
        "is_offset": is_offset,
        "is_amo": is_amo,
        "tag": tag,
        "disclosed_quantity": disclosed_quantity,
        "slice": slice_order,
        "algo_name": algo_name,
        "live_ltp_at_place": live_ltp,
        "sdk_payload": _serialize_sdk_object(body),
    }

    base_context = {
        "instrument_token": clean_token,
        "live_ltp": live_ltp,
        "lot_size": lot_size,
        "lot_size_source": lot_size_source,
        "quantity": total_quantity,
        "order_type": resolved_order_type,
        "transaction_type": tx_type,
        "product": prod_code,
        "price": final_price,
        "trigger_price": final_trigger,
        "request": serialized_request,
    }

    # ------------------------------------------------------------------
    # STEP 7 — ORDER PLACEMENT
    # ------------------------------------------------------------------
    try:
        api_response = (
            order_api.place_order(body=body, algo_name=algo_name)
            if algo_name
            else order_api.place_order(body=body)
        )
        serialized_response = _serialize_sdk_object(api_response)

        logger.info(
            f"Order placed | token={clean_token} | type={resolved_order_type} | "
            f"qty={total_quantity} | price={final_price} | trigger={final_trigger} | "
            f"response={serialized_response}"
        )

    except ApiException as e:
        err = _parse_api_exception(e)
        logger.error(f"ApiException on order placement: {err}")
        _safe_persist(clean_token, serialized_request, err, "API_ERROR")
        return _error_result(STEP_ORDER, err, context=base_context)

    except Exception as e:
        err = _internal_error("ORDER_PLACEMENT_ERROR", str(e))
        logger.exception("Unexpected order placement failure")
        _safe_persist(clean_token, serialized_request, err, "FAILED")
        return _error_result(STEP_ORDER, err, context=base_context)

    # ------------------------------------------------------------------
    # STEP 8 — PERSISTENCE
    # ------------------------------------------------------------------
    try:
        _persist_order_to_daily_doc(
            instrument_token=clean_token,
            request_payload=serialized_request,
            response_payload=serialized_response,
            status="SUCCESS",
        )
    except Exception as e:
        logger.exception("Persistence failed after successful order")
        base_context.update({
            "status": "SUCCESS",
            "response": serialized_response,
            "persistence_warning": {
                "step": STEP_PERSIST,
                "error": str(e),
            },
        })
        return base_context

    base_context.update({
        "status": "SUCCESS",
        "response": serialized_response,
    })
    return base_context