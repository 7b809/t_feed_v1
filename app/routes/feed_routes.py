import logging
from fastapi import APIRouter, HTTPException

from app.upstox_services.fetch_options import (
    options_cache,
    get_available_feeds as get_all_available_feeds,
    get_feed_by_instrument_key,
    NIFTY_INDEX_FEED,
    NIFTY_SUPPORTED_INTERVALS,
    OPTION_SUPPORTED_INTERVALS,
)

try:
    from app.services.feed_cache_service import (
        live_feed_cache,
        candle_1m_cache,
        candle_3m_cache,
        candle_5m_cache,
    )
except Exception:
    # Safe fallback until feed_cache_service is available
    live_feed_cache = {}
    candle_1m_cache = {}
    candle_3m_cache = {}
    candle_5m_cache = {}

from app.websocket.websocket_manager import websocket_manager

logger = logging.getLogger("uvicorn")

router = APIRouter(
    prefix="/api/feeds",
    tags=["Market Feeds"],
)


# ------------------------------------------------------------------
# Feed Discovery
# ------------------------------------------------------------------


@router.get(
    "",
    summary="List all available feeds",
)
def get_available_feeds():
    """
    Returns all feeds available for custom websocket subscription.

    Includes:

    - Nifty Index
        - interval=0 only
        - live tick only
        - no EMA
        - no candle aggregation

    - Option Contracts
        - interval=0 live full tick
        - interval=1 1-minute candle
        - interval=3 3-minute candle
        - interval=5 5-minute candle
    """

    feeds = get_all_available_feeds()

    return {
        "status": "success",
        "nearest_expiry": options_cache.get("nearest_expiry"),
        "total_feeds": len(feeds),
        "feeds": feeds,
    }


# ------------------------------------------------------------------
# Feed Details
# ------------------------------------------------------------------


@router.get(
    "/instrument/{instrument_key:path}",
    summary="Get feed details",
)
def get_feed_details(
    instrument_key: str,
):
    """
    Returns metadata for a specific feed.

    Example:

    /api/feeds/instrument/NSE_INDEX|Nifty 50

    /api/feeds/instrument/NSE_FO|63935
    """

    feed = get_feed_by_instrument_key(instrument_key)

    if not feed:
        raise HTTPException(
            status_code=404,
            detail=f"Feed not found: {instrument_key}",
        )

    return {
        "status": "success",
        "data": feed,
    }


# ------------------------------------------------------------------
# Strike Feed Lookup
# ------------------------------------------------------------------


@router.get(
    "/strike/{strike_price}/{option_type}",
    summary="Get feed details by strike and option type",
)
def get_feed_by_strike_and_type(
    strike_price: float,
    option_type: str,
):
    """
    Returns feed metadata using strike + option type.

    Example:

    /api/feeds/strike/23400/CE

    /api/feeds/strike/23400/PE
    """

    data = options_cache.get("data", [])

    option_type = option_type.upper().strip()

    if option_type not in ["CE", "PE"]:
        raise HTTPException(
            status_code=400,
            detail="option_type must be CE or PE.",
        )

    for item in data:
        if (
            item.get("strike_price") is not None
            and float(item.get("strike_price")) == float(strike_price)
            and item.get("instrument_type") == option_type
        ):
            return {
                "status": "success",
                "data": {
                    **item,
                    "supported_intervals": OPTION_SUPPORTED_INTERVALS,
                },
            }

    raise HTTPException(
        status_code=404,
        detail=f"Feed not found for strike={strike_price}, type={option_type}",
    )


# ------------------------------------------------------------------
# Live Feed Cache
# ------------------------------------------------------------------


@router.get(
    "/live",
    summary="Get latest live tick cache",
)
def get_live_cache():
    """
    Returns latest full feed payloads currently stored in memory.

    This includes:

    - Nifty Index full tick feed
    - Option full tick feeds

    This is the latest cached full Upstox feed object,
    not only LTP.
    """

    return {
        "status": "success",
        "total_feeds": len(live_feed_cache),
        "data": live_feed_cache,
    }


@router.get(
    "/live/{instrument_key:path}",
    summary="Get latest live feed by instrument",
)
def get_live_cache_by_instrument(
    instrument_key: str,
):
    """
    Returns latest full feed packet for a single instrument.

    Example:

    /api/feeds/live/NSE_INDEX|Nifty 50

    /api/feeds/live/NSE_FO|63935
    """

    if instrument_key not in live_feed_cache:
        raise HTTPException(
            status_code=404,
            detail=f"No live feed found for {instrument_key}",
        )

    return {
        "status": "success",
        "instrument_key": instrument_key,
        "data": live_feed_cache[instrument_key],
    }


# ------------------------------------------------------------------
# Candle Cache
# ------------------------------------------------------------------


@router.get(
    "/candles/{interval}",
    summary="Get candle cache",
)
def get_candle_cache(
    interval: int,
):
    """
    Returns runtime candle cache.

    Supported candle intervals:

    - 1
    - 3
    - 5

    Note:
    Nifty index is not processed for candles.
    Candle cache is for option instruments only.
    """

    if interval == 1:
        data = candle_1m_cache

    elif interval == 3:
        data = candle_3m_cache

    elif interval == 5:
        data = candle_5m_cache

    else:
        raise HTTPException(
            status_code=400,
            detail="Supported candle intervals are 1, 3, 5.",
        )

    return {
        "status": "success",
        "interval": interval,
        "total_instruments": len(data),
        "data": data,
    }


@router.get(
    "/candles/{interval}/{instrument_key:path}",
    summary="Get latest candle by instrument",
)
def get_candle_by_instrument(
    interval: int,
    instrument_key: str,
):
    """
    Returns latest cached candle for a single option instrument.

    Example:

    /api/feeds/candles/1/NSE_FO|63935

    /api/feeds/candles/3/NSE_FO|63935

    /api/feeds/candles/5/NSE_FO|63935
    """

    if interval == 1:
        data = candle_1m_cache

    elif interval == 3:
        data = candle_3m_cache

    elif interval == 5:
        data = candle_5m_cache

    else:
        raise HTTPException(
            status_code=400,
            detail="Supported candle intervals are 1, 3, 5.",
        )

    if instrument_key not in data:
        raise HTTPException(
            status_code=404,
            detail=f"No candle found for {instrument_key} at interval={interval}",
        )

    return {
        "status": "success",
        "instrument_key": instrument_key,
        "interval": interval,
        "data": data[instrument_key],
    }


# ------------------------------------------------------------------
# WebSocket Status
# ------------------------------------------------------------------


@router.get(
    "/websocket/status",
    summary="WebSocket subscription statistics",
)
def get_websocket_status():
    """
    Returns custom websocket subscription stats.
    """

    return {
        "status": "success",
        "total_clients": websocket_manager.get_total_clients(),
        "subscriptions": websocket_manager.get_stats(),
    }


# ------------------------------------------------------------------
# Supported Intervals
# ------------------------------------------------------------------


@router.get(
    "/intervals",
    summary="Supported websocket intervals",
)
def get_supported_intervals():
    """
    Returns supported interval list.

    Nifty Index:
        interval=0 only

    Options:
        interval=0,1,3,5
    """

    return {
        "status": "success",
        "nifty_index": {
            "instrument_key": NIFTY_INDEX_FEED.get("instrument_key"),
            "supported_intervals": [
                {
                    "value": 0,
                    "description": "Live Tick Feed only",
                }
            ],
        },
        "options": {
            "supported_intervals": [
                {
                    "value": 0,
                    "description": "Live Tick Feed",
                },
                {
                    "value": 1,
                    "description": "1 Minute Candle",
                },
                {
                    "value": 3,
                    "description": "3 Minute Candle",
                },
                {
                    "value": 5,
                    "description": "5 Minute Candle",
                },
            ],
        },
    }


# ------------------------------------------------------------------
# Feed Summary
# ------------------------------------------------------------------


@router.get(
    "/summary",
    summary="Get feed system summary",
)
def get_feed_summary():
    """
    Returns feed system summary.
    """

    option_count = len(options_cache.get("data", []))

    return {
        "status": "success",
        "nearest_expiry": options_cache.get("nearest_expiry"),
        "nifty_index": {
            **NIFTY_INDEX_FEED,
            "supported_intervals": NIFTY_SUPPORTED_INTERVALS,
        },
        "option_feeds_count": option_count,
        "total_available_feeds": option_count + 1,
        "live_cache_count": len(live_feed_cache),
        "candle_cache": {
            "1m": len(candle_1m_cache),
            "3m": len(candle_3m_cache),
            "5m": len(candle_5m_cache),
        },
        "websocket_clients": websocket_manager.get_total_clients(),
    }
