"""Validated request models for multi-leg GTT orders."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, model_validator


class PlaceGTTRequest(BaseModel):
    instrument_key: str = Field(min_length=3, max_length=100)
    trading_symbol: str | None = Field(default=None, max_length=100)
    entry: float | None = Field(default=None, gt=0)
    target: float = Field(gt=0)
    stoploss: float = Field(gt=0)
    lot: int = Field(default=1, ge=1, le=1000)
    lot_size: int | None = Field(default=None, ge=1, le=100000)
    product: Literal["I", "D", "MTF"] = "I"
    transaction_type: Literal["BUY"] = "BUY"
    source_order_id: str | None = Field(default=None, max_length=160)
    isolation_id: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_levels(self):
        if self.target <= self.stoploss:
            raise ValueError("target must be greater than stoploss for a BUY GTT")
        if self.entry is not None and not (self.stoploss < self.entry < self.target):
            raise ValueError("For a BUY GTT, entry must be between stoploss and target")
        return self



class CancelGTTRequest(BaseModel):
    gtt_order_id: str = Field(min_length=8, max_length=80, pattern=r"^GTT-[A-Za-z0-9-]+$")


class CleanupGTTRequest(BaseModel):
    gtt_order_ids: list[str] = Field(min_length=1, max_length=20)
    isolation_id: str | None = Field(default=None, max_length=200)
    source_order_id: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_ids(self):
        if any(not value.startswith("GTT-") for value in self.gtt_order_ids):
            raise ValueError("Every gtt_order_id must start with GTT-")
        self.gtt_order_ids = list(dict.fromkeys(self.gtt_order_ids))
        return self


class GTTOrderIDsRequest(BaseModel):
    gtt_order_ids: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_ids(self):
        cleaned = list(dict.fromkeys(str(value).strip() for value in self.gtt_order_ids))
        for value in cleaned:
            suffix = value[4:] if value.startswith("GTT-") else ""
            if not (8 <= len(value) <= 80) or not suffix or not suffix.replace("-", "").isalnum():
                raise ValueError("Every gtt_order_id must be a valid GTT- identifier")
        self.gtt_order_ids = cleaned
        return self
