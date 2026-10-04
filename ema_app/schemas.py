"""Pydantic schemas for the EMA app HTTP + WebSocket APIs."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CrossRecord(BaseModel):
    instrument_key: str
    underlying: str = ""
    strike: Optional[float] = None
    option_type: Optional[str] = None      # "CE" | "PE" | None for indexes
    expiry: Optional[str] = None
    trading_symbol: Optional[str] = None

    cross_time: int = Field(..., description="Unix seconds of the cross candle")
    cross_time_iso: str = Field(..., description="Human-readable IST time")
    cross_type: str = Field(..., description="bullish_cross | bearish_cross")

    close: float
    ema_fast: float
    ema_slow: float
    ema_calculation_mode: str = "candle_close"


class EmaStateSnapshot(BaseModel):
    instrument_key: str
    underlying: str = ""
    strike: Optional[float] = None
    option_type: Optional[str] = None
    expiry: Optional[str] = None

    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    prev_diff: Optional[float] = None
    last_candle_time: Optional[int] = None
    last_close: Optional[float] = None
    crosses_today: int = 0


class ServiceStatus(BaseModel):
    enabled: bool
    running: bool
    market_open: bool
    inside_session: bool
    instrument_count: int
    crosses_today: int
    last_cross_at: Optional[str] = None
    started_at: Optional[str] = None
    streamer_attached: bool
    session_date: Optional[str] = None


class CrossListResponse(BaseModel):
    count: int
    crosses: List[CrossRecord]


class InstrumentListResponse(BaseModel):
    count: int
    instruments: List[Dict[str, Any]]


# ---------------------------------------------------------------------------
# WebSocket subscription filters
# ---------------------------------------------------------------------------
class WsFilter(BaseModel):
    """
    A single client-side filter. Matches a cross when every provided field
    matches. Omitted fields act as wildcards.
    """
    instrument_key: Optional[str] = None
    underlying: Optional[str] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None
    expiry: Optional[str] = None


class WsSubscribeMessage(BaseModel):
    action: str = "subscribe"
    filter: Optional[WsFilter] = None
    filters: Optional[List[WsFilter]] = None
    subscribe_all: bool = False


class WsUnsubscribeMessage(BaseModel):
    action: str = "unsubscribe"
    filter: Optional[WsFilter] = None
    unsubscribe_all: bool = False