"""
place_dual_gtt_service.py

Places TWO MULTIPLE-leg GTT orders at once on the same instrument:
  Order #1 : live-market ENTRY, given SL, given TARGET (no trailing)
  Order #2 : live-market ENTRY, given SL, given TARGET, given TRAILING SL

Exception model
---------------
Group-level steps FAIL FAST (return whole result as FAILED):
    VALIDATION → API_CLIENT_INIT → LOT_SIZE_RESOLUTION → LTP_FETCH

Per-leg steps are ISOLATED (one bad leg doesn't abort the other):
    PRICE_RESOLUTION → REQUEST_BUILD → ORDER_PLACEMENT → PERSISTENCE

Every failure carries:
    - "step" : the exact step that failed
    - "error": { error_code, message, property_path, invalid_value,
                 http_status, raw }   # official Upstox schema

Lot-size resolution
-------------------
Delegated to ``sandbox_services.lot_size_resolver``:
    Tier 1  Upstox instruments master keyed by instrument_key  [authoritative]
    Tier 2  SYMBOL_LOT_SIZE_MAP                                [fallback]
    Tier 3  EXCHANGE_DEFAULT_LOT_MAP                           [last resort]

``lot_size_override`` (caller-supplied) always wins.

Return shape on group-level failure
-----------------------------------
{
    "status": "FAILED",
    "step":   "LTP_FETCH",
    "error":  { ...official schema... },
    "group_id"        : "DUAL-...",
    "instrument_token": "NSE_FO|64473",
    "orders"          : []
}

Return shape on success / partial
---------------------------------
{
    "status": "SUCCESS" | "PARTIAL" | "FAILED",
    "group_id"        : "DUAL-...",
    "instrument_token": "NSE_FO|64473",
    "live_ltp"        : 123.45,
    "lot_size"        : 65,
    "lot_size_source" : "override" | "resolver",
    "orders"          : [
        { "order_index": 0, "status": "SUCCESS",
          "step": "ORDER_PLACEMENT",
          "request": {...}, "response": {...} },
        { "order_index": 1, "status": "FAILED",
          "step": "PRICE_RESOLUTION",
          "request": {...}, "error": {...} }
    ]
}
"""

import json
import urllib.parse
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

# Step labels
STEP_VALIDATION = "VALIDATION"
STEP_API_CLIENT = "API_CLIENT_INIT"
STEP_LOT_SIZE   = "LOT_SIZE_RESOLUTION"
STEP_LTP        = "LTP_FETCH"
STEP_PRICE      = "PRICE_RESOLUTION"
STEP_REQUEST    = "REQUEST_BUILD"
STEP_ORDER      = "ORDER_PLACEMENT"
STEP_PERSIST    = "PERSISTENCE"


# ---------------------------------------------------------------------------
# Basic helpers
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


def _persist_dual_orders_to_daily_doc(
    instrument_token: str,
    group_id: str,
    request_payload: Dict[str, Any],
    response_payload: Dict[str, Any],
    status: str = "SUCCESS",
) -> None:
    now = datetime.now(timezone.utc)
    date_id = now.strftime("%Y-%m-%d")

    order_entry = {
        "timestamp": now,
        "group_id": group_id,
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
        logger.info(
            f"Persisted dual GTT leg into ORDERS_COLLECTION doc '{date_id}' | "
            f"group={group_id}"
        )
    except Exception as db_err:
        logger.exception(
            f"Failed to persist dual GTT leg to MongoDB doc '{date_id}': {db_err}"
        )
        raise


def _safe_persist(
    instrument_token: str,
    group_id: str,
    request_payload: Dict[str, Any],
    response_payload: Dict[str, Any],
    status: str,
) -> None:
    """Best-effort persistence — never raises."""
    try:
        _persist_dual_orders_to_daily_doc(
            instrument_token, group_id, request_payload, response_payload, status
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


def _internal_error(code: str, message: str, path: Any = None, value: Any = None) -> Dict[str, Any]:
    """Generic non-API error payload (validation, build, etc.)."""
    return {
        "error_code": code,
        "message": message,
        "property_path": path,
        "invalid_value": value,
        "http_status": None,
        "raw": message,
    }


def _group_failure(
    step: str,
    error: Dict[str, Any],
    group_id: str,
    instrument_token: str,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "status": "FAILED",
        "step": step,
        "error": error,
        "group_id": group_id,
        "instrument_token": instrument_token,
        "orders": [],
    }
    if context:
        result["context"] = context
    return result


# ---------------------------------------------------------------------------
# Rule builders / price helpers
# ---------------------------------------------------------------------------
def _build_gtt_rule(
    strategy: str,
    trigger_price: float,
    trailing_gap: Optional[float] = None,
) -> upstox_client.GttRule:
    kwargs: Dict[str, Any] = dict(
        strategy=strategy,
        trigger_type="IMMEDIATE",
        trigger_price=round(float(trigger_price), 2),
    )
    if trailing_gap is not None:
        kwargs["trailing_gap"] = float(trailing_gap)
    return upstox_client.GttRule(**kwargs)


def _compute_levels(
    entry_price: float,
    config: Dict[str, Any],
    tx_type: str,
    is_offset: bool,
) -> Dict[str, Optional[float]]:
    sl_val = float(config["sl"])
    target_val = float(config["target"])
    trail_val = config.get("trail_sl")

    if is_offset:
        if tx_type == "BUY":
            sl_price = round(entry_price - sl_val, 2)
            target_price = round(entry_price + target_val, 2)
        else:
            sl_price = round(entry_price + sl_val, 2)
            target_price = round(entry_price - target_val, 2)
    else:
        sl_price = round(sl_val, 2)
        target_price = round(target_val, 2)

    if sl_price <= 0 or target_price <= 0:
        raise ValueError(
            f"Computed trigger prices must be positive "
            f"(entry={entry_price}, sl={sl_price}, target={target_price})."
        )
    return {
        "sl_price": sl_price,
        "target_price": target_price,
        "trail_gap": float(trail_val) if trail_val is not None else None,
    }


# ---------------------------------------------------------------------------
# Public service
# ---------------------------------------------------------------------------
def place_dual_gtt_orders(
    instrument_token: str,
    order_configs: List[Dict[str, Any]],
    lot: int = 1,
    transaction_type: str = "BUY",
    product: str = "I",
    symbol: Optional[str] = None,
    is_offset: bool = True,
    lot_size_override: Optional[int] = None,
    strict_lot_size: bool = False,
) -> Dict[str, Any]:
    """
    Places TWO GTT orders at once for the same instrument.

    Order #1: live-market ENTRY + SL + TARGET (no trailing).
    Order #2: live-market ENTRY + SL + TARGET + TRAILING SL.

    New in this version
    -------------------
    * Lot-size resolution now uses the shared ``sandbox_services.lot_size_resolver``
      (Tier 1 Upstox instruments master → Tier 2 symbol map → Tier 3 exchange
      default). ``lot_size_override`` still wins.
    * ``strict_lot_size`` — when True, raises at LOT_SIZE_RESOLUTION (group-
      level failure) if the instruments master has no entry for this
      instrument. Recommended for LIVE trading; leave False in sandbox.
    * ``lot_size_source`` is exposed on the top-level result and on every
      per-leg ``serialized_request`` for auditability.
    """
    group_id = f"DUAL-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"

    # ------------------------------------------------------------------
    # STEP 1 — VALIDATION
    # ------------------------------------------------------------------
    try:
        if not isinstance(order_configs, list) or len(order_configs) != 2:
            raise ValueError(
                f"order_configs must be a list of exactly 2 dicts, "
                f"got {order_configs!r}."
            )
        for idx, cfg in enumerate(order_configs):
            if not isinstance(cfg, dict) or "sl" not in cfg or "target" not in cfg:
                raise ValueError(
                    f"order_configs[{idx}] must be a dict with 'sl' and 'target'. "
                    f"Got: {cfg!r}"
                )

        clean_token = _normalize_instrument_token(instrument_token)
        tx_type = transaction_type.upper().strip()
        prod_code = product.upper().strip()

        if not clean_token:
            raise ValueError("instrument_token cannot be empty.")
        if lot < 1:
            raise ValueError(f"Default lot count must be >= 1, got {lot}")
        if tx_type not in VALID_TX_TYPES:
            raise ValueError(
                f"Invalid transaction_type '{tx_type}'. Expected 'BUY' or 'SELL'."
            )
        if prod_code not in VALID_PRODUCTS:
            raise ValueError(
                f"Invalid product '{prod_code}'. Expected 'D' or 'I'."
            )

        logger.info(
            f"[{group_id}] Initiating DUAL GTT orders | token={clean_token} | "
            f"default_lot={lot} | product={prod_code} | tx={tx_type} | "
            f"is_offset={is_offset} | configs={order_configs}"
        )
    except Exception as e:
        logger.exception(f"[{group_id}] Validation failed")
        return _group_failure(
            STEP_VALIDATION,
            _internal_error("VALIDATION_ERROR", str(e)),
            group_id,
            instrument_token or "",
            context={
                "input": {
                    "instrument_token": instrument_token,
                    "order_configs": order_configs,
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
        logger.exception(f"[{group_id}] API client init failed")
        err = (
            _parse_api_exception(e)
            if isinstance(e, ApiException)
            else _internal_error("CLIENT_INIT_ERROR", str(e))
        )
        return _group_failure(STEP_API_CLIENT, err, group_id, clean_token)

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
            raise ValueError(f"Resolved lot_size must be >= 1, got {lot_size}")
        logger.info(
            f"[{group_id}] Resolved lot_size={lot_size} "
            f"(source={lot_size_source}) for '{clean_token}'"
        )
    except Exception as e:
        logger.exception(f"[{group_id}] Lot-size resolution failed")
        return _group_failure(
            STEP_LOT_SIZE,
            _internal_error(
                "LOT_SIZE_ERROR", str(e),
                path="symbol / lot_size_override", value=symbol,
            ),
            group_id,
            clean_token,
            context={
                "symbol": symbol,
                "lot": lot,
                "lot_size_override": lot_size_override,
                "strict_lot_size": strict_lot_size,
            },
        )

    # ------------------------------------------------------------------
    # STEP 4 — LIVE LTP (shared by both legs)
    # ------------------------------------------------------------------
    try:
        live_ltp = _fetch_live_ltp(api_client, clean_token)
        entry_price = round(live_ltp, 2)
    except Exception as e:
        logger.exception(f"[{group_id}] LTP fetch failed")
        err = (
            _parse_api_exception(e)
            if isinstance(e, ApiException)
            else _internal_error("LTP_FETCH_ERROR", str(e))
        )
        return _group_failure(
            STEP_LTP, err, group_id, clean_token,
            context={"symbol": symbol},
        )

    # ------------------------------------------------------------------
    # Per-leg loop — each leg isolated
    # ------------------------------------------------------------------
    results: List[Dict[str, Any]] = []

    for idx, cfg in enumerate(order_configs):
        # --- per-leg context used in every failure branch ------------------
        leg_lot = int(cfg.get("lot", lot))
        leg_entry = (
            round(float(cfg["entry"]), 2)
            if (not is_offset and cfg.get("entry") is not None)
            else entry_price
        )
        leg_quantity = int(leg_lot * lot_size)

        # -------------------- STEP 5 — PRICE RESOLUTION -------------------
        try:
            if leg_lot < 1:
                raise ValueError(
                    f"order_configs[{idx}]['lot'] must be >= 1, got {leg_lot}"
                )
            levels = _compute_levels(leg_entry, cfg, tx_type, is_offset=is_offset)
        except Exception as e:
            logger.exception(f"[{group_id}] Price resolution failed for leg #{idx + 1}")
            results.append({
                "order_index": idx,
                "status": "FAILED",
                "step": STEP_PRICE,
                "error": _internal_error(
                    "PRICE_RESOLUTION_ERROR", str(e),
                    path=f"order_configs[{idx}]",
                    value=cfg,
                ),
            })
            continue

        # -------------------- STEP 6 — REQUEST BUILD ----------------------
        try:
            entry_rule = _build_gtt_rule("ENTRY", leg_entry)
            target_rule = _build_gtt_rule("TARGET", levels["target_price"])
            stoploss_rule = _build_gtt_rule(
                "STOPLOSS", levels["sl_price"], trailing_gap=levels["trail_gap"]
            )
            body = upstox_client.GttPlaceOrderRequest(
                type="MULTIPLE",
                instrument_token=clean_token,
                product=prod_code,
                quantity=leg_quantity,
                rules=[entry_rule, target_rule, stoploss_rule],
                transaction_type=tx_type,
            )
            serialized_request = {
                "group_id": group_id,
                "order_index": idx,
                "instrument_token": clean_token,
                "symbol": symbol,
                "lot": leg_lot,
                "lot_size": lot_size,
                "lot_size_source": lot_size_source,
                "quantity": leg_quantity,
                "product": prod_code,
                "transaction_type": tx_type,
                "entry_price": leg_entry,
                "sl_price": levels["sl_price"],
                "target_price": levels["target_price"],
                "trail_gap": levels["trail_gap"],
                "is_offset": is_offset,
                "live_ltp_at_order": live_ltp,
                "sdk_payload": _serialize_sdk_object(body),
            }
        except Exception as e:
            logger.exception(f"[{group_id}] Request build failed for leg #{idx + 1}")
            results.append({
                "order_index": idx,
                "status": "FAILED",
                "step": STEP_REQUEST,
                "error": _internal_error("REQUEST_BUILD_ERROR", str(e)),
                "context": {
                    "leg_entry": leg_entry,
                    "sl_price": levels["sl_price"],
                    "target_price": levels["target_price"],
                    "quantity": leg_quantity,
                },
            })
            continue

        # -------------------- STEP 7 — ORDER PLACEMENT --------------------
        try:
            api_response = order_api.place_gtt_order(body=body)
            serialized_response = _serialize_sdk_object(api_response)

            logger.info(
                f"[{group_id}] Order #{idx + 1} placed | token={clean_token} | "
                f"qty={leg_quantity} | entry={leg_entry} | "
                f"sl={levels['sl_price']} | target={levels['target_price']} | "
                f"trail={levels['trail_gap']} | response={serialized_response}"
            )
        except ApiException as e:
            err = _parse_api_exception(e)
            logger.error(
                f"[{group_id}] ApiException on leg #{idx + 1} for "
                f"{clean_token}: {err}"
            )
            _safe_persist(clean_token, group_id, serialized_request, err, "API_ERROR")
            results.append({
                "order_index": idx,
                "status": "API_ERROR",
                "step": STEP_ORDER,
                "request": serialized_request,
                "error": err,
            })
            continue
        except Exception as e:
            err = _internal_error("ORDER_PLACEMENT_ERROR", str(e))
            logger.exception(
                f"[{group_id}] Unexpected failure on leg #{idx + 1} for "
                f"{clean_token}: {e}"
            )
            _safe_persist(clean_token, group_id, serialized_request, err, "FAILED")
            results.append({
                "order_index": idx,
                "status": "FAILED",
                "step": STEP_ORDER,
                "request": serialized_request,
                "error": err,
            })
            continue

        # -------------------- STEP 8 — PERSISTENCE -----------------------
        leg_result: Dict[str, Any] = {
            "order_index": idx,
            "status": "SUCCESS",
            "step": STEP_ORDER,
            "request": serialized_request,
            "response": serialized_response,
        }
        try:
            _persist_dual_orders_to_daily_doc(
                instrument_token=clean_token,
                group_id=group_id,
                request_payload=serialized_request,
                response_payload=serialized_response,
                status="SUCCESS",
            )
        except Exception as e:
            # Order already placed — persistence failure is non-fatal.
            logger.exception(
                f"[{group_id}] Persistence failed for leg #{idx + 1} (order still placed)"
            )
            leg_result["persistence_warning"] = {
                "step": STEP_PERSIST,
                "error": _internal_error("PERSISTENCE_ERROR", str(e)),
            }
        results.append(leg_result)

    # ------------------------------------------------------------------
    # Aggregate
    # ------------------------------------------------------------------
    success_count = sum(1 for r in results if r["status"] == "SUCCESS")
    if success_count == len(results):
        overall_status = "SUCCESS"
    elif success_count == 0:
        overall_status = "FAILED"
    else:
        overall_status = "PARTIAL"

    return {
        "status": overall_status,
        "group_id": group_id,
        "instrument_token": clean_token,
        "live_ltp": live_ltp,
        "lot_size": lot_size,
        "lot_size_source": lot_size_source,
        "orders": results,
    }


# ---------------------------------------------------------------------------
# Manual smoke-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    result_nifty = place_dual_gtt_orders(
        instrument_token="NSE_FO%7C64473",
        order_configs=[
            {"sl": 30, "target": 50},
            {"sl": 30, "target": 50, "trail_sl": 10},
        ],
        symbol="NIFTY",
    )
    print(result_nifty)

    result_sensex = place_dual_gtt_orders(
        instrument_token="BSE_FO|825123",
        order_configs=[
            {"sl": 40, "target": 100, "lot": 2},
            {"sl": 40, "target": 100, "trail_sl": 15, "lot": 2},
        ],
        symbol="SENSEX",
    )
    print(result_sensex)