"""Bucket LTP ticks into fixed-interval OHLC candles.

One aggregator per instrument. It consumes ``(ltp, ltt, received_at)`` ticks
and emits a completed candle every time the wall-clock bucket rolls over.

Buckets are aligned to wall-clock intervals in ``settings.market_timezone``:

* ``LIVE_EMA_INTERVAL_SECONDS=60``  -> 1-minute calendar buckets.
* ``LIVE_EMA_INTERVAL_SECONDS=300`` -> 5-minute buckets at :00, :05, :10, ...

The aggregator never emits EMA alerts. It only produces closed candles;
the caller is responsible for merging them, computing EMA, and detecting
crosses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

class CandleAggregator:
    def __init__(
        self,
        interval_seconds: int,
        on_candle_completed: Callable[[dict[str, Any]], None],
    ) -> None:
        self.interval_seconds = max(1, interval_seconds)
        self._on_candle_completed = on_candle_completed
        self._bucket: dict[str, Any] | None = None
        try:
            self._tz = ZoneInfo(settings.market_timezone)
        except Exception:
            self._tz = timezone.utc

    # ------------------------------------------------------------------ #
    # Tick intake                                                        #
    # ------------------------------------------------------------------ #
    def on_tick(self, ltp: Any, ltt_ms: Any, received_at: datetime) -> None:
        if ltp is None:
            return
        try:
            ltp_f = float(ltp)
        except (TypeError, ValueError):
            return

        bucket_start = self._bucket_start_for(received_at)
        current = self._bucket

        if current is None:
            self._bucket = self._new_bucket(bucket_start, ltp_f)
            return

        if bucket_start > current["bucket_start"]:
            completed = self._finalize(current)
            self._bucket = self._new_bucket(bucket_start, ltp_f)
            try:
                self._on_candle_completed(completed)
            except Exception:
                logger.exception("Candle completion callback failed")
            return

        if ltp_f > current["high"]:
            current["high"] = ltp_f
        if ltp_f < current["low"]:
            current["low"] = ltp_f
        current["close"] = ltp_f
        current["tick_count"] += 1

    def current_bucket(self) -> dict[str, Any] | None:
        if self._bucket is None:
            return None
        return {
            "timestamp": self._bucket["bucket_start"].isoformat(),
            "open": self._bucket["open"],
            "high": self._bucket["high"],
            "low": self._bucket["low"],
            "close": self._bucket["close"],
            "tick_count": self._bucket["tick_count"],
            "interval_seconds": self._bucket["interval_seconds"],
        }

    # ------------------------------------------------------------------ #
    # Internal helpers                                                   #
    # ------------------------------------------------------------------ #
    def _bucket_start_for(self, ts: datetime) -> datetime:
        local = ts.astimezone(self._tz)
        epoch = int(local.timestamp())
        aligned = epoch - (epoch % self.interval_seconds)
        return datetime.fromtimestamp(aligned, tz=self._tz)

    def _new_bucket(self, bucket_start: datetime, ltp: float) -> dict[str, Any]:
        return {
            "bucket_start": bucket_start,
            "open": ltp,
            "high": ltp,
            "low": ltp,
            "close": ltp,
            "tick_count": 1,
            "interval_seconds": self.interval_seconds,
        }

    def _finalize(self, bucket: dict[str, Any]) -> dict[str, Any]:
        start = bucket["bucket_start"]
        return {
            "timestamp": start.isoformat(),
            "open": bucket["open"],
            "high": bucket["high"],
            "low": bucket["low"],
            "close": bucket["close"],
            "volume": 0,
            "oi": 0,
            "tick_count": bucket["tick_count"],
            "interval_seconds": bucket["interval_seconds"],
        }