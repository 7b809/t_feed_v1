import asyncio
from typing import Any

from fastapi import APIRouter, Query

from core import config
from services.main_index_ltp_service import (
    get_main_index_option_chain,
    get_nearest_option_instruments,
)
from services.option_service import options_cache

router = APIRouter(
    prefix="/api",
    tags=["Instruments"],
)


# ============================================================
# Value Conversion Helpers
# ============================================================


def safe_float(
    value: Any,
    default: float | None = None,
) -> float | None:
    """
    Safely converts a value to float.

    Returns the provided default value when conversion fails.
    """

    try:
        if value is None:
            return default

        return float(value)

    except (TypeError, ValueError, OverflowError):
        return default


# ============================================================
# Strike Range Helpers
# ============================================================


def is_in_strike_range(
    instrument: dict,
    strike_from: float,
    strike_to: float,
) -> bool:
    """
    Returns True when the instrument strike price is inside
    the configured strike range.

    Both the lower and upper limits are inclusive.
    """

    strike_price = safe_float(
        instrument.get("strike_price"),
    )

    if strike_price is None:
        return False

    return strike_from <= strike_price <= strike_to


# ============================================================
# Loaded Instruments Route
# ============================================================


@router.get("/instruments")
async def get_loaded_instruments(
    range: bool = Query(
        default=True,
        description=(
            "When true, returns only instruments whose strike price is "
            "between STRIKE_FROM and STRIKE_TO. "
            "When false, returns all loaded instruments."
        ),
    ),
):
    """
    Returns option instruments currently loaded in options_cache.

    Examples:

        GET /api/instruments

        GET /api/instruments?range=true

        GET /api/instruments?range=false

    Default behavior:

        range=true
    """

    loaded_instruments = options_cache.get(
        "data",
        [],
    )

    if not isinstance(
        loaded_instruments,
        list,
    ):
        loaded_instruments = []

    strike_from = safe_float(
        getattr(
            config,
            "STRIKE_FROM",
            23000.0,
        ),
        23000.0,
    )

    strike_to = safe_float(
        getattr(
            config,
            "STRIKE_TO",
            25000.0,
        ),
        25000.0,
    )

    if strike_from is None:
        strike_from = 23000.0

    if strike_to is None:
        strike_to = 25000.0

    # Handle accidentally reversed configuration values.
    if strike_from > strike_to:
        strike_from, strike_to = (
            strike_to,
            strike_from,
        )

    total_loaded_instruments = len(
        loaded_instruments,
    )

    if range:
        returned_instruments = [
            instrument
            for instrument in loaded_instruments
            if isinstance(instrument, dict)
            and is_in_strike_range(
                instrument=instrument,
                strike_from=strike_from,
                strike_to=strike_to,
            )
        ]

        filter_mode = "strike_range"

    else:
        returned_instruments = [
            instrument
            for instrument in loaded_instruments
            if isinstance(instrument, dict)
        ]

        filter_mode = "all"

    subscribed_keys = options_cache.get(
        "subscribed_keys",
        [],
    )

    if not isinstance(
        subscribed_keys,
        list,
    ):
        subscribed_keys = []

    instruments_loaded = total_loaded_instruments > 0

    return {
        "success": True,
        "instruments_loaded": instruments_loaded,
        "message": (
            "Loaded instruments returned successfully."
            if instruments_loaded
            else (
                "No instruments are currently loaded. "
                "The instrument startup or refresh process "
                "may not have completed yet."
            )
        ),
        "filter": {
            "range": range,
            "mode": filter_mode,
            "strike_from": strike_from,
            "strike_to": strike_to,
            "range_inclusive": True,
        },
        "nearest_expiry": options_cache.get(
            "nearest_expiry",
        ),
        "total_contracts": options_cache.get(
            "total_contracts",
            total_loaded_instruments,
        ),
        "total_loaded_instruments": (total_loaded_instruments),
        "total_subscribed_instruments": len(
            subscribed_keys,
        ),
        "returned_instruments_count": len(
            returned_instruments,
        ),
        "instruments": returned_instruments,
    }


# ============================================================
# Latest Nearest Range Route
# ============================================================


@router.get("/latest-range")
async def get_latest_range(
    strike_type: str = Query(
        default="CE",
        description=(
            "Option type used to select instruments. " "Supported values are CE and PE."
        ),
    ),
    count: int | None = Query(
        default=None,
        ge=1,
        le=50,
        description=(
            "Number of nearest option strikes to return. "
            "When omitted, MAIN_INDEX_NEAREST_INSTRUMENTS_COUNT "
            "from config is used."
        ),
    ),
    unit: str | None = Query(
        default=None,
        description=(
            "Intraday candle unit. When omitted, "
            "OPENING_RANGE_INTRADAY_UNIT from config is used."
        ),
    ),
    interval: str | None = Query(
        default=None,
        description=(
            "Intraday candle interval. When omitted, "
            "OPENING_RANGE_INTRADAY_INTERVAL from config is used."
        ),
    ),
):
    """
    Returns the nearest CE or PE instruments based on the latest
    main NIFTY intraday candle close.

    Every returned instrument includes its latest intraday candle.

    Processing flow:

        1. Fetch the latest main NIFTY intraday candle.
        2. Read its close price.
        3. Find the nearest loaded strikes.
        4. Filter instruments by CE or PE.
        5. Fetch the latest intraday candle for each instrument.
        6. Return every instrument with its candle.

    Examples:

        GET /api/latest-range?strike_type=CE

        GET /api/latest-range?strike_type=PE

        GET /api/latest-range?strike_type=CE&count=3

        GET /api/latest-range?strike_type=PE&count=5

        GET /api/latest-range?strike_type=CE&unit=minutes&interval=1
    """

    normalized_strike_type = str(strike_type or "").strip().upper()

    if normalized_strike_type not in {
        "CE",
        "PE",
        "CALL",
        "PUT",
        "C",
        "P",
    }:
        return {
            "status": "failed",
            "success": False,
            "message": ("Invalid strike_type. " "Supported values are CE and PE."),
            "main_index": {},
            "option_type": normalized_strike_type,
            "requested_instruments_count": count,
            "nearest_strikes": [],
            "returned_instruments_count": 0,
            "candle_fetch_summary": {
                "success_count": 0,
                "empty_count": 0,
                "failed_count": 0,
            },
            "nearest_expiry": options_cache.get(
                "nearest_expiry",
            ),
            "instruments": [],
            "error": ("Invalid strike_type. Use CE or PE."),
        }

    loaded_instruments = options_cache.get(
        "data",
        [],
    )

    if not isinstance(
        loaded_instruments,
        list,
    ):
        loaded_instruments = []

    if not loaded_instruments:
        return {
            "status": "empty",
            "success": False,
            "message": (
                "No option instruments are currently loaded. "
                "Wait for application startup to complete or "
                "run the instrument refresh process."
            ),
            "main_index": {},
            "option_type": normalized_strike_type,
            "requested_instruments_count": count,
            "nearest_strikes": [],
            "returned_instruments_count": 0,
            "candle_fetch_summary": {
                "success_count": 0,
                "empty_count": 0,
                "failed_count": 0,
            },
            "nearest_expiry": options_cache.get(
                "nearest_expiry",
            ),
            "instruments": [],
            "error": ("options_cache does not contain " "loaded instruments."),
        }

    # The service makes synchronous Upstox API requests.
    # Execute it in a worker thread so the FastAPI event loop
    # remains available for other requests and WebSockets.
    result = await asyncio.to_thread(
        get_nearest_option_instruments,
        option_type=normalized_strike_type,
        count=count,
        unit=unit,
        interval=interval,
    )

    if not isinstance(result, dict):
        return {
            "status": "failed",
            "success": False,
            "message": (
                "Nearest option instrument service returned " "an invalid response."
            ),
            "main_index": {},
            "option_type": normalized_strike_type,
            "requested_instruments_count": count,
            "nearest_strikes": [],
            "returned_instruments_count": 0,
            "candle_fetch_summary": {
                "success_count": 0,
                "empty_count": 0,
                "failed_count": 0,
            },
            "nearest_expiry": options_cache.get(
                "nearest_expiry",
            ),
            "instruments": [],
            "error": (
                "Nearest option instrument service "
                "returned a non-dictionary response."
            ),
        }

    return result


# ============================================================
# Put/Call Option Chain Route
# ============================================================


@router.get("/option-chain")
async def get_option_chain(
    expiry_date: str | None = Query(
        default=None,
        description=(
            "Option chain expiry date in YYYY-MM-DD format. "
            "When omitted, the nearest expiry from the loaded "
            "instrument cache is used."
        ),
    ),
):
    """
    Returns the Upstox put/call option chain for the configured
    main NIFTY index.

    Underlying instrument:

        config.MAIN_NIFTY_SECURITY

    Default expiry:

        options_cache["nearest_expiry"]

    When expiry_date is provided in the request, the provided
    expiry is used instead of the cached nearest expiry.

    Examples:

        GET /api/option-chain

        GET /api/option-chain?expiry_date=2026-09-01
    """

    loaded_instruments = options_cache.get(
        "data",
        [],
    )

    if not isinstance(
        loaded_instruments,
        list,
    ):
        loaded_instruments = []

    normalized_expiry_date = str(expiry_date or "").strip()

    if not normalized_expiry_date:
        normalized_expiry_date = None

    cached_expiry = options_cache.get(
        "nearest_expiry",
    )

    if normalized_expiry_date is None and not cached_expiry and not loaded_instruments:
        return {
            "status": "empty",
            "success": False,
            "message": (
                "No option expiry is currently available. "
                "Wait for instrument loading to complete or "
                "provide expiry_date explicitly."
            ),
            "underlying_instrument_key": getattr(
                config,
                "MAIN_NIFTY_SECURITY",
                "NSE_INDEX|Nifty 50",
            ),
            "expiry_date": None,
            "expiry_source": None,
            "nearest_expiry": None,
            "option_chain_count": 0,
            "option_chain": [],
            "response": None,
            "error": ("No expiry date is available in options_cache."),
        }

    # get_main_index_option_chain performs a synchronous
    # Upstox SDK API request. Execute it in a worker thread
    # to avoid blocking FastAPI and active WebSocket clients.
    result = await asyncio.to_thread(
        get_main_index_option_chain,
        expiry_date=normalized_expiry_date,
    )

    if not isinstance(result, dict):
        return {
            "status": "failed",
            "success": False,
            "message": ("Option chain service returned an " "invalid response."),
            "underlying_instrument_key": getattr(
                config,
                "MAIN_NIFTY_SECURITY",
                "NSE_INDEX|Nifty 50",
            ),
            "expiry_date": normalized_expiry_date,
            "expiry_source": (
                "query_parameter" if normalized_expiry_date else "options_cache"
            ),
            "nearest_expiry": options_cache.get(
                "nearest_expiry",
            ),
            "option_chain_count": 0,
            "option_chain": [],
            "response": None,
            "error": ("Option chain service returned a " "non-dictionary response."),
        }

    return result
