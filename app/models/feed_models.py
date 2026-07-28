from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# ==========================================================
# Feed Metadata
# ==========================================================


class FeedDefinition(BaseModel):
    """
    Available feed information.
    """

    instrument_key: str

    trading_symbol: Optional[str] = None

    instrument_type: str

    strike_price: Optional[float] = None

    expiry: Optional[str] = None

    supported_intervals: List[int] = Field(default=[0, 1, 3, 5])


# ==========================================================
# Subscription Request
# ==========================================================


class SubscriptionRequest(BaseModel):
    """
    WebSocket subscription request.

    Examples:

    {
        "instrument_key": "NSE_FO|63935",
        "interval": 0
    }

    OR

    {
        "strike": 23400,
        "type": "CE",
        "interval": 1
    }
    """

    instrument_key: Optional[str] = None

    strike: Optional[float] = None

    type: Optional[str] = None

    interval: int = 0


# ==========================================================
# Live Tick Response
# ==========================================================


class TickResponse(BaseModel):
    """
    Full live feed response.
    """

    feed_type: str = "tick"

    instrument_key: str

    trading_symbol: Optional[str] = None

    current_ts: Optional[str] = None

    received_at: Optional[str] = None

    data: Dict[str, Any]


# ==========================================================
# OHLC Candle
# ==========================================================


class CandleData(BaseModel):

    timestamp: str

    open: float

    high: float

    low: float

    close: float

    volume: Optional[float] = None


# ==========================================================
# Candle Stream Response
# ==========================================================


class CandleResponse(BaseModel):
    """
    Candle websocket response.
    """

    feed_type: str = "candle"

    instrument_key: str

    trading_symbol: Optional[str] = None

    interval: int

    candle: CandleData


# ==========================================================
# Live EMA Response
# ==========================================================


class EMAResponse(BaseModel):
    """
    Real-time EMA response.
    """

    feed_type: str = "ema"

    instrument_key: str

    trading_symbol: Optional[str] = None

    ema9: float

    ema21: float

    close: float

    signal: Optional[str] = None

    timestamp: str


# ==========================================================
# Feed Cache Item
# ==========================================================


class FeedCacheItem(BaseModel):
    """
    Cached feed record.
    """

    instrument_key: str

    timestamp: Optional[str] = None

    received_at: Optional[str] = None

    feed: Dict[str, Any]


# ==========================================================
# WebSocket Status
# ==========================================================


class WebSocketStatus(BaseModel):

    total_clients: int

    subscriptions: Dict[
        str,
        Dict[int, int],
    ]


# ==========================================================
# Interval Definition
# ==========================================================


class IntervalDefinition(BaseModel):

    value: int

    description: str


# ==========================================================
# API Responses
# ==========================================================


class FeedsResponse(BaseModel):

    status: str = "success"

    total_feeds: int

    feeds: List[FeedDefinition]


class LiveFeedResponse(BaseModel):

    status: str = "success"

    instrument_key: str

    data: Dict[str, Any]


class IntervalsResponse(BaseModel):

    status: str = "success"

    intervals: List[IntervalDefinition]
