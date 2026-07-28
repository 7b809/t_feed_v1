import logging
from typing import Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Query

from app.config import settings

from app.upstox_services.fetch_options import (
    options_cache,
    get_options_contracts,
    get_available_feeds,
)

from app.services.options_history_service import (
    batch_history_service,
    options_history_cache,
)

from app.services.indicator_service import (
    indicator_service,
    indicator_cache,
)

logger = logging.getLogger("uvicorn")

router = APIRouter(
    prefix="/api/options",
    tags=["Options Data"],
)


# ==========================================================
# Status
# ==========================================================


@router.get(
    "/status",
    summary="Options cache status",
)
def get_options_status():

    return {
        "status": "success",
        "nearest_expiry": options_cache.get("nearest_expiry"),
        "total_contracts": options_cache.get(
            "total_contracts",
            0,
        ),
        "history_cached": len(options_history_cache),
        "indicators_cached": len(indicator_cache),
    }


# ==========================================================
# Refresh
# ==========================================================


@router.get(
    "/refresh",
    summary="Force refresh options cache and recalculate indicators in memory",
)
def refresh_cache(
    save_data: bool = False,
):
    """
    Refresh option contracts,
    historical candles,
    EMA calculations.
    """

    result = get_options_contracts(
        instrument_key=getattr(
            settings,
            "OPTION_INSTRUMENT_KEY",
            "NSE_INDEX|Nifty 50",
        ),
        filter_nearest=True,
        save_data=save_data,
    )

    if not result:

        raise HTTPException(
            status_code=500,
            detail=("Failed to fetch options " "contracts from Upstox."),
        )

    logger.info("Re-running candle processing...")

    history_summary = batch_history_service.process_target_options_history(
        min_strike=float(
            getattr(
                settings,
                "STRIKE_FROM",
                23000,
            )
        ),
        max_strike=float(
            getattr(
                settings,
                "STRIKE_TO",
                25000,
            )
        ),
        save_files=save_data,
    )

    processed_emas = 0

    for (
        instrument_key,
        contract_data,
    ) in options_history_cache.items():

        trading_symbol = contract_data.get(
            "trading_symbol",
            instrument_key,
        )

        candles = (
            contract_data.get("candles")
            or batch_history_service.history_service.candles
        )

        if not candles:
            continue

        indicator_service.process_and_cache_contract_ema(
            trading_symbol=trading_symbol,
            candles=candles,
            ema_short=settings.EMA_SHORT_PERIOD,
            ema_long=settings.EMA_LONG_PERIOD,
        )

        processed_emas += 1

    return {
        "status": "success",
        "message": ("Options cache, history " "and indicators refreshed."),
        "nearest_expiry": options_cache.get("nearest_expiry"),
        "total_contracts": options_cache.get("total_contracts"),
        "history_pipeline_summary": (history_summary),
        "indicators_cached_count": (processed_emas),
    }


# ==========================================================
# Available Feeds
# ==========================================================


@router.get(
    "/feeds",
    summary="Available feeds for websocket subscription",
)
def get_feeds():

    feeds = get_available_feeds()

    return {
        "status": "success",
        "total_feeds": len(feeds),
        "feeds": feeds,
    }


# ==========================================================
# Strikes
# ==========================================================


@router.get(
    "/strikes",
    summary="Get list of all strike prices",
)
def get_all_strikes():

    data = options_cache.get(
        "data",
        [],
    )

    if not data:

        raise HTTPException(
            status_code=404,
            detail=("No options data in cache."),
        )

    strikes = sorted(
        list(
            set(
                item["strike_price"]
                for item in data
                if item.get("strike_price") is not None
            )
        )
    )

    return {
        "status": "success",
        "nearest_expiry": options_cache.get("nearest_expiry"),
        "total_strikes": len(strikes),
        "strikes": strikes,
    }


# ==========================================================
# Single Instrument
# ==========================================================


@router.get(
    "/instrument/{instrument_key:path}",
    summary="Get option contract by instrument key",
)
def get_instrument(
    instrument_key: str,
):

    data = options_cache.get(
        "data",
        [],
    )

    for item in data:

        if item.get("instrument_key") == instrument_key:

            return {
                "status": "success",
                "contract": item,
            }

    raise HTTPException(
        status_code=404,
        detail=(f"Instrument not found: " f"{instrument_key}"),
    )


# ==========================================================
# Strike Details
# ==========================================================


@router.get(
    "/strike/{strike_price}",
    summary="Get both CE and PE contracts",
)
def get_strike_details(
    strike_price: float,
):

    data = options_cache.get(
        "data",
        [],
    )

    if not data:

        raise HTTPException(
            status_code=404,
            detail="No options data in cache.",
        )

    matching_contracts = [
        item
        for item in data
        if (
            item.get("strike_price") is not None
            and float(item["strike_price"]) == float(strike_price)
        )
    ]

    if not matching_contracts:

        raise HTTPException(
            status_code=404,
            detail=(f"No contracts found " f"for strike " f"{strike_price}"),
        )

    def attach_cached_data(
        contract: Optional[Dict[str, Any]],
    ):

        if not contract:
            return None

        item_copy = contract.copy()

        instrument_key = item_copy.get("instrument_key")

        symbol = item_copy.get("trading_symbol")

        if instrument_key and instrument_key in options_history_cache:

            item_copy["history_ema"] = options_history_cache[instrument_key]

        if symbol and symbol in indicator_cache:

            item_copy["indicator_data"] = indicator_cache[symbol]

        return item_copy

    ce_contract = attach_cached_data(
        next(
            (c for c in matching_contracts if c.get("instrument_type") == "CE"),
            None,
        )
    )

    pe_contract = attach_cached_data(
        next(
            (c for c in matching_contracts if c.get("instrument_type") == "PE"),
            None,
        )
    )

    return {
        "status": "success",
        "nearest_expiry": options_cache.get("nearest_expiry"),
        "strike_price": strike_price,
        "CE": ce_contract,
        "PE": pe_contract,
    }


# ==========================================================
# Single Contract
# ==========================================================


@router.get(
    "/contract",
    summary="Get contract by strike and type",
)
def get_single_contract(
    strike_price: float = Query(
        ...,
        description="Strike Price",
    ),
    type: str = Query(
        ...,
        description="CE or PE",
    ),
):

    data = options_cache.get(
        "data",
        [],
    )

    if not data:

        raise HTTPException(
            status_code=404,
            detail=("No options data in cache."),
        )

    instrument_type = type.upper().strip()

    if instrument_type not in [
        "CE",
        "PE",
    ]:

        raise HTTPException(
            status_code=400,
            detail=("Type must be " "CE or PE"),
        )

    for item in data:

        if (
            item.get("strike_price") is not None
            and float(item["strike_price"]) == float(strike_price)
            and item.get("instrument_type") == instrument_type
        ):

            return {
                "status": "success",
                "contract": item,
            }

    raise HTTPException(
        status_code=404,
        detail=(f"Contract not found " f"for {strike_price} " f"{instrument_type}"),
    )


# ==========================================================
# Option Chain
# ==========================================================


@router.get(
    "/chain",
    summary="Get option chain",
)
def get_option_chain():

    data = options_cache.get(
        "data",
        [],
    )

    if not data:

        raise HTTPException(
            status_code=404,
            detail=("No options data in cache."),
        )

    chain_map: Dict[float, Dict[str, Any]] = {}

    for item in data:

        strike_price = item.get("strike_price")

        if strike_price is None:
            continue

        strike_price = float(strike_price)

        if strike_price not in chain_map:

            chain_map[strike_price] = {
                "strike_price": (strike_price),
                "CE": None,
                "PE": None,
            }

        if item.get("instrument_type") == "CE":
            chain_map[strike_price]["CE"] = item

        elif item.get("instrument_type") == "PE":
            chain_map[strike_price]["PE"] = item

    sorted_chain = [chain_map[key] for key in sorted(chain_map.keys())]

    return {
        "status": "success",
        "nearest_expiry": options_cache.get("nearest_expiry"),
        "total_strikes": len(sorted_chain),
        "option_chain": sorted_chain,
    }
