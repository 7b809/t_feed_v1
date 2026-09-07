from datetime import date, datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


InstrumentType = Literal["CE", "PE"]
SignalType = Literal["bullish", "bearish"]
CrossType = Literal[
    "bullish_cross",
    "bearish_cross",
]
OrderStatus = Literal[
    "IGNORED",
    "EXIT_FAILED",
    "PLACE_ORDER_FAILED",
    "ORDER_PLACED",
    "PROCESSING_FAILED",
]


class FlexibleModel(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )


class Instrument(FlexibleModel):
    instrument_key: str
    trading_symbol: str
    instrument_type: InstrumentType
    option_type: InstrumentType | None = None
    strike_price: float
    expiry: date | None = None
    lot_size: int | float | None = None
    live_ltp: float | None = None
    ltp: float | None = None


class IsolatedInstrument(FlexibleModel):
    selected: bool
    instrument_key: str
    instrument_type: InstrumentType
    strike_price: float
    selected_level: str
    level_value: float
    trigger_price: float
    trigger_field: str
    touch_source: str
    touch_time: datetime
    role: str | None = None
    reference_average: float | None = None
    latest_main_index_ltp: float | None = None


class OppositeInstrument(FlexibleModel):
    instrument_type: InstrumentType
    trading_symbol: str
    strike_price: float
    role: str | None = None


class Candle(FlexibleModel):
    timestamp: datetime
    timestamp_ms: int | None = None
    open: float
    high: float
    low: float
    close: float
    volume: int | float
    oi: int | float | None = None


class EmaEvent(FlexibleModel):
    type: str
    cross_type: CrossType
    current_signal: SignalType
    previous_signal: SignalType
    ema_calculation_mode: str
    interval_minutes: int = Field(
        gt=0,
    )
    timestamp: datetime
    timestamp_ms: int | None = None
    close: float
    ltp: float | None = None
    ema_fast_period: int = Field(
        gt=0,
    )
    ema_slow_period: int = Field(
        gt=0,
    )
    ema_fast: float
    ema_slow: float
    previous_ema_fast: float
    previous_ema_slow: float
    source: str
    created_at: datetime
    candle: Candle
    tick: Any | None = None

    @model_validator(mode="after")
    def validate_ema_cross(self):
        if self.current_signal == self.previous_signal:
            raise ValueError(
                "current_signal and previous_signal must differ"
            )

        expected_cross = (
            f"{self.current_signal}_cross"
        )

        if self.cross_type != expected_cross:
            raise ValueError(
                "cross_type must agree with current_signal"
            )

        return self


class ContractInfo(FlexibleModel):
    instrument_key: str
    instrument_type: InstrumentType
    option_type: InstrumentType | None = None
    strike_price: float
    expiry: date | None = None
    trading_symbol: str
    underlying_type: str | None = None
    underlying_symbol: str | None = None
    lot_size: int | float | None = None
    supported_intervals: list[int] = Field(
        default_factory=list,
    )


class RawEmaEvent(FlexibleModel):
    type: str
    instrument_key: str
    timestamp: datetime
    timestamp_ms: int | None = None
    cross_type: CrossType
    direction: SignalType | None = None
    interval_minutes: int = Field(
        gt=0,
    )
    close: float
    ltp: float | None = None
    ema_fast_period: int = Field(
        gt=0,
    )
    ema_slow_period: int = Field(
        gt=0,
    )
    previous_ema_fast: float
    previous_ema_slow: float
    ema_fast: float
    ema_slow: float
    previous_signal: SignalType
    current_signal: SignalType
    source: str
    ema_calculation_mode: str
    created_at: datetime
    candle: Candle
    tick: Any | None = None
    contract_info: ContractInfo | None = None
    info: ContractInfo | None = None
    telegram_alert_scope: str | None = None
    is_simulation: bool = False
    simulation: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_ema_cross(self):
        if self.current_signal == self.previous_signal:
            raise ValueError(
                "current_signal and previous_signal must differ"
            )

        expected_cross = (
            f"{self.current_signal}_cross"
        )

        if self.cross_type != expected_cross:
            raise ValueError(
                "cross_type must agree with current_signal"
            )

        if self.direction is None:
            self.direction = self.current_signal

        if self.direction != self.current_signal:
            raise ValueError(
                "direction must agree with current_signal"
            )

        if (
            self.contract_info is not None
            and self.contract_info.instrument_key
            != self.instrument_key
        ):
            raise ValueError(
                "contract_info.instrument_key must match "
                "instrument_key"
            )

        if (
            self.info is not None
            and self.info.instrument_key
            != self.instrument_key
        ):
            raise ValueError(
                "info.instrument_key must match instrument_key"
            )

        return self


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
    r3_threshold: float | None = None
    s3_threshold: float | None = None


class TouchStatus(FlexibleModel):
    r2_touched: bool
    r3_touched: bool
    s2_touched: bool
    s3_touched: bool
    first_touch_level: str | None = None
    first_touch_source: str | None = None
    first_touch_time: datetime | None = None


class OpeningRange(FlexibleModel):
    available: bool | None = None
    date: None = None
    interval: str | None = None
    selected_level: str | None = None
    selected_level_value: float | None = None
    trigger_price: float | None = None
    trigger_field: str | None = None
    touch_source: str | None = None
    touch_time: datetime | None = None
    reference_average: float | None = None
    range: RangeValues
    levels: Levels
    touch_status: TouchStatus | None = None


class MarketData(FlexibleModel):
    ltp: float | None = None
    volume: int | float | None = None
    oi: int | float | None = None
    close_price: float | None = None
    bid_price: float | None = None
    bid_qty: int | float | None = None
    ask_price: float | None = None
    ask_qty: int | float | None = None
    prev_oi: int | float | None = None


class OptionGreeks(FlexibleModel):
    vega: float | None = None
    theta: float | None = None
    gamma: float | None = None
    delta: float | None = None
    iv: float | None = None
    pop: float | None = None


class OrderInstrument(FlexibleModel):
    instrument_key: str
    trading_symbol: str
    strike_price: float
    instrument_type: InstrumentType
    option_type: InstrumentType | None = None
    expiry: date | None = None
    available: bool = True
    live_ltp: float | None = None
    ltp: float | None = None
    lot_size: int | float | None = None
    underlying_key: str | None = None
    underlying_type: str | None = None
    underlying_symbol: str | None = None
    underlying_spot_price: float | None = None
    distance_from_nifty: float | None = None
    pcr: float | None = None
    close_price: float | None = None
    market_data: MarketData | None = None
    option_greeks: OptionGreeks | None = None
    data_source: str | None = None
    is_isolated_instrument: bool | None = None

    @model_validator(mode="after")
    def populate_live_ltp(self):
        if self.live_ltp is not None:
            return self

        if self.ltp is not None:
            self.live_ltp = self.ltp
            return self

        if (
            self.market_data is not None
            and self.market_data.ltp is not None
        ):
            self.live_ltp = self.market_data.ltp

        return self


class BudgetRange(FlexibleModel):
    enabled: bool
    minimum_price: float
    maximum_price: float
    instrument_type: InstrumentType | None = None
    maximum_instruments: int | None = None
    instruments: list[OrderInstrument] = Field(
        default_factory=list,
    )


class BudgetFilter(FlexibleModel):
    enabled: bool
    minimum_price: float
    maximum_price: float
    maximum_instruments: int | None = None
    sort_mode: str | None = None
    matched_count: int | None = None
    included_count: int | None = None
    range_inclusive: bool = True
    data_source: str | None = None
    instruments: list[OrderInstrument] = Field(
        default_factory=list,
    )


class OrderSuggestion(FlexibleModel):
    rule: str
    suggested_order_side: InstrumentType
    isolated_instrument_type: InstrumentType | None = None
    selection_basis: str | None = None
    nifty_ltp: float | None = None
    underlying_spot_price: float | None = None
    expiry_date: date | None = None
    expiry_source: str | None = None
    data_source: str | None = None
    nearest_strikes: list[float] = Field(
        default_factory=list,
    )
    nearest_instruments: list[OrderInstrument] = Field(
        default_factory=list,
    )
    nearest_order_instruments: list[
        OrderInstrument
    ] = Field(
        default_factory=list,
    )
    budget_filter: BudgetFilter | None = None
    budget_range: BudgetRange | None = None

    @model_validator(mode="after")
    def validate_suggested_side(self):
        if self.budget_range is not None:
            budget_type = (
                self.budget_range.instrument_type
            )

            if (
                budget_type is not None
                and budget_type
                != self.suggested_order_side
            ):
                raise ValueError(
                    "suggested_order_side must match "
                    "budget_range.instrument_type"
                )

        for instrument in (
            self.get_candidate_instruments()
        ):
            if (
                instrument.instrument_type
                != self.suggested_order_side
            ):
                raise ValueError(
                    "candidate instrument type must match "
                    "suggested_order_side"
                )

        return self

    def get_candidate_instruments(
        self,
    ) -> list:
        if (
            self.budget_filter is not None
            and self.budget_filter.instruments
        ):
            return self.budget_filter.instruments

        if (
            self.budget_range is not None
            and self.budget_range.instruments
        ):
            return self.budget_range.instruments

        if self.nearest_order_instruments:
            return self.nearest_order_instruments

        return self.nearest_instruments


class InstrumentSelection(FlexibleModel):
    performed: bool = False
    target_price: float | None = None
    instrument_type: InstrumentType | None = None
    candidate_instrument_count: int = Field(
        default=0,
        ge=0,
    )
    available_instrument_count: int = Field(
        default=0,
        ge=0,
    )
    selected_instrument: OrderInstrument | None = None
    selected_live_ltp: float | None = None
    price_difference: float | None = None

    @model_validator(mode="after")
    def calculate_selection_values(self):
        if self.selected_instrument is None:
            return self

        if self.selected_live_ltp is None:
            self.selected_live_ltp = (
                self.selected_instrument.live_ltp
            )

        if (
            self.selected_live_ltp is not None
            and self.target_price is not None
        ):
            self.price_difference = round(
                abs(
                    self.selected_live_ltp
                    - self.target_price
                ),
                2,
            )

        self.performed = True

        return self


class SelectedOrderInstrument(FlexibleModel):
    instrument_key: str | None = None
    trading_symbol: str | None = None
    instrument_type: InstrumentType | None = None
    strike_price: float | None = None
    live_ltp: float | None = None
    lot_size: int | float | None = None


class PositionExitResult(FlexibleModel):
    success: bool
    response: Any | None = None
    error: Any | None = None


class MarketOrderRequest(FlexibleModel):
    instrument_key: str | None = None
    quantity: int | None = None
    transaction_type: str | None = None
    product: str | None = None
    validity: str | None = None
    tag: str | None = None
    order_type: str | None = None
    price: float | None = None
    trigger_price: float | None = None
    disclosed_quantity: int | None = None
    is_amo: bool | None = None


class MarketOrderResult(FlexibleModel):
    success: bool
    placed_at: datetime | None = None
    request: MarketOrderRequest | None = None
    response: Any | None = None
    error: Any | None = None
    selected_instrument: (
        SelectedOrderInstrument | None
    ) = None


class OrderExecutionResult(FlexibleModel):
    success: bool
    order_status: OrderStatus
    reason: str | None = None
    selected_instrument: (
        SelectedOrderInstrument | None
    ) = None
    exit_result: PositionExitResult | None = None
    place_order_result: (
        MarketOrderResult | None
    ) = None
    error: str | None = None

    @model_validator(mode="after")
    def validate_execution_result(self):
        if self.order_status == "IGNORED":
            if not self.reason:
                raise ValueError(
                    "reason is required when "
                    "order_status is IGNORED"
                )

            if self.exit_result is not None:
                raise ValueError(
                    "exit_result must be empty for "
                    "an ignored order"
                )

            if self.place_order_result is not None:
                raise ValueError(
                    "place_order_result must be empty "
                    "for an ignored order"
                )

        if self.order_status == "ORDER_PLACED":
            if not self.success:
                raise ValueError(
                    "success must be true when "
                    "order_status is ORDER_PLACED"
                )

            if (
                self.exit_result is None
                or not self.exit_result.success
            ):
                raise ValueError(
                    "Successful exit_result is required "
                    "when order_status is ORDER_PLACED"
                )

            if (
                self.place_order_result is None
                or not self.place_order_result.success
            ):
                raise ValueError(
                    "Successful place_order_result is "
                    "required when order_status is "
                    "ORDER_PLACED"
                )

        if self.order_status == "EXIT_FAILED":
            if self.success:
                raise ValueError(
                    "success must be false when "
                    "order_status is EXIT_FAILED"
                )

            if self.place_order_result is not None:
                raise ValueError(
                    "place_order_result must be empty "
                    "when position exit fails"
                )

        if self.order_status == "PLACE_ORDER_FAILED":
            if self.success:
                raise ValueError(
                    "success must be false when "
                    "order_status is PLACE_ORDER_FAILED"
                )

            if self.exit_result is None:
                raise ValueError(
                    "exit_result is required when "
                    "order placement fails"
                )

        if self.order_status == "PROCESSING_FAILED":
            if self.success:
                raise ValueError(
                    "success must be false when "
                    "order_status is PROCESSING_FAILED"
                )

        return self


class OrderRequestProcessing(FlexibleModel):
    target_price: float
    instrument_type: InstrumentType | None = None
    candidate_instrument_count: int = Field(
        default=0,
        ge=0,
    )
    available_instrument_count: int = Field(
        default=0,
        ge=0,
    )
    selection_attempted: bool = False
    selection_performed: bool = False
    selected_instrument: OrderInstrument | None = None
    price_difference: float | None = None
    selection: InstrumentSelection | None = None
    execution_result: OrderExecutionResult | None = None

    @model_validator(mode="after")
    def validate_processing_result(self):
        if self.selected_instrument is not None:
            self.selection_performed = True

            selected_ltp = (
                self.selected_instrument.live_ltp
            )

            if selected_ltp is not None:
                self.price_difference = round(
                    abs(
                        selected_ltp
                        - self.target_price
                    ),
                    2,
                )

        if (
            self.execution_result is not None
            and self.selected_instrument is None
        ):
            raise ValueError(
                "selected_instrument is required when "
                "execution_result exists"
            )

        return self


class SimulationDeliveryControls(FlexibleModel):
    send_telegram: bool = True
    send_algo_app: bool = True


class Simulation(FlexibleModel):
    enabled: bool = False
    dry_run: bool = False
    requested_by: str | None = None
    requested_at: datetime | None = None
    live_state_modified: bool = False
    selected_state_modified: bool = False
    delivery_controls: (
        SimulationDeliveryControls | None
    ) = None


class MarketSnapshot(FlexibleModel):
    nifty_ltp: float | None = None
    isolated_instrument_ltp: float | None = None
    snapshot_at: datetime | None = None
    underlying_spot_price: float | None = None
    option_chain_expiry: date | None = None
    option_data_source: str | None = None


class DuplicateControl(FlexibleModel):
    minute_alert_key: str | None = None
    direction: SignalType | None = None
    bypassed_for_simulation: bool = False


class CurrentEmaDetails(FlexibleModel):
    cross_type: CrossType
    calculation_mode: str
    previous_signal: SignalType
    current_signal: SignalType
    fast_period: int = Field(
        gt=0,
    )
    slow_period: int = Field(
        gt=0,
    )
    fast_value: float
    slow_value: float
    previous_fast_value: float
    previous_slow_value: float
    price: float
    source: str
    timestamp: datetime
    candle: Candle

    @model_validator(mode="after")
    def validate_ema_cross(self):
        if self.current_signal == self.previous_signal:
            raise ValueError(
                "current_signal and previous_signal must differ"
            )

        expected_cross = (
            f"{self.current_signal}_cross"
        )

        if self.cross_type != expected_cross:
            raise ValueError(
                "cross_type must agree with current_signal"
            )

        return self


class RawEmaOrderRequest(FlexibleModel):
    schema_version: str | int | None = None
    event_id: str | None = None
    event_type: str | None = None
    source: str | None = None
    market: str | None = None
    timezone: str | None = None
    created_at: datetime | None = None
    is_simulation: bool = False
    simulation: Simulation | None = None
    instrument: Instrument | None = None
    opening_range: OpeningRange | None = None
    market_snapshot: MarketSnapshot | None = None
    ema: CurrentEmaDetails | None = None
    order_suggestion: OrderSuggestion | None = None
    duplicate_control: DuplicateControl | None = None
    raw_ema_event: RawEmaEvent
    target_price: float | None = None
    requested_price: float | None = None
    instrument_type: InstrumentType | None = None
    available_instruments: list[
        OrderInstrument
    ] = Field(
        default_factory=list,
    )
    processing: OrderRequestProcessing | None = None

    @model_validator(mode="after")
    def validate_request_consistency(self):
        if (
            self.instrument is not None
            and self.instrument.instrument_key
            != self.raw_ema_event.instrument_key
        ):
            raise ValueError(
                "instrument.instrument_key must match "
                "raw_ema_event.instrument_key"
            )

        if self.ema is not None:
            if (
                self.ema.cross_type
                != self.raw_ema_event.cross_type
            ):
                raise ValueError(
                    "ema.cross_type must match "
                    "raw_ema_event.cross_type"
                )

            if (
                self.ema.current_signal
                != self.raw_ema_event.current_signal
            ):
                raise ValueError(
                    "ema.current_signal must match "
                    "raw_ema_event.current_signal"
                )

        if (
            self.processing is not None
            and self.processing.execution_result
            is not None
            and not self.event_id
        ):
            raise ValueError(
                "event_id is required when "
                "execution_result exists"
            )

        return self


class TelegramStatus(FlexibleModel):
    enabled: bool
    sent: bool
    sent_at: datetime | None = None


class LegacyIsolatedEmaAlert(FlexibleModel):
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
        if (
            self.instrument_key
            != self.instrument.instrument_key
        ):
            raise ValueError(
                "instrument_key must match "
                "instrument.instrument_key"
            )

        if (
            self.instrument_key
            != self.isolated_instrument.instrument_key
        ):
            raise ValueError(
                "instrument_key must match "
                "isolated_instrument.instrument_key"
            )

        if (
            self.instrument.instrument_type
            != self.isolated_instrument.instrument_type
        ):
            raise ValueError(
                "instrument type must match "
                "isolated instrument type"
            )

        return self


IsolatedEmaAlert = RawEmaOrderRequest