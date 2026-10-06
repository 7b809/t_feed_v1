from typing import Literal

from pydantic import BaseModel, Field, field_validator


FeedMode = Literal["ltpc", "full", "full_d30", "option_greeks"]


class SubscriptionRequest(BaseModel):
    instrument_keys: list[str] = Field(min_length=1)
    mode: FeedMode = "full"

    @field_validator("instrument_keys")
    @classmethod
    def clean_keys(cls, values: list[str]) -> list[str]:
        keys = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not keys:
            raise ValueError("At least one instrument key is required")
        return keys


class InstrumentSearchParams(BaseModel):
    query: str = Field(min_length=2, max_length=50)
