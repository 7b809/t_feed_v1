"""
place_dual_gtt_service.py

Places TWO GTT orders (MULTIPLE-leg) at once on the same instrument:
  Order #1: live-market ENTRY, given SL, given TARGET (no trailing)
  Order #2: live-market ENTRY, given SL, given TARGET, given TRAILING SL

Caller passes:
    instrument_token       : e.g. "NSE_FO%7C64473"
    order_configs          : list of exactly 2 dicts:
        {
            "sl"      : <float, required>,       # offset (or absolute if is_offset=False)
            "target"  : <float, required>,       # offset (or absolute if is_offset=False)
            "trail_sl": <float, optional>,       # only used for order #2
            "lot"     : <int, optional>,         # per-order lot override
            "entry"   : <float, optional>,       # absolute entry (only when is_offset=False)
        }
    lot                    : default lots for both orders (default 1)
    transaction_type       : "BUY" | "SELL" (default "BUY")
    product                : "I" intraday | "D" delivery (default "I")
    symbol                 : "NIFTY", "SENSEX", ... used for lot-size lookup
    is_offset              : True  -> sl/target/trail are offsets from live LTP
                             False -> sl/target are absolute prices (entry taken from cfg or live LTP)
    lot_size_override      : explicit lot size, bypassing the symbol/exchange map
"""

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

logger = get_logger(__file__)

# ---------------------------------------------------------------------------
# Instrument / Exchange Lot Size Mapping
# ---------------------------------------------------------------------------
SYMBOL_LOT_SIZE_MAP: Dict[str, int] = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "FINNIFTY": 65,
    "MIDCPNIFTY": 120,
    "SENSEX": 20,
    "BANKEX": 30,
}

EXCHANGE_DEFAULT_LOT_MAP: Dict[str, int] = {
    "NSE_FO": 65,   # NSE F&O default (e.g., NIFTY)
    "BSE_FO": 20,   # BSE F&O default (e.g., SENSEX)
    "NSE_EQ": 1,
    "BSE_EQ": 1,
}


# ---------------------------------------------------------------------------
# Helpers (shared with place_gtt_order.py)
# ---------------------------------------------------------------------------
def _normalize_instrument_token(instrument_token: str) -> str:
    """Decodes URL-encoded tokens (e.g., 'NSE_FO%7C64473' -> 'NSE_FO|64473')."""
    return urllib.parse.unquote(instrument_token).strip()


def resolve_lot_size(instrument_token: str, symbol: Optional[str] = None) -> int:
    """Resolves lot multiplier from symbol (preferred) or exchange prefix."""
    if symbol:
        clean_sym = symbol.upper().strip()
        if clean_sym in SYMBOL_LOT_SIZE_MAP:
            return SYMBOL_LOT_SIZE_MAP[clean_sym]

    normalized_token = _normalize_instrument_token(instrument_token)
    exchange_prefix = normalized_token.split("|")[0].upper() if "|" in normalized_token else ""
    lot_size = EXCHANGE_DEFAULT_LOT_MAP.get(exchange_prefix, 1)
    logger.debug(
        f"Resolved lot_size={lot_size} for instrument='{normalized_token}' "
        f"(exchange='{exchange_prefix}', symbol='{symbol}')"
    )
    return lot_size


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


def _fetch_live_ltp(api_client: upstox_client.ApiClient, instrument_token: str) -> float:
    """Fetches live LTP for the given instrument token."""
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
                f"LTP data missing in market quote response for {instrument_token}: {resp_dict}"
            )

        ltp = float(quote_entry["last_price"])
        logger.info(f"Fetched live LTP for {instrument_token}: {ltp}")
        return ltp

    except ApiException as e:
        logger.error(f"Upstox ApiException while fetching LTP for {instrument_token}: {e}")
        raise
    except Exception as e:
        logger.exception(f"Unexpected error fetching LTP for {instrument_token}: {e}")
        raise


def _serialize_sdk_object(obj: Any) -> Any:
    """Recursively convert Upstox SDK models into Mongo-serializable structures."""
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
    """Appends one leg of the dual-GTT group into today's Mongo daily doc."""
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
            f"Persisted dual GTT leg into ORDERS_COLLECTION doc '{date_id}' | group={group_id}"
        )
    except Exception as db_err:
        logger.exception(
            f"Failed to persist dual GTT leg to MongoDB doc '{date_id}': {db_err}"
        )
        raise


def _build_gtt_rule(
    strategy: str,
    trigger_price: float,
    trailing_gap: Optional[float] = None,
) -> upstox_client.GttRule:
    """
    Builds a GttRule. `trailing_gap` is only attached when provided,
    so order #1 (no-trail) is sent without a `trailing_gap` field.
    """
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
    """
    Resolve absolute SL / TARGET / TRAIL values for one GTT config.

    Config keys:
        sl       : required
        target   : required
        trail_sl : optional (only meaningful for order #2)
    """
    sl_val = float(config["sl"])
    target_val = float(config["target"])
    trail_val = config.get("trail_sl")

    if is_offset:
        if tx_type == "BUY":
            sl_price = round(entry_price - sl_val, 2)
            target_price = round(entry_price + target_val, 2)
        else:  # SELL
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
) -> Dict[str, Any]:
    """
    Places TWO GTT orders at once for the same instrument.

    Order #1: live-market ENTRY, given SL, given TARGET (no trailing).
    Order #2: live-market ENTRY, given SL, given TARGET, given TRAILING SL.

    See module docstring for full parameter description.

    Returns
    -------
    dict : {
        "status"        : "SUCCESS" | "PARTIAL" | "FAILED",
        "group_id"      : shared id for both legs,
        "instrument_token",
        "live_ltp",
        "lot_size",
        "orders"        : [ {order_index, status, request, response|error}, ... ]
    }
    """
    # --- 0. Validate inputs -------------------------------------------------
    if not isinstance(order_configs, list) or len(order_configs) != 2:
        raise ValueError(
            f"order_configs must be a list of exactly 2 dicts, got {order_configs!r}."
        )

    clean_token = _normalize_instrument_token(instrument_token)
    tx_type = transaction_type.upper().strip()
    prod_code = product.upper().strip()

    if lot < 1:
        raise ValueError(f"Default lot count must be >= 1, got {lot}")
    if tx_type not in {"BUY", "SELL"}:
        raise ValueError(f"Invalid transaction_type '{tx_type}'. Expected 'BUY' or 'SELL'.")

    logger.info(
        f"Initiating DUAL GTT orders | token={clean_token} | default_lot={lot} | "
        f"product={prod_code} | tx={tx_type} | is_offset={is_offset} | "
        f"configs={order_configs}"
    )

    # --- 1. Client + resolved defaults -------------------------------------
    api_client = _get_upstox_api_client()
    order_api = upstox_client.OrderApiV3(api_client)

    lot_size = (
        lot_size_override
        if lot_size_override is not None
        else resolve_lot_size(clean_token, symbol=symbol)
    )
    if lot_size < 1:
        raise ValueError(f"Resolved lot_size must be >= 1, got {lot_size}")

    # --- 2. Live LTP (used as the market-based entry for BOTH orders) ------
    live_ltp = _fetch_live_ltp(api_client, clean_token)
    entry_price = round(live_ltp, 2)

    group_id = f"DUAL-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    results: List[Dict[str, Any]] = []

    # --- 3. Place both legs -----------------------------------------------
    for idx, cfg in enumerate(order_configs):
        if not isinstance(cfg, dict) or "sl" not in cfg or "target" not in cfg:
            raise ValueError(
                f"order_configs[{idx}] must be a dict with 'sl' and 'target'. Got: {cfg!r}"
            )

        order_lot = int(cfg.get("lot", lot))
        if order_lot < 1:
            raise ValueError(f"order_configs[{idx}]['lot'] must be >= 1, got {order_lot}")

        total_quantity = int(order_lot * lot_size)

        # Market-based live entry (unless caller forces an absolute entry with is_offset=False)
        if not is_offset and cfg.get("entry") is not None:
            per_order_entry = round(float(cfg["entry"]), 2)
        else:
            per_order_entry = entry_price

        levels = _compute_levels(per_order_entry, cfg, tx_type, is_offset=is_offset)

        # --- 3a. Build rules ------------------------------------------------
        entry_rule = _build_gtt_rule("ENTRY", per_order_entry)
        target_rule = _build_gtt_rule("TARGET", levels["target_price"])
        stoploss_rule = _build_gtt_rule(
            "STOPLOSS",
            levels["sl_price"],
            trailing_gap=levels["trail_gap"],
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
            "group_id": group_id,
            "order_index": idx,
            "instrument_token": clean_token,
            "symbol": symbol,
            "lot": order_lot,
            "lot_size": lot_size,
            "quantity": total_quantity,
            "product": prod_code,
            "transaction_type": tx_type,
            "entry_price": per_order_entry,
            "sl_price": levels["sl_price"],
            "target_price": levels["target_price"],
            "trail_gap": levels["trail_gap"],
            "is_offset": is_offset,
            "sdk_payload": _serialize_sdk_object(body),
        }

        # --- 3b. Execute + persist -----------------------------------------
        try:
            api_response = order_api.place_gtt_order(body=body)
            serialized_response = _serialize_sdk_object(api_response)

            logger.info(
                f"[{group_id}] Order #{idx + 1} placed | token={clean_token} | "
                f"qty={total_quantity} | entry={per_order_entry} | "
                f"sl={levels['sl_price']} | target={levels['target_price']} | "
                f"trail={levels['trail_gap']} | response={serialized_response}"
            )

            _persist_dual_orders_to_daily_doc(
                instrument_token=clean_token,
                group_id=group_id,
                request_payload=serialized_request,
                response_payload=serialized_response,
                status="SUCCESS",
            )
            results.append(
                {
                    "order_index": idx,
                    "status": "SUCCESS",
                    "request": serialized_request,
                    "response": serialized_response,
                }
            )

        except ApiException as e:
            error_payload = {
                "status_code": getattr(e, "status", None),
                "reason": getattr(e, "reason", str(e)),
                "body": getattr(e, "body", None),
            }
            logger.error(
                f"[{group_id}] ApiException on order #{idx + 1} for {clean_token}: {error_payload}"
            )
            _persist_dual_orders_to_daily_doc(
                instrument_token=clean_token,
                group_id=group_id,
                request_payload=serialized_request,
                response_payload=error_payload,
                status="API_ERROR",
            )
            results.append(
                {
                    "order_index": idx,
                    "status": "API_ERROR",
                    "request": serialized_request,
                    "error": error_payload,
                }
            )

        except Exception as e:
            error_payload = {"error": str(e)}
            logger.exception(
                f"[{group_id}] Unexpected failure on order #{idx + 1} for {clean_token}: {e}"
            )
            _persist_dual_orders_to_daily_doc(
                instrument_token=clean_token,
                group_id=group_id,
                request_payload=serialized_request,
                response_payload=error_payload,
                status="FAILED",
            )
            results.append(
                {
                    "order_index": idx,
                    "status": "FAILED",
                    "request": serialized_request,
                    "error": error_payload,
                }
            )

    # --- 4. Aggregate ------------------------------------------------------
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
        "orders": results,
    }


# ---------------------------------------------------------------------------
# Manual smoke-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # NIFTY option (NSE_FO). Default 1 lot = 65 qty.
    # Order #1: SL = LTP - 30, Target = LTP + 50  (no trail)
    # Order #2: SL = LTP - 30, Target = LTP + 50, trailing_gap = 10
    result_nifty = place_dual_gtt_orders(
        instrument_token="NSE_FO%7C64473",
        order_configs=[
            {"sl": 30, "target": 50},
            {"sl": 30, "target": 50, "trail_sl": 10},
        ],
        symbol="NIFTY",
    )
    print(result_nifty)

    # SENSEX option (BSE_FO). Custom 2 lots each = 2 * 20 = 40 qty.
    result_sensex = place_dual_gtt_orders(
        instrument_token="BSE_FO|825123",
        order_configs=[
            {"sl": 40, "target": 100, "lot": 2},
            {"sl": 40, "target": 100, "trail_sl": 15, "lot": 2},
        ],
        symbol="SENSEX",
    )
    print(result_sensex)