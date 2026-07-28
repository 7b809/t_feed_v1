import logging
from typing import Dict, Any, List
from urllib.parse import unquote
from fastapi import APIRouter, HTTPException, Query
from app.upstox_options.fetch_options import options_cache
from app.services.options_history_service import options_history_cache
from app.services.indicator_service import indicator_cache

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/ema", tags=["EMA Data"])


@router.get("/summary", summary="Get processing status summary of all option EMAs")
def get_ema_summary():
    """
    Returns summary info of all contracts that have completed historical EMA processing
    and indicator calculations stored in memory.
    """
    data = options_cache.get("data", [])
    total_cached_contracts = len(data)
    processed_count = len(options_history_cache)
    indicators_count = len(indicator_cache)

    processed_symbols = [
        val.get("trading_symbol") for val in options_history_cache.values()
    ]

    return {
        "status": "success",
        "nearest_expiry": options_cache.get("nearest_expiry"),
        "total_options_in_cache": total_cached_contracts,
        "history_processed_count": processed_count,
        "indicators_cached_count": indicators_count,
        "processed_symbols": processed_symbols,
    }


@router.get("/all", summary="Get all processed EMA and candle data")
def get_all_ema_data():
    """
    Returns full historical candles and EMA (9, 21) rows for all processed options (23000 to 25000 range).
    """
    if not options_history_cache:
        raise HTTPException(
            status_code=404,
            detail="EMA history cache is empty or processing is still running.",
        )

    return {
        "status": "success",
        "total_processed": len(options_history_cache),
        "data": options_history_cache,
    }


@router.get("/indicators/all", summary="Get all computed indicators from project cache")
def get_all_indicator_cache():
    """
    Returns calculated indicator series and crossover events for all contracts directly from memory.
    """
    if not indicator_cache:
        raise HTTPException(
            status_code=404,
            detail="Indicator cache is empty or calculation is still running.",
        )

    return {
        "status": "success",
        "total_indicators_cached": len(indicator_cache),
        "data": indicator_cache,
    }


@router.get(
    "/indicators/symbol/{trading_symbol}",
    summary="Get calculated EMA indicators by trading symbol from cache",
)
def get_indicator_by_symbol(trading_symbol: str):
    """
    Fetches computed EMA indicator series and crossovers directly from project memory cache.
    Example: `/api/ema/indicators/symbol/NIFTY24JUL24500CE`
    """
    decoded_symbol = unquote(trading_symbol)

    if decoded_symbol not in indicator_cache:
        raise HTTPException(
            status_code=404,
            detail=f"Indicator data not found in cache for symbol: '{decoded_symbol}'",
        )

    return {
        "status": "success",
        "trading_symbol": decoded_symbol,
        "data": indicator_cache[decoded_symbol],
    }


@router.get(
    "/instrument/{instrument_key:path}",
    summary="Get EMA details for a single contract by instrument key",
)
def get_ema_by_instrument_key(instrument_key: str):
    """
    Fetches historical candles and EMA (9, 21) details using the contract's instrument key.
    Example: `/api/ema/instrument/NSE_FO|43212` or URL-encoded `NSE_FO%7C43212`
    """
    decoded_key = unquote(instrument_key)

    if decoded_key not in options_history_cache:
        raise HTTPException(
            status_code=404,
            detail=f"EMA data not found or failed processing for instrument_key: '{decoded_key}'",
        )

    # Attach computed indicator cache payload if available
    contract_data = options_history_cache[decoded_key].copy()
    symbol = contract_data.get("trading_symbol")

    if symbol and symbol in indicator_cache:
        contract_data["computed_indicators"] = indicator_cache[symbol]

    return {
        "status": "success",
        "instrument_key": decoded_key,
        "data": contract_data,
    }


@router.get(
    "/strike/{strike_price}",
    summary="Get EMA data for a specific strike price (both CE & PE)",
)
def get_ema_by_strike(strike_price: float):
    """
    Fetches processed EMA and candle data for both Call (CE) and Put (PE) options for a target strike price.
    Example: `/api/ema/strike/23000`
    """
    data = options_cache.get("data", [])
    if not data:
        raise HTTPException(status_code=404, detail="Options cache is empty.")

    matching_contracts = [
        item
        for item in data
        if item.get("strike_price") is not None
        and float(item["strike_price"]) == float(strike_price)
    ]

    if not matching_contracts:
        raise HTTPException(
            status_code=404,
            detail=f"No options found for strike price: {strike_price}",
        )

    result: Dict[str, Any] = {"strike_price": strike_price, "CE": None, "PE": None}

    for contract in matching_contracts:
        key = contract.get("instrument_key")
        itype = contract.get("instrument_type")

        # Returns processed item if completed, otherwise skips gracefully
        if key in options_history_cache:
            item_data = options_history_cache[key].copy()
            symbol = item_data.get("trading_symbol")

            if symbol and symbol in indicator_cache:
                item_data["computed_indicators"] = indicator_cache[symbol]

            result[itype] = item_data

    if not result["CE"] and not result["PE"]:
        raise HTTPException(
            status_code=404,
            detail=f"EMA history not found or failed processing for strike {strike_price}.",
        )

    return {"status": "success", "data": result}


@router.get(
    "/contract",
    summary="Get specific contract EMA data by strike price and type (CE/PE)",
)
def get_contract_ema(
    strike_price: float = Query(
        ...,
        openapi_examples={"default": {"value": 23000.0}},
        description="Option Strike Price",
    ),
    type: str = Query(
        ...,
        openapi_examples={"default": {"value": "CE"}},
        description="Instrument Type: CE or PE",
    ),
):
    """
    Fetches historical candles + EMAs for a single contract matching strike price & type.
    Example: `/api/ema/contract?strike_price=23000&type=CE`
    """
    data = options_cache.get("data", [])
    instrument_type = type.upper().strip()

    if instrument_type not in ["CE", "PE"]:
        raise HTTPException(
            status_code=400, detail="Type parameter must be either 'CE' or 'PE'"
        )

    target_contract = next(
        (
            item
            for item in data
            if item.get("strike_price") is not None
            and float(item["strike_price"]) == float(strike_price)
            and item.get("instrument_type") == instrument_type
        ),
        None,
    )

    if not target_contract:
        raise HTTPException(
            status_code=404,
            detail=f"Contract not found for strike {strike_price} {instrument_type}",
        )

    instrument_key = target_contract.get("instrument_key")
    ema_data = options_history_cache.get(instrument_key)

    if not ema_data:
        raise HTTPException(
            status_code=404,
            detail=f"EMA data unavailable or failed to process for {strike_price} {instrument_type}",
        )

    res_data = ema_data.copy()
    symbol = res_data.get("trading_symbol")

    if symbol and symbol in indicator_cache:
        res_data["computed_indicators"] = indicator_cache[symbol]

    return {"status": "success", "data": res_data}
