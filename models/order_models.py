"""Validated request models for order APIs."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, model_validator

from core.config import (
    DEFAULT_ORDER_QUANTITY,
    DEFAULT_TRANSACTION_TYPE,
    DEFAULT_PRODUCT,
    DEFAULT_ORDER_TYPE,
    DEFAULT_VALIDITY,
)


class PlaceOrderRequest(BaseModel):
    instrument_token: str = Field(min_length=3, max_length=100)
    quantity: int = Field(default=DEFAULT_ORDER_QUANTITY, gt=0)
    transaction_type: Literal["BUY", "SELL"] = DEFAULT_TRANSACTION_TYPE
    product: Literal["I", "D", "MTF"] = DEFAULT_PRODUCT
    order_type: Literal["MARKET", "LIMIT", "SL", "SL-M"] = DEFAULT_ORDER_TYPE
    validity: Literal["DAY", "IOC"] = DEFAULT_VALIDITY
    price: float = Field(default=0, ge=0)
    trigger_price: float = Field(default=0, ge=0)
    disclosed_quantity: int = Field(default=0, ge=0)
    is_amo: bool = False
    slice: bool = False
    tag: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def validate_order_prices(self):
        if self.order_type == "MARKET" and self.price != 0:
            raise ValueError("MARKET orders must use price=0")
        if self.order_type in {"SL", "SL-M"} and self.trigger_price <= 0:
            raise ValueError("SL and SL-M orders require trigger_price > 0")
        if self.order_type == "LIMIT" and self.price <= 0:
            raise ValueError("LIMIT orders require price > 0")
        return self


class OrderIdsRequest(BaseModel):
    order_ids: list[str] = Field(min_length=1, max_length=50)


class ExitAllRequest(BaseModel):
    tag: str | None = Field(default=None, max_length=40)
    segment: str | None = Field(default=None, max_length=20)
