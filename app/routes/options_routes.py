import logging
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Query

from app.config import settings
from app.upstox_options.fetch_options import options_cache, get_options_contracts
from app.services.options_history_service import batch_history_service, options_history_cache
from app.services.indicator_service import indicator_service, indicator_cache

logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/options", tags=["Options Data"])


@router.get("/refresh", summary="Force refresh options cache and recalculate indicators in memory")
def refresh_cache(save_data: bool = False):
    """
    Manually triggers a fresh fetch from Upstox API, updates the in-memory options_cache,
    and runs the historical candles & indicator pipeline to update project memory cache.
    """
    result = get_options_contracts(
        instrument_key=getattr(settings, "OPTION_INSTRUMENT_KEY", "NSE_INDEX|Nifty 50"),
        filter_nearest=True,
        save_data=save_data,
    )
    
    if not result:
        raise HTTPException(
            status_code=500, detail="Failed to fetch options contracts from Upstox"
        )

    # Automatically re-run historical candle & indicator pipeline
    logger.info("Re-running historical candle processing and EMA calculations...")
    history_summary = batch_history_service.process_target_options_history(
        min_strike=float(getattr(settings, "STRIKE_FROM", 23000)),
        max_strike=float(getattr(settings, "STRIKE_TO", 25000)),
        save_files=save_data,
    )

    processed_emas = 0
    for key, contract_data in options_history_cache.items():
        trading_symbol = contract_data.get("trading_symbol", key)
        candles = contract_data.get("candles") or batch_history_service.history_service.candles

        if candles:
            indicator_service.process_and_cache_contract_ema(
                trading_symbol=trading_symbol,
                candles=candles,
                ema_short=9,
                ema_long=21,
            )
            processed_emas += 1

    return {
        "status": "success",
        "message": "Options cache, history, and indicators successfully refreshed in memory.",
        "nearest_expiry": options_cache.get("nearest_expiry"),
        "total_contracts": options_cache.get("total_contracts"),
        "history_pipeline_summary": history_summary,
        "indicators_cached_count": processed_emas,
    }


@router.get("/strikes", summary="Get list of all available strike prices")
def get_all_strikes():
    """
    Returns a sorted list of all unique strike prices available in the cached expiry.
    """
    data = options_cache.get("data", [])
    if not data:
        raise HTTPException(
            status_code=404, detail="No options data in cache. Try refreshing."
        )

    strikes = sorted(
        list(set(item["strike_price"] for item in data if item.get("strike_price") is not None))
    )

    return {
        "status": "success",
        "nearest_expiry": options_cache.get("nearest_expiry"),
        "total_strikes": len(strikes),
        "strikes": strikes,
    }


@router.get("/strike/{strike_price}", summary="Get both CE and PE contracts for a specific strike")
def get_strike_details(strike_price: float):
    """
    Returns Call (CE) and Put (PE) contracts for a strike price, with cached indicators attached.
    Example: `/api/options/strike/23000`
    """
    data = options_cache.get("data", [])
    if not data:
        raise HTTPException(status_code=404, detail="No options data in cache.")

    matching_contracts = [
        item for item in data
        if item.get("strike_price") is not None and float(item["strike_price"]) == float(strike_price)
    ]

    if not matching_contracts:
        raise HTTPException(
            status_code=404,
            detail=f"No option contracts found for strike_price={strike_price}",
        )

    def attach_cached_indicators(contract: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not contract:
            return None
        item_copy = contract.copy()
        inst_key = item_copy.get("instrument_key")
        symbol = item_copy.get("trading_symbol")

        if inst_key and inst_key in options_history_cache:
            item_copy["history_ema"] = options_history_cache[inst_key]

        if symbol and symbol in indicator_cache:
            item_copy["indicator_data"] = indicator_cache[symbol]

        return item_copy

    ce_contract = attach_cached_indicators(
        next((c for c in matching_contracts if c.get("instrument_type") == "CE"), None)
    )
    pe_contract = attach_cached_indicators(
        next((c for c in matching_contracts if c.get("instrument_type") == "PE"), None)
    )

    return {
        "status": "success",
        "nearest_expiry": options_cache.get("nearest_expiry"),
        "strike_price": strike_price,
        "CE": ce_contract,
        "PE": pe_contract,
    }


@router.get("/contract", summary="Get specific contract by strike price and type (CE/PE)")
def get_single_contract(
    strike_price: float = Query(..., openapi_examples={"default": {"value": 23000.0}}, description="Option Strike Price"),
    type: str = Query(..., openapi_examples={"default": {"value": "CE"}}, description="Instrument Type: CE or PE"),
):
    """
    Fetches a single contract matching the strike_price and instrument type (CE or PE).
    Example: `/api/options/contract?strike_price=23000&type=CE`
    """
    data = options_cache.get("data", [])
    if not data:
        raise HTTPException(status_code=404, detail="No options data in cache.")

    instrument_type = type.upper().strip()
    if instrument_type not in ["CE", "PE"]:
        raise HTTPException(
            status_code=400, detail="Type parameter must be either 'CE' or 'PE'"
        )

    for item in data:
        if (
            item.get("strike_price") is not None
            and float(item["strike_price"]) == float(strike_price)
            and item.get("instrument_type") == instrument_type
        ):
            contract_data = item.copy()
            inst_key = contract_data.get("instrument_key")
            symbol = contract_data.get("trading_symbol")

            if inst_key and inst_key in options_history_cache:
                contract_data["history_ema"] = options_history_cache[inst_key]

            if symbol and symbol in indicator_cache:
                contract_data["indicator_data"] = indicator_cache[symbol]

            return {
                "status": "success",
                "contract": contract_data,
            }

    raise HTTPException(
        status_code=404,
        detail=f"Contract not found for strike_price={strike_price} and type={instrument_type}",
    )


@router.get("/chain", summary="Get full option chain grouped by strike price")
def get_option_chain():
    """
    Groups all cached contracts strike-by-strike into a structured option chain layout.
    """
    data = options_cache.get("data", [])
    if not data:
        raise HTTPException(status_code=404, detail="No options data in cache.")

    chain_map: Dict[float, Dict[str, Any]] = {}

    for item in data:
        sp = item.get("strike_price")
        if sp is None:
            continue

        sp = float(sp)
        if sp not in chain_map:
            chain_map[sp] = {"strike_price": sp, "CE": None, "PE": None}

        if item.get("instrument_type") == "CE":
            chain_map[sp]["CE"] = item
        elif item.get("instrument_type") == "PE":
            chain_map[sp]["PE"] = item

    sorted_chain = [chain_map[sp] for sp in sorted(chain_map.keys())]

    return {
        "status": "success",
        "nearest_expiry": options_cache.get("nearest_expiry"),
        "total_strikes": len(sorted_chain),
        "option_chain": sorted_chain,
    }