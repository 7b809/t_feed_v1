"""
Per-instrument state and minute-bar aggregation.

Every tracked instrument keeps:
  - a rolling EMA fast / slow state
  - a previous-diff snapshot for cross detection
  - one active minute buffer (open / high / low / close)
  - the opening range levels for today (derived from the first candle)
  - today's cross records (persisted to intraday_cross.json)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class MinuteBar:
    minute_ts: int
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    ticks: int = 0

    def add_tick(self, price: float) -> None:
        if self.ticks == 0:
            self.open = price
            self.high = price
            self.low = price
            self.close = price
        else:
            if price > self.high:
                self.high = price
            if price < self.low:
                self.low = price
            self.close = price
        self.ticks += 1

    def is_flat(self) -> bool:
        return (
            self.ticks == 0
            or (self.open == self.high == self.low == self.close)
        )

    def to_candle(self) -> Dict[str, float]:
        return {
            "time": self.minute_ts,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
        }


@dataclass
class InstrumentState:
    instrument_key: str

    underlying: str = ""
    strike: Optional[float] = None
    option_type: Optional[str] = None      # "CE" | "PE" | None
    expiry: Optional[str] = None
    trading_symbol: Optional[str] = None

    # EMA state (None until first candle processed)
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    prev_diff: Optional[float] = None

    # Last processed candle
    last_candle_time: Optional[int] = None
    last_close: Optional[float] = None

    # Active minute buffer
    active_bar: Optional[MinuteBar] = None

    # Opening range — computed from the first candle of today
    opening_range_levels: Optional[Dict[str, float]] = None
    opening_range_candle_time: Optional[int] = None

    # Today's session stats
    first_candle_time_today: Optional[int] = None
    candles_considered_today: int = 0

    # Crosses recorded today (persisted to intraday_cross.json)
    crosses_today_records: List[Dict[str, Any]] = field(default_factory=list)
    persisted_cross_times: Set[int] = field(default_factory=set)

    # Backfill throttling
    last_backfilled_at: Optional[float] = None

    # ------------------------------------------------------------------
    # EMA
    # ------------------------------------------------------------------
    def apply_close(
        self,
        candle_ts: int,
        close_price: float,
        fast_alpha: float,
        slow_alpha: float,
    ) -> None:
        """Advance EMA state with a completed candle's close."""
        if self.ema_fast is None:
            self.ema_fast = close_price
        else:
            self.ema_fast = close_price * fast_alpha + self.ema_fast * (1 - fast_alpha)

        if self.ema_slow is None:
            self.ema_slow = close_price
        else:
            self.ema_slow = close_price * slow_alpha + self.ema_slow * (1 - slow_alpha)

        self.last_candle_time = candle_ts
        self.last_close = close_price

    def current_diff(self) -> Optional[float]:
        if self.ema_fast is None or self.ema_slow is None:
            return None
        return self.ema_fast - self.ema_slow

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        return {
            "instrument_key": self.instrument_key,
            "underlying": self.underlying,
            "strike": self.strike,
            "option_type": self.option_type,
            "expiry": self.expiry,
            "trading_symbol": self.trading_symbol,
            "ema_fast": self.ema_fast,
            "ema_slow": self.ema_slow,
            "prev_diff": self.prev_diff,
            "last_candle_time": self.last_candle_time,
            "last_close": self.last_close,
            "crosses_today": len(self.crosses_today_records),
            "opening_range_levels": self.opening_range_levels,
            "opening_range_candle_time": self.opening_range_candle_time,
            "first_candle_time_today": self.first_candle_time_today,
            "candles_considered_today": self.candles_considered_today,
        }