from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class EmaEvent:
    """
    Common EMA event payload.

    A single EmaEvent is created by the EMA runtime and can then be
    consumed by Telegram, WebSocket clients, API consumers, and storage.

    Event types currently used:

        - ema.crossover
        - ema.candle
        - ema.live_started
        - ema.live_stopped
        - ema.lifecycle
        - ema.error

    Modes currently used:

        - historical
        - intraday
        - live
        - system
    """

    event: str
    mode: str
    timestamp: str
    instrument_key: str | None = None
    trading_symbol: str | None = None
    exchange: str | None = None
    segment: str | None = None
    underlying: str | None = None
    strike: float | None = None
    option_type: str | None = None
    close: float | None = None
    ema_9: float | None = None
    ema_21: float | None = None
    ema_difference: float | None = None
    previous_ema_difference: float | None = None
    cross_type: str | None = None
    is_bullish_cross: bool = False
    is_bearish_cross: bool = False
    candle_timestamp: str | None = None
    trading_date: str | None = None
    message: str | None = None
    error: str | None = None
    source: str | None = None
    sequence: int | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """
        Convert the event into a JSON-serializable dictionary.
        """

        payload = asdict(self)
        if self.created_at is None:
            payload["created_at"] = datetime.now().astimezone().isoformat()
        return payload

    @classmethod
    def from_candle(
        cls,
        *,
        candle: dict[str, Any],
        contract: dict[str, Any],
        mode: str = "live",
        source: str = "ema_runtime",
        sequence: int | None = None,
    ) -> "EmaEvent":
        """
        Create an EMA candle/crossover event from a calculated candle.
        """

        cross_type = candle.get("cross_type")
        if cross_type:
            event_name = "ema.crossover"
        else:
            event_name = "ema.candle"

        timestamp = candle.get("timestamp") or datetime.now().astimezone().isoformat()
        trading_date = candle.get("date") or (
            timestamp[:10] if isinstance(timestamp, str) else None
        )

        return cls(
            event=event_name,
            mode=mode,
            timestamp=timestamp,
            instrument_key=contract.get("instrument_key"),
            trading_symbol=(contract.get("trading_symbol") or contract.get("symbol")),
            exchange=contract.get("exchange"),
            segment=contract.get("segment"),
            underlying=(
                contract.get("underlying") or contract.get("underlying_symbol")
            ),
            strike=_safe_float(contract.get("strike") or contract.get("strike_price")),
            option_type=(
                contract.get("option_type") or contract.get("option_type_code")
            ),
            close=_safe_float(candle.get("close")),
            ema_9=_safe_float(candle.get("ema_9")),
            ema_21=_safe_float(candle.get("ema_21")),
            ema_difference=_safe_float(candle.get("ema_difference")),
            previous_ema_difference=_safe_float(candle.get("previous_ema_difference")),
            cross_type=cross_type,
            is_bullish_cross=(
                cross_type == "bullish" or bool(candle.get("is_bullish_cross"))
            ),
            is_bearish_cross=(
                cross_type == "bearish" or bool(candle.get("is_bearish_cross"))
            ),
            candle_timestamp=timestamp,
            trading_date=trading_date,
            source=source,
            sequence=sequence,
        )

    @classmethod
    def live_started(
        cls, *, instrument_count: int, trading_date: str, message: str | None = None
    ) -> "EmaEvent":
        """
        Create a live EMA engine started event.
        """

        return cls(
            event="ema.live_started",
            mode="live",
            timestamp=datetime.now().astimezone().isoformat(),
            trading_date=trading_date,
            message=(
                message
                or (
                    "Live EMA processing started " f"for {instrument_count} instruments"
                )
            ),
            source="ema_runtime",
        )

    @classmethod
    def live_stopped(
        cls, *, trading_date: str | None = None, message: str | None = None
    ) -> "EmaEvent":
        """
        Create a live EMA engine stopped event.
        """

        return cls(
            event="ema.live_stopped",
            mode="live",
            timestamp=datetime.now().astimezone().isoformat(),
            trading_date=trading_date,
            message=(message or "Live EMA processing stopped"),
            source="ema_runtime",
        )

    @classmethod
    def lifecycle(
        cls,
        *,
        message: str,
        mode: str = "system",
        trading_date: str | None = None,
        source: str = "application",
    ) -> "EmaEvent":
        """
        Create an application/EMA lifecycle event.
        """

        return cls(
            event="ema.lifecycle",
            mode=mode,
            timestamp=datetime.now().astimezone().isoformat(),
            trading_date=trading_date,
            message=message,
            source=source,
        )

    @classmethod
    def error(
        cls,
        *,
        message: str,
        error: str | None = None,
        mode: str = "system",
        instrument_key: str | None = None,
        trading_date: str | None = None,
        source: str = "application",
    ) -> "EmaEvent":
        """
        Create a standardized EMA/application error event.
        """

        return cls(
            event="ema.error",
            mode=mode,
            timestamp=datetime.now().astimezone().isoformat(),
            instrument_key=instrument_key,
            trading_date=trading_date,
            message=message,
            error=error,
            source=source,
        )


def _safe_float(value: Any) -> float | None:
    """
    Safely convert a value to float.
    """

    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None
