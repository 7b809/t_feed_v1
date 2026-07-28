from fastapi import APIRouter, Depends

from app.config import settings
from app.database import (
    token_state,
    get_token_status,
)
from app.dependencies import get_access_token

router = APIRouter()


@router.get("/")
async def root():
    """
    Application overview.
    """

    candle_intervals = [
        int(x.strip()) for x in settings.CANDLE_INTERVALS.split(",") if x.strip()
    ]

    return {
        "status": "running",
        "app_mode": settings.UPSTOX_MODE,
        "strike_range": (f"{settings.STRIKE_FROM}" f" - " f"{settings.STRIKE_TO}"),
        "candle_intervals": candle_intervals,
        "daily_refresh_time": settings.DAILY_REFRESH_TIME,
        "websocket_connect_time": settings.WEBSOCKET_CONNECT_TIME,
        "market_open_time": settings.MARKET_OPEN_TIME,
        "market_close_time": settings.MARKET_CLOSE_TIME,
        # New APIs
        "feeds_api": "/api/feeds",
        "feed_status_api": "/api/feeds/websocket/status",
        # Custom websocket
        "websocket_endpoint": "/ws/feed",
        # Supported intervals
        "supported_intervals": {
            "0": "Live Tick Feed",
            "1": "1 Minute Candle",
            "3": "3 Minute Candle",
            "5": "5 Minute Candle",
        },
    }


@router.get("/health")
async def health():
    """
    Health endpoint.
    """

    return {
        "status": "healthy",
    }


@router.get("/token")
async def read_token(
    token: str = Depends(get_access_token),
):
    """
    Returns current in-memory token details.
    """

    return {
        "access_token": token,
        "updated_at": token_state.updated_at,
    }


@router.get("/token/status")
async def token_status():
    """
    Returns token runtime information
    without exposing token value.
    """

    return get_token_status()


@router.get("/websocket")
async def websocket_info():
    """
    WebSocket documentation endpoint.
    """

    return {
        "endpoint": "/ws/feed",
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
        "examples": [
            {
                "instrument_key": "NSE_INDEX|Nifty 50",
                "interval": 0,
            },
            {
                "instrument_key": "NSE_FO|63935",
                "interval": 0,
            },
            {
                "instrument_key": "NSE_FO|63935",
                "interval": 1,
            },
            {
                "strike": 23400,
                "type": "CE",
                "interval": 0,
            },
            {
                "strike": 23400,
                "type": "PE",
                "interval": 1,
            },
        ],
    }
