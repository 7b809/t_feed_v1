"""
place_gtt_order.py

Places ONE MULTIPLE-leg GTT order (ENTRY + TARGET + STOPLOSS) using live LTP
as the entry. Uses OrderApiV3 (place_gtt_order).

Behaviour
---------
* Default product   : "I" (intraday). Use "D" for delivery GTTs.
* Default quantity  : 1 lot  (resolved via shared lot_size_resolver:
                            master → symbol map → exchange default).
* Entry             : live LTP (immediate market entry).
* SL / Target       : offset from LTP by default (`is_offset=True`),
                      otherwise treated as absolute prices.
* All failures are returned as a structured payload (no raw exceptions),
  tagged with the exact step that failed:
    VALIDATION, API_CLIENT_INIT, LOT_SIZE_RESOLUTION, LTP_FETCH,
    PRICE_RESOLUTION, REQUEST_BUILD, ORDER_PLACEMENT, PERSISTENCE

Return value on failure
-----------------------
{
    "status": "FAILED",
    "step":   "LTP_FETCH",
    "error":  {
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
# Re-exports SYMBOL_LOT_SIZE_MAP / EXCHANGE_DEFAULT_LOT_MAP for backwards compat.
from sandbox_services.lot_size_resolver import (
    EXCHANGE_DEFAULT_LOT_MAP,   # noqa: F401  (re-exported)
    SYMBOL_LOT_SIZE_MAP,        # noqa: F401  (re-exported)
    resolve_lot_size,
)

logger = get_logger(__file__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VALID_TX_TYPES = {"BUY", "SELL"}
VALID_PRODUCTS = {"D", "I"}

# Step labels used for reporting
STEP_VALIDATION    = "VALIDATION"
STEP_API_CLIENT    = "API_CLIENT_INIT"
STEP_LOT_SIZE      = "LOT_SIZE_RESOLUTION"
STEP_LTP           = "LTP_FETCH"
STEP_PRICE         = "PRICE_RESOLUTION"
STEP_REQUEST       = "REQUEST_BUILD"
STEP_ORDER         = "ORDER_PLACEMENT"
STEP_PERSIST       = "PERSISTENCE"


# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------
def _normalize_instrument_token(instrument_token: str) -> str:
    """Decodes URL-encoded tokens (e.g., 'NSE_FO%7C64473' -> 'NSE_FO|64473')."""
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

        resp_dict = (
            response.to_dict() if hasattr(response, "to_dict") else dict(response)
        )
        data_map = resp_dict.get("data", {})

        quote_entry = data_map.get(instrument_token)
        if not quote_entry and data_map:
            quote_entry = next(iter(data_map.values()))

        if not quote_entry or "last_price" not in quote_entry:
            raise ValueError(
                f"LTP data missing in market quote response for "
                f"{instrument_token}: {resp_dict}"
            )

        ltp = float(quote_entry["last_price"])
        logger.info(f"Fetched live LTP for {instrument_token}: {ltp}")
        return ltp

    except ApiException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error fetching LTP for {instrument_token}: {e}")
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
    request_payload: Dict[str, Any],
    response_payload: Dict[str, Any],
    status: str = "SUCCESS",
) -> None:
    now = datetime.now(timezone.utc)
    date_id = now.strftime("%Y-%m-%d")

    order_entry = {
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
                "$push": {"orders": order_entry},
                "$inc": {"total_orders": 1},
            },
            upsert=True,
        )
        logger.info(
            f"Persisted GTT order entry into ORDERS_COLLECTION doc '{date_id}'."
        )
    except Exception as db_err:
        logger.exception(
            f"Failed to persist order to MongoDB doc '{date_id}': {db_err}"
        )
        raise


def _safe_persist(
    request_payload: Dict[str, Any],
    response_payload: Dict[str, Any],
    status: str,
) -> None:
    """Best-effort persistence — never raises."""
    try:
        _persist_order_to_daily_doc(request_payload, response_payload, status)
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
                        "error_code": first.get("error_code")
                        or first.get("errorCode"),
                        "message": first.get("message"),
                        "property_path": first.get("property_path")
                        or first.get("propertyPath"),
                        "invalid_value": first.get("invalid_value")
                        or first.get("invalidValue"),
                    }
                elif decoded.get("error_code") or decoded.get("errorCode"):
                    parsed = {
                        "error_code": decoded.get("error_code")
                        or decoded.get("errorCode"),
                        "message": decoded.get("message"),
                        "property_path": decoded.get("property_path")
                        or decoded.get("propertyPath"),
                        "invalid_value": decoded.get("invalid_value")
                        or decoded.get("invalidValue"),
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
# Public service
# ---------------------------------------------------------------------------
def place_gtt_order(
    instrument_token: str,
    sl_value: float,
    target_value: float,
    lot: int = 1,
    transaction_type: str = "BUY",
    product: str = "I",
    symbol: Optional[str] = None,
    is_offset: bool = True,
    lot_size_override: Optional[int] = None,
    strict_lot_size: bool = False,
) -> Dict[str, Any]:
    """
    Places an intraday MULTIPLE-leg GTT order using live market price as ENTRY.

    On failure returns a structured FAILED payload with `step` + `error`.
    On success returns {"status": "SUCCESS", "request", "response"}.

    New in this version
    -------------------
    * Lot-size resolution now uses the shared ``sandbox_services.lot_size_resolver``
      (Tier 1 Upstox instruments master → Tier 2 symbol map → Tier 3 exchange
      default). ``lot_size_override`` still wins.
    * ``strict_lot_size`` — when True, raises at LOT_SIZE_RESOLUTION if the
      instruments master has no entry for this instrument. Recommended for
      LIVE trading; leave False in sandbox.
    """

    # ------------------------------------------------------------------
    # STEP 1 — VALIDATION
    # ------------------------------------------------------------------
    try:
        clean_token = _normalize_instrument_token(instrument_token)
        tx_type = transaction_type.upper().strip()
        prod_code = product.upper().strip()

        if not clean_token:
            raise ValueError("instrument_token cannot be empty.")
        if lot < 1:
            raise ValueError(f"Lot count must be >= 1, got {lot}")
        if tx_type not in VALID_TX_TYPES:
            raise ValueError(
                f"Invalid transaction_type '{tx_type}'. Expected 'BUY' or 'SELL'."
            )
        if prod_code not in VALID_PRODUCTS:
            raise ValueError(
                f"Invalid product '{prod_code}'. Expected 'D' or 'I'."
            )

        logger.info(
            f"Initiating GTT order | token={clean_token} | lot={lot} | "
            f"sl_value={sl_value} | target_value={target_value} | "
            f"product={prod_code}"
        )
    except Exception as e:
        logger.exception("GTT validation failed")
        return _error_result(
            STEP_VALIDATION,
            _internal_error("VALIDATION_ERROR", str(e)),
            context={
                "input": {
                    "instrument_token": instrument_token,
                    "sl_value": sl_value,
                    "target_value": target_value,
                    "lot": lot,
                    "transaction_type": transaction_type,
                    "product": product,
                    "is_offset": is_offset,
                }
            },
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
            _parse_api_exception(e)
            if isinstance(e, ApiException)
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
        logger.exception("Lot-size resolution failed")
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
        entry_price = round(live_ltp, 2)
    except Exception as e:
        logger.exception("LTP fetch failed")
        err = (
            _parse_api_exception(e)
            if isinstance(e, ApiException)
            else _internal_error("LTP_FETCH_ERROR", str(e))
        )
        return _error_result(
            STEP_LTP,
            err,
            context={"instrument_token": clean_token, "symbol": symbol},
        )

    # ------------------------------------------------------------------
    # STEP 5 — PRICE RESOLUTION (SL / Target)
    # ------------------------------------------------------------------
    try:
        if is_offset:
            if tx_type == "BUY":
                sl_price = round(entry_price - float(sl_value), 2)
                target_price = round(entry_price + float(target_value), 2)
            else:  # SELL
                sl_price = round(entry_price + float(sl_value), 2)
                target_price = round(entry_price - float(target_value), 2)
        else:
            sl_price = round(float(sl_value), 2)
            target_price = round(float(target_value), 2)

        if sl_price <= 0 or target_price <= 0:
            raise ValueError(
                f"Computed trigger prices must be positive "
                f"(entry={entry_price}, sl={sl_price}, target={target_price})."
            )
    except Exception as e:
        logger.exception("SL/Target resolution failed")
        return _error_result(
            STEP_PRICE,
            _internal_error(
                "PRICE_RESOLUTION_ERROR", str(e),
                path="sl_value / target_value",
                value={"sl_value": sl_value, "target_value": target_value},
            ),
            context={
                "instrument_token": clean_token,
                "live_ltp": live_ltp,
                "is_offset": is_offset,
                "transaction_type": tx_type,
            },
        )

    # ------------------------------------------------------------------
    # STEP 6 — REQUEST BUILD
    # ------------------------------------------------------------------
    try:
        entry_rule = upstox_client.GttRule(
            strategy="ENTRY",
            trigger_type="IMMEDIATE",
            trigger_price=entry_price,
        )
        target_rule = upstox_client.GttRule(
            strategy="TARGET",
            trigger_type="IMMEDIATE",
            trigger_price=target_price,
        )
        stoploss_rule = upstox_client.GttRule(
            strategy="STOPLOSS",
            trigger_type="IMMEDIATE",
            trigger_price=sl_price,
        )

        body = upstox_client.GttPlaceOrderRequest(
            type="MULTIPLE",
            instrument_token=clean_token,
            product=prod_code,
            quantity=total_quantity,
            rules=[entry_rule, target_rule, stoploss_rule],
            transaction_type=tx_type,
        )

        serialized_request = {
            "instrument_token": clean_token,
            "symbol": symbol,
            "lot": lot,
            "lot_size": lot_size,
            "lot_size_source": lot_size_source,
            "quantity": total_quantity,
            "product": prod_code,
            "transaction_type": tx_type,
            "is_offset": is_offset,
            "entry_price": entry_price,
            "sl_price": sl_price,
            "target_price": target_price,
            "live_ltp_at_order": live_ltp,
            "sdk_payload": _serialize_sdk_object(body),
        }
    except Exception as e:
        logger.exception("GTT request build failed")
        return _error_result(
            STEP_REQUEST,
            _internal_error("REQUEST_BUILD_ERROR", str(e)),
            context={
                "instrument_token": clean_token,
                "entry_price": entry_price,
                "sl_price": sl_price,
                "target_price": target_price,
                "quantity": total_quantity,
            },
        )

    base_context: Dict[str, Any] = {
        "instrument_token": clean_token,
        "lot": lot,
        "lot_size": lot_size,
        "lot_size_source": lot_size_source,
        "quantity": total_quantity,
        "product": prod_code,
        "transaction_type": tx_type,
        "entry_price": entry_price,
        "sl_price": sl_price,
        "target_price": target_price,
        "live_ltp": live_ltp,
        "request": serialized_request,
    }

    # ------------------------------------------------------------------
    # STEP 7 — ORDER PLACEMENT
    # ------------------------------------------------------------------
    try:
        api_response = order_api.place_gtt_order(body=body)
        serialized_response = _serialize_sdk_object(api_response)

        logger.info(
            f"GTT order placed successfully for {clean_token} | "
            f"Qty={total_quantity} | Entry={entry_price} | "
            f"SL={sl_price} | Target={target_price} | "
            f"Response={serialized_response}"
        )

    except ApiException as e:
        err = _parse_api_exception(e)
        logger.error(f"ApiException in place_gtt_order for {clean_token}: {err}")
        _safe_persist(serialized_request, err, "API_ERROR")
        return _error_result(STEP_ORDER, err, context=base_context)

    except Exception as e:
        err = _internal_error("ORDER_PLACEMENT_ERROR", str(e))
        logger.exception(f"Unexpected failure in place_gtt_order for {clean_token}")
        _safe_persist(serialized_request, err, "FAILED")
        return _error_result(STEP_ORDER, err, context=base_context)

    # ------------------------------------------------------------------
    # STEP 8 — PERSISTENCE
    # ------------------------------------------------------------------
    try:
        _persist_order_to_daily_doc(
            request_payload=serialized_request,
            response_payload=serialized_response,
            status="SUCCESS",
        )
    except Exception as e:
        # GTT is already placed — persistence failure is non-fatal.
        logger.exception("Persistence failed after successful GTT placement")
        base_context.update(
            {
                "status": "SUCCESS",
                "response": serialized_response,
                "persistence_warning": {
                    "step": STEP_PERSIST,
                    "error": str(e),
                },
            }
        )
        return base_context

    base_context.update({"status": "SUCCESS", "response": serialized_response})
    return base_context


# ---------------------------------------------------------------------------
# Manual smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Default 1 lot (resolved via master), SL = LTP - 30, Target = LTP + 50, Intraday
    result = place_gtt_order("NSE_FO%7C64473", 30, 50)
    print(result)

    # Custom 2 lots for BSE_FO, SL = LTP - 40, Target = LTP + 100
    result_sensex = place_gtt_order(
        "BSE_FO|825123", sl_value=40, target_value=100, lot=2
    )
    print(result_sensex)