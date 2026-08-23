from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class Instrument(FlexibleModel):
    instrument_key: str
    trading_symbol: str
    instrument_type: Literal["CE", "PE"]
    strike_price: float
    expiry: date | None = None
    live_ltp: float


class IsolatedInstrument(FlexibleModel):
    selected: bool
    instrument_key: str
    instrument_type: Literal["CE", "PE"]
    strike_price: float
    selected_level: str
    level_value: float
    trigger_price: float
    trigger_field: str
    touch_source: str
    touch_time: datetime
    role: str
    reference_average: float
    latest_main_index_ltp: float


class OppositeInstrument(FlexibleModel):
    instrument_type: Literal["CE", "PE"]
    trading_symbol: str
    strike_price: float
    role: str


class Candle(FlexibleModel):
    timestamp: datetime
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: int | float


class EmaEvent(FlexibleModel):
    type: str
    cross_type: Literal["bullish_cross", "bearish_cross"]
    current_signal: Literal["bullish", "bearish"]
    previous_signal: Literal["bullish", "bearish"]
    ema_calculation_mode: str
    interval_minutes: int = Field(gt=0)
    timestamp: datetime
    timestamp_ms: int
    close: float
    ema_fast_period: int = Field(gt=0)
    ema_slow_period: int = Field(gt=0)
    ema_fast: float
    ema_slow: float
    previous_ema_fast: float
    previous_ema_slow: float
    source: str
    created_at: datetime
    candle: Candle
    tick: Any | None = None


class RangeValues(FlexibleModel):
    open: float
    high: float
    low: float
    close: float
    average: float


class Levels(FlexibleModel):
    r1: float
    s1: float
    r2: float
    s2: float
    r3: float
    s3: float
    r3_threshold: float
    s3_threshold: float


class TouchStatus(FlexibleModel):
    r2_touched: bool
    r3_touched: bool
    s2_touched: bool
    s3_touched: bool
    first_touch_level: str | None = None
    first_touch_source: str | None = None
    first_touch_time: datetime | None = None


class OpeningRange(FlexibleModel):
    available: bool
    date: date
    interval: str
    range: RangeValues
    levels: Levels
    touch_status: TouchStatus


class OrderInstrument(FlexibleModel):
    instrument_key: str
    trading_symbol: str
    strike_price: float
    instrument_type: Literal["CE", "PE"]
    available: bool = True
    live_ltp: float
    distance_from_nifty: float | None = None


class BudgetRange(FlexibleModel):
    enabled: bool
    minimum_price: float
    maximum_price: float
    instrument_type: Literal["CE", "PE"]
    instruments: list[OrderInstrument]


class OrderSuggestion(FlexibleModel):
    rule: str
    suggested_order_side: Literal["CE", "PE"]
    selection_basis: str
    nifty_ltp: float
    nearest_order_instruments: list[OrderInstrument]
    budget_range: BudgetRange


class TelegramStatus(FlexibleModel):
    enabled: bool
    sent: bool
    sent_at: datetime | None = None


class IsolatedEmaAlert(FlexibleModel):
    type: Literal["isolated_ema_alert"]
    alert_title: str
    alert_scope: str
    telegram_alert: bool
    instrument_key: str
    instrument: Instrument
    isolated_instrument: IsolatedInstrument
    opposite_instrument: OppositeInstrument
    ema_event: EmaEvent
    opening_range: OpeningRange
    order_suggestion: OrderSuggestion
    telegram: TelegramStatus

    @model_validator(mode="after")
    def validate_business_consistency(self):
        if self.instrument_key != self.instrument.instrument_key:
            raise ValueError("instrument_key must match instrument.instrument_key")
        if self.instrument_key != self.isolated_instrument.instrument_key:
            raise ValueError("instrument_key must match isolated_instrument.instrument_key")
        if self.instrument.instrument_type != self.isolated_instrument.instrument_type:
            raise ValueError("instrument type must match isolated instrument type")
        if self.ema_event.current_signal == self.ema_event.previous_signal:
            raise ValueError("current_signal and previous_signal must differ for an EMA cross")
        expected_cross = f"{self.ema_event.current_signal}_cross"
        if self.ema_event.cross_type != expected_cross:
            raise ValueError("cross_type must agree with current_signal")
        if self.order_suggestion.suggested_order_side != self.order_suggestion.budget_range.instrument_type:
            raise ValueError("suggested order side must match budget range instrument type")
        return self
