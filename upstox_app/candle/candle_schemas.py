from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class CandleFile(BaseModel):
    status: str
    index_name: str
    instrument_key: str
    strike_price: Optional[float] = None
    option_type: Optional[str] = None
    expiry: Optional[str] = None
    trading_symbol: Optional[str] = None
    unit: str
    interval: str
    days: int
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    fetched_at: str
    historical_count: int = 0
    intraday_count: int = 0
    errors: List[str] = []
    candles: List[List[Any]] = []


class CandleIndexResult(BaseModel):
    index_name: str
    total_contracts: int = 0
    already_present: int = 0
    missing_before: int = 0
    fetched: int = 0
    failed: int = 0
    errors: List[str] = []
    paths: List[str] = []


class CandleLoadSummary(BaseModel):
    enabled: bool
    days: int
    results: Dict[str, CandleIndexResult]