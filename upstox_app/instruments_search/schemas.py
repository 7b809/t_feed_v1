"""Request / response schemas for the Instrument Search API."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class InstrumentSearchQuery(BaseModel):
    """Query parameters for GET /v2/instruments/search."""

    query: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="Free text search — symbol, name, ISIN, or strike (e.g. RELIANCE, NIFTY 24000 CE, INE002A01018). Max 50 chars.",
    )
    exchanges: Optional[str] = Field(
        default=None,
        description="Comma-separated exchanges: ALL, NSE, BSE, MCX. Default: ALL.",
    )
    segments: Optional[str] = Field(
        default=None,
        description="Comma-separated segments: ALL, EQ, FO, CURR, COMM, INDEX, OPT, FUT. Default: ALL.",
    )
    instrument_types: Optional[str] = Field(
        default=None,
        description="Comma-separated instrument types. Option types: CE, PE. Series: A, X, etc.",
    )
    expiry: Optional[str] = Field(
        default=None,
        description="Comma-separated expiry keywords or yyyy-MM-dd dates. Keywords: current_week, this_week, near_week, weekly, next_week, far_week, current_month, this_month, near_month, monthly, next_month, far_month.",
    )
    atm_offset: Optional[int] = Field(
        default=None,
        description="Distance from ATM strike. 0 = ATM, positive = above, negative = below. Defaults to current week if expiry is omitted.",
    )
    page_number: int = Field(
        default=1,
        ge=1,
        description="Page number, starting from 1. Default: 1.",
    )
    records: int = Field(
        default=10,
        ge=1,
        le=30,
        description="Records per page. Default: 10, max: 30.",
    )


class InstrumentSearchItem(BaseModel):
    """A single instrument from the search response."""

    name: Optional[str] = None
    segment: Optional[str] = None
    exchange: Optional[str] = None
    isin: Optional[str] = None
    instrument_key: Optional[str] = None
    exchange_token: Optional[str] = None
    trading_symbol: Optional[str] = None
    short_name: Optional[str] = None
    tick_size: Optional[float] = None
    lot_size: Optional[float] = None
    instrument_type: Optional[str] = None
    freeze_quantity: Optional[float] = None
    qty_multiplier: Optional[float] = None
    security_type: Optional[str] = None
    cas_eligible: Optional[bool] = None
    expiry: Optional[str] = None
    weekly: Optional[bool] = None
    underlying_key: Optional[str] = None
    underlying_type: Optional[str] = None
    underlying_symbol: Optional[str] = None
    strike_price: Optional[float] = None
    minimum_lot: Optional[float] = None

    model_config = {"extra": "allow"}


class PageMeta(BaseModel):
    page_number: int = 1
    total_pages: int = 0
    records: int = 0
    total_records: int = 0


class MetaData(BaseModel):
    page: PageMeta = Field(default_factory=PageMeta)


class InstrumentSearchResponse(BaseModel):
    """Response from GET /v2/instruments/search."""

    status: str = "success"
    data: List[InstrumentSearchItem] = Field(default_factory=list)
    meta_data: MetaData = Field(default_factory=MetaData)

    model_config = {"extra": "allow"}


class InstrumentSearchProxyResponse(BaseModel):
    """Response wrapper for the proxy endpoint."""

    success: bool = True
    message: str = "OK"
    status_code: int = 200
    data: Optional[Dict[str, Any]] = None