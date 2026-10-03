"""
api/schemas.py

Response models for the global instrument + candle API.
Contract bodies are kept as plain dicts because the shape comes
straight from Upstox and may gain fields over time.
"""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ContractLookupResponse(BaseModel):
    found: bool
    index_name: Optional[str] = None
    instrument_key: Optional[str] = None
    contract: Optional[Dict[str, Any]] = None


class CandleResponse(BaseModel):
    found: bool
    instrument_key: str
    index_name: Optional[str] = None
    source: str = Field(description="readonly | upstream")
    unit: str
    interval: str
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    count: int
    candles: List[List[Any]]


class CombinedCandleResponse(BaseModel):
    found: bool
    instrument_key: str
    index_name: Optional[str] = None
    unit: str
    interval: str
    historical: CandleResponse
    intraday: CandleResponse


class InstrumentsStatus(BaseModel):
    enabled_indexes: List[str]
    readonly_root: str
    runtime_root: str
    default_historical_days: int
    default_unit: str
    default_interval: str