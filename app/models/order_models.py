"""Validated order API request models."""
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict
from app.core.config import settings

class PlaceOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument_token: str = Field(..., min_length=3, description="Upstox instrument token/key")
    transaction_type: Literal["BUY", "SELL"] = settings.DEFAULT_TRANSACTION_TYPE
    quantity: int = Field(default=settings.DEFAULT_QUANTITY, gt=0)
    product: Literal["I", "D", "MTF"] = settings.DEFAULT_PRODUCT
    order_type: Literal["MARKET", "LIMIT", "SL", "SL-M"] = settings.DEFAULT_ORDER_TYPE
    validity: Literal["DAY", "IOC"] = settings.DEFAULT_VALIDITY
    # Upstox requires price=0 for MARKET orders; this is not a guaranteed execution price.
    price: float = Field(default=0, ge=0)
    trigger_price: float = Field(default=0, ge=0)
    disclosed_quantity: int = Field(default=0, ge=0)
    is_amo: bool = False
    market_protection: int = settings.DEFAULT_MARKET_PROTECTION
    tag: str | None = Field(default=None, max_length=40)
    slice: bool = False

class ExitSingleOrderRequest(BaseModel):
    order_id: str = Field(..., min_length=1)

class ExitAllOrdersRequest(BaseModel):
    segment: str | None = None
    tag: str | None = None
