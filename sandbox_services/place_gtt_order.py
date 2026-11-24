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

logger = get_logger(__file__)

# ---------------------------------------------------------------------------
# Instrument / Exchange Lot Size Mapping
# ---------------------------------------------------------------------------
# Specific underlying symbols take precedence, followed by exchange segment defaults.
SYMBOL_LOT_SIZE_MAP: Dict[str, int] = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "FINNIFTY": 65,
    "MIDCPNIFTY": 120,
    "SENSEX": 20,
    "BANKEX": 30,
}

EXCHANGE_DEFAULT_LOT_MAP: Dict[str, int] = {
    "NSE_FO": 65,   # Default fallback for NSE F&O (e.g., NIFTY)
    "BSE_FO": 20,   # Default fallback for BSE F&O (e.g., SENSEX)
    "NSE_EQ": 1,
    "BSE_EQ": 1,
}


def _normalize_instrument_token(instrument_token: str) -> str:
    """Decodes URL-encoded tokens (e.g., 'NSE_FO%7C64473' -> 'NSE_FO|64473')."""
    return urllib.parse.unquote(instrument_token).strip()


def resolve_lot_size(instrument_token: str, symbol: Optional[str] = None) -> int:
    """
    Resolves the lot multiplier based on explicit symbol name or exchange prefix
    (e.g., NSE_FO -> 65, BSE_FO -> 20).
    """
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
    # Apply configured HTTP timeout if supported by the underlying REST pool
    if hasattr(api_client, "rest_client") and hasattr(api_client.rest_client, "pool_manager"):
        api_client.rest_client.pool_manager.connection_pool_kw["timeout"] = UPSTOX_HTTP_TIMEOUT_SECONDS

    return api_client


def _fetch_live_ltp(api_client: upstox_client.ApiClient, instrument_token: str) -> float:
    """Fetches the live Last Traded Price (LTP) for the given instrument token."""
    try:
        # Upstox SDK V3 MarketQuoteV3Api (falls back to MarketQuoteApi if older SDK)
        if hasattr(upstox_client, "MarketQuoteV3Api"):
            quote_api = upstox_client.MarketQuoteV3Api(api_client)
            response = quote_api.get_ltp(instrument_key=instrument_token)
        else:
            quote_api = upstox_client.MarketQuoteApi(api_client)
            response = quote_api.ltp(symbol=instrument_token, api_version="2.0")

        resp_dict = response.to_dict() if hasattr(response, "to_dict") else dict(response)
        data_map = resp_dict.get("data", {})

        # Upstox returns keys either as 'NSE_FO|64473' or 'NSE_FO:SYMBOL'
        quote_entry = data_map.get(instrument_token)
        if not quote_entry and data_map:
            quote_entry = next(iter(data_map.values()))

        if not quote_entry or "last_price" not in quote_entry:
            raise ValueError(f"LTP data missing in market quote response for {instrument_token}: {resp_dict}")

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
    """Safely converts Upstox SDK model objects into MongoDB-serializable dicts."""
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
    """
    Appends the placed order into a single daily document keyed by current UTC/local date (_id='YYYY-MM-DD').
    Maintains document-level `created_at` and `last_updated_at`.
    """
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
                "$setOnInsert": {
                    "date": date_id,
                    "created_at": now,
                },
                "$set": {
                    "last_updated_at": now,
                },
                "$push": {
                    "orders": order_entry,
                },
                "$inc": {
                    "total_orders": 1,
                },
            },
            upsert=True,
        )
        logger.info(f"Persisted GTT order entry into ORDERS_COLLECTION doc '{date_id}'.")
    except Exception as db_err:
        logger.exception(f"Failed to persist order to MongoDB doc '{date_id}': {db_err}")
        raise


def place_gtt_order(
    instrument_token: str,
    sl_value: float,
    target_value: float,
    lot: int = 1,
    transaction_type: str = "BUY",
    product: str = "I",
    symbol: Optional[str] = None,
    is_offset: bool = True,
) -> Dict[str, Any]:
    """
    Places an intraday MULTIPLE-leg GTT order using live market price as ENTRY.

    Args:
        instrument_token: Instrument key (e.g., 'NSE_FO|64473' or 'NSE_FO%7C64473').
        sl_value: Stop-loss points offset from entry (or absolute price if is_offset=False).
        target_value: Target points offset from entry (or absolute price if is_offset=False).
        lot: Number of lots to trade (defaults to 1).
        transaction_type: 'BUY' or 'SELL' (defaults to 'BUY').
        product: Upstox product code ('I' for Intraday, 'D' for Delivery). Defaults to 'I'.
        symbol: Optional underlying symbol ('NIFTY', 'SENSEX', etc.) for lot size lookup.
        is_offset: If True, computes SL/Target relative to live LTP; if False, uses raw values.

    Returns:
        Dict containing serialized request data and Upstox official response data.
    """
    clean_token = _normalize_instrument_token(instrument_token)
    tx_type = transaction_type.upper().strip()
    prod_code = product.upper().strip()

    if lot < 1:
        raise ValueError(f"Lot count must be >= 1, got {lot}")
    if tx_type not in {"BUY", "SELL"}:
        raise ValueError(f"Invalid transaction_type '{tx_type}'. Expected 'BUY' or 'SELL'.")

    logger.info(
        f"Initiating GTT order | token={clean_token} | lot={lot} | "
        f"sl_value={sl_value} | target_value={target_value} | product={prod_code}"
    )

    api_client = _get_upstox_api_client()
    order_api = upstox_client.OrderApiV3(api_client)

    # 1. Calculate quantity from lot size mapping
    lot_size = resolve_lot_size(clean_token, symbol=symbol)
    total_quantity = int(lot * lot_size)

    # 2. Fetch live LTP for immediate market entry
    live_ltp = _fetch_live_ltp(api_client, clean_token)
    entry_price = round(live_ltp, 2)

    # 3. Compute Stop-Loss and Target trigger prices
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
            f"Computed trigger prices must be positive (entry={entry_price}, sl={sl_price}, target={target_price})."
        )

    # 4. Construct GTT Rules & Request Body
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
        "quantity": total_quantity,
        "product": prod_code,
        "transaction_type": tx_type,
        "live_ltp_at_order": live_ltp,
        "sdk_payload": _serialize_sdk_object(body),
    }

    # 5. Execute GTT Order and Persist Result
    try:
        api_response = order_api.place_gtt_order(body=body)
        serialized_response = _serialize_sdk_object(api_response)

        logger.info(
            f"GTT order placed successfully for {clean_token} | "
            f"Qty={total_quantity} | Entry={entry_price} | SL={sl_price} | Target={target_price} | "
            f"Response={serialized_response}"
        )

        _persist_order_to_daily_doc(
            request_payload=serialized_request,
            response_payload=serialized_response,
            status="SUCCESS",
        )

        return {
            "status": "SUCCESS",
            "request": serialized_request,
            "response": serialized_response,
        }

    except ApiException as e:
        error_payload = {
            "status_code": getattr(e, "status", None),
            "reason": getattr(e, "reason", str(e)),
            "body": getattr(e, "body", None),
        }
        logger.error(f"Upstox ApiException in place_gtt_order for {clean_token}: {error_payload}")

        _persist_order_to_daily_doc(
            request_payload=serialized_request,
            response_payload=error_payload,
            status="API_ERROR",
        )
        raise

    except Exception as e:
        error_payload = {"error": str(e)}
        logger.exception(f"Unexpected failure in place_gtt_order for {clean_token}: {e}")

        _persist_order_to_daily_doc(
            request_payload=serialized_request,
            response_payload=error_payload,
            status="FAILED",
        )
        raise


if __name__ == "__main__":

        # Default 1 lot (65 qty for NSE_FO), SL = LTP - 30, Target = LTP + 50, Intraday ('I')
    result = place_gtt_order("NSE_FO%7C64473", 30, 50)

    # Custom 2 lots for BSE_FO (2 * 20 = 40 qty), SL = LTP - 40, Target = LTP + 100
    result_sensex = place_gtt_order("BSE_FO|825123", sl_value=40, target_value=100, lot=2)        