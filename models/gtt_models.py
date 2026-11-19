"""Validated request models for multi-leg GTT orders."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, model_validator


class PlaceGTTRequest(BaseModel):
    instrument_key: str = Field(min_length=3, max_length=100)
    entry: float | None = Field(default=None, gt=0)
    target: float = Field(gt=0)
    stoploss: float = Field(gt=0)
    lot: int = Field(default=1, ge=1, le=1000)
    lot_size: int | None = Field(default=None, ge=1, le=100000)
    product: Literal["I", "D", "MTF"] = "I"
    transaction_type: Literal["BUY"] = "BUY"

    @model_validator(mode="after")
    def validate_levels(self):
        if self.target <= self.stoploss:
            raise ValueError("target must be greater than stoploss for a BUY GTT")
        if self.entry is not None and not (self.stoploss < self.entry < self.target):
            raise ValueError("For a BUY GTT, entry must be between stoploss and target")
        return self
