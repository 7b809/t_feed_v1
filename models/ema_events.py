from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from core import config
from utils.common import parse_timestamp


@dataclass(slots=True)
class EmaEvent:
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
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int | float | None = None
    open_interest: int | float | None = None
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
        payload = asdict(self)

        if payload.get("created_at") is None:
            payload["created_at"] = _now_iso()

        return payload

    def to_websocket_payload(
        self,
    ) -> dict[str, Any]:
        content = {
            "timestamp": (self.candle_timestamp or self.timestamp),
            "date": self.trading_date,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "open_interest": self.open_interest,
            "source": self.source,
            "ema_9": self.ema_9,
            "ema_21": self.ema_21,
            "ema_difference": (self.ema_difference),
            "previous_ema_difference": (self.previous_ema_difference),
            "cross_type": self.cross_type,
            "is_bullish_cross": (self.is_bullish_cross),
            "is_bearish_cross": (self.is_bearish_cross),
        }

        return {
            "event": content,
            "event_type": self.event,
            "mode": self.mode,
            "instrument_key": (self.instrument_key),
            "symbol": self.trading_symbol,
            "trading_symbol": (self.trading_symbol),
            "exchange": self.exchange,
            "segment": self.segment,
            "underlying": self.underlying,
            "strike_price": self.strike,
            "option_type": self.option_type,
            "sequence": self.sequence,
            "published_at": (self.created_at or _now_iso()),
        }

    @classmethod
    def from_candle(
        cls,
        *,
        candle: dict[str, Any],
        contract: dict[str, Any],
        mode: str = "live",
        source: str | None = None,
        sequence: int | None = None,
    ) -> EmaEvent:
        cross_type = _normalize_cross_type(candle.get("cross_type"))

        event_name = "ema.crossover" if cross_type else "ema.candle"

        timestamp = _normalize_timestamp(candle.get("timestamp"))

        trading_date = _resolve_trading_date(
            candle,
            timestamp,
        )

        resolved_source = source or str(candle.get("source") or mode)

        return cls(
            event=event_name,
            mode=mode,
            timestamp=timestamp,
            instrument_key=_safe_text(contract.get("instrument_key")),
            trading_symbol=_resolve_symbol(contract),
            exchange=_safe_text(contract.get("exchange")),
            segment=_safe_text(contract.get("segment")),
            underlying=_safe_text(
                contract.get("underlying") or contract.get("underlying_symbol")
            ),
            strike=_safe_float(
                contract.get("strike")
                if contract.get("strike") is not None
                else contract.get("strike_price")
            ),
            option_type=_safe_text(
                contract.get("option_type") or contract.get("option_type_code")
            ),
            open=_safe_float(candle.get("open")),
            high=_safe_float(candle.get("high")),
            low=_safe_float(candle.get("low")),
            close=_safe_float(candle.get("close")),
            volume=_safe_number(candle.get("volume")),
            open_interest=_safe_number(
                candle.get("open_interest")
                if candle.get("open_interest") is not None
                else candle.get("oi")
            ),
            ema_9=_safe_float(candle.get("ema_9")),
            ema_21=_safe_float(candle.get("ema_21")),
            ema_difference=_safe_float(candle.get("ema_difference")),
            previous_ema_difference=(
                _safe_float(candle.get("previous_ema_difference"))
            ),
            cross_type=cross_type,
            is_bullish_cross=(
                cross_type == "bullish" or bool(candle.get("is_bullish_cross"))
            ),
            is_bearish_cross=(
                cross_type == "bearish" or bool(candle.get("is_bearish_cross"))
            ),
            candle_timestamp=timestamp,
            trading_date=trading_date,
            source=resolved_source,
            sequence=sequence,
            created_at=_now_iso(),
        )

    @classmethod
    def live_started(
        cls,
        *,
        instrument_count: int,
        trading_date: str,
        message: str | None = None,
    ) -> EmaEvent:
        return cls(
            event="ema.live_started",
            mode="live",
            timestamp=_now_iso(),
            trading_date=trading_date,
            message=(
                message
                or (
                    "Live EMA processing started "
                    f"for {instrument_count} "
                    "instruments"
                )
            ),
            source="ema_runtime",
            created_at=_now_iso(),
        )

    @classmethod
    def live_stopped(
        cls,
        *,
        trading_date: str | None = None,
        message: str | None = None,
    ) -> EmaEvent:
        return cls(
            event="ema.live_stopped",
            mode="live",
            timestamp=_now_iso(),
            trading_date=trading_date,
            message=(message or "Live EMA processing stopped"),
            source="ema_runtime",
            created_at=_now_iso(),
        )

    @classmethod
    def lifecycle(
        cls,
        *,
        message: str,
        mode: str = "system",
        trading_date: str | None = None,
        source: str = "application",
    ) -> EmaEvent:
        return cls(
            event="ema.lifecycle",
            mode=mode,
            timestamp=_now_iso(),
            trading_date=trading_date,
            message=message,
            source=source,
            created_at=_now_iso(),
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
    ) -> EmaEvent:
        return cls(
            event="ema.error",
            mode=mode,
            timestamp=_now_iso(),
            instrument_key=instrument_key,
            trading_date=trading_date,
            message=message,
            error=error,
            source=source,
            created_at=_now_iso(),
        )


def build_ema_live_event(
    contract: dict[str, Any],
    candle: dict[str, Any],
    *,
    mode: str = "live",
    source: str | None = None,
    sequence: int | None = None,
) -> dict[str, Any]:
    event = EmaEvent.from_candle(
        candle=candle,
        contract=contract,
        mode=mode,
        source=source,
        sequence=sequence,
    )

    return event.to_websocket_payload()


def _now_iso() -> str:
    return datetime.now(config.MARKET_TIMEZONE).isoformat()


def _normalize_timestamp(
    value: Any,
) -> str:
    parsed = parse_timestamp(value)

    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=config.MARKET_TIMEZONE)
        else:
            parsed = parsed.astimezone(config.MARKET_TIMEZONE)

        return parsed.isoformat()

    if value is not None:
        text = str(value).strip()

        if text:
            return text

    return _now_iso()


def _resolve_trading_date(
    candle: dict[str, Any],
    timestamp: str,
) -> str | None:
    candle_date = candle.get("date")

    if candle_date is not None:
        text = str(candle_date).strip()

        if text:
            return text

    parsed = parse_timestamp(timestamp)

    if parsed is not None:
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(config.MARKET_TIMEZONE)

        return parsed.date().isoformat()

    if len(timestamp) >= 10:
        return timestamp[:10]

    return None


def _resolve_symbol(
    contract: dict[str, Any],
) -> str | None:
    return _safe_text(
        contract.get("trading_symbol")
        or contract.get("symbol")
        or contract.get("tradingsymbol")
        or contract.get("underlying_symbol")
        or contract.get("underlying")
        or contract.get("instrument_key")
    )


def _normalize_cross_type(
    value: Any,
) -> str | None:
    if value is None:
        return None

    normalized = str(value).strip().lower()

    if normalized in {
        "bullish",
        "bull",
        "buy",
        "bullish_cross",
    }:
        return "bullish"

    if normalized in {
        "bearish",
        "bear",
        "sell",
        "bearish_cross",
    }:
        return "bearish"

    return normalized or None


def _safe_text(
    value: Any,
) -> str | None:
    if value is None:
        return None

    text = str(value).strip()

    return text or None


def _safe_float(
    value: Any,
) -> float | None:
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_number(
    value: Any,
) -> int | float | None:
    if value is None:
        return None

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    if number.is_integer():
        return int(number)

    return number


__all__ = [
    "EmaEvent",
    "build_ema_live_event",
]
