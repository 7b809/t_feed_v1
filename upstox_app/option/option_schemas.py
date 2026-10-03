"""
upstox_app/option_schemas.py
Pydantic models for the option-service API.
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ContractSummary(BaseModel):
    instrument_key: Optional[str] = None
    trading_symbol: Optional[str] = None
    instrument_type: Optional[str] = None
    option_type: Optional[str] = None
    strike_price: Optional[float] = None
    expiry: Optional[str] = None
    underlying: Optional[str] = None
    underlying_symbol: Optional[str] = None
    lot_size: Optional[int] = None


class IndexOptionSummary(BaseModel):
    index_name: str
    enabled: bool
    instrument_key: Optional[str] = None
    nearest_expiry: Optional[str] = None
    total_contracts: int
    strike_from: Optional[float] = None
    strike_to: Optional[float] = None
    loaded_at: Optional[str] = None
    source: Optional[str] = None   # runtime | memory | readonly | none


class OptionCacheSummary(BaseModel):
    total_contracts: int
    enabled_indexes: List[str]
    loaded_indexes: List[str]
    per_index: List[IndexOptionSummary]
    readonly_dir: str
    runtime_dir: str


class ContractsResponse(BaseModel):
    index_name: str
    nearest_expiry: Optional[str] = None
    total_contracts: int
    contracts: List[ContractSummary]


class ContractLookupResponse(BaseModel):
    found: bool
    contract: Optional[Dict[str, Any]] = None


class StrikeWindowRequest(BaseModel):
    index_name: str = Field(..., description="NIFTY, SENSEX, ...")
    lower_limit: float
    upper_limit: float
    option_types: Optional[List[str]] = Field(None, description="['CE','PE'] or subset")


class LoadResponse(BaseModel):
    ok: bool
    loaded: List[str]
    failed: List[str]
    persisted: bool
    total_contracts: int
    message: str