# app/candles.py
#
# Interval-based OHLCVOI candle aggregation built on top of the official
# 1-minute OHLC data ("I1") that the Upstox full feed carries on every
# tick.
#
# Why I1?
#   The Upstox MarketDataStreamerV3 in `full` / `full_d30` mode includes
#   a `marketOHLC.ohlc` array on every tick. One of those entries has
#   `"interval": "I1"` and represents the currently-forming 1-minute
#   candle for the instrument, updated in real time by Upstox itself.
#
#   Using the exchange-provided 1-minute candle instead of manually
#   accumulating LTP / VTT / OI means our aggregated candles match the
#   official Upstox candles exactly (same OHLC, same volume, same
#   timestamps), including for instruments where the tick stream is
#   sparse or where vtt / oi are not present.
#
# Aggregation model
#   For each instrument we track the last-seen I1 candle ("pending").
#   A minute is considered CLOSED the moment a new I1 with a different
#   `ts` arrives. When that happens, the closed I1 is merged into the
#   running state for each subscribed interval:
#
#     * interval == 1  : the closed I1 IS a complete 1-minute candle.
#                        It is emitted immediately on the same tick
#                        that closed it.
#
#     * interval >= 3  : closed I1 candles are merged into the interval
#                        bucket (open from first, high = max, low = min,
#                        close from last, volume = sum, oi from last).
#                        The bucket is emitted only when its boundary
#                        is crossed by a later I1.
#
# Candle payload shape (matches the client contract):
#
#     [
#         "2026-08-12T15:39:00+05:30",   # interval start, IST, ISO-8601
#         61.0,                          # open
#         61.1,                          # high
#         60.3,                          # low
#         60.3,                          # close
#         157040,                        # volume traded inside the candle
#         7173530                        # open interest at candle close
#     ]

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.logger import get_logger

logger = get_logger(__file__)

IST = ZoneInfo("Asia/Kolkata")

# Only these intervals are allowed.
VALID_INTERVALS: frozenset[int] = frozenset({1, 3, 5, 15, 30, 60})

# If two consecutive I1 timestamps are further apart than this, we treat
# it as a session break (or a long outage) and drop the previous minute
# without emitting a candle for it. Prevents a stale candle from being
# delivered when the market reopens the next morning.
MAX_I1_GAP_SECONDS = 5 * 60


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_ist_from_ms(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=IST)


def align_to_interval(ts_ist: datetime, interval_minutes: int) -> datetime:
    """
    Floor a datetime down to the start of the interval bucket it belongs
    to. Seconds and microseconds are zeroed. The minute is floored to the
    nearest lower multiple of `interval_minutes`.

    For interval=60 this naturally lands on hour boundaries because any
    minute < 60 floors to 0.
    """
    base = ts_ist.replace(second=0, microsecond=0)
    floored_minute = (base.minute // interval_minutes) * interval_minutes
    return base.replace(minute=floored_minute)


@dataclass
class I1Candle:
    """A single, currently-forming 1-minute candle as delivered by Upstox."""
    ts_ms: int
    ts_ist: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    oi: float

    def to_payload(self) -> list:
        return [
            self.ts_ist.isoformat(),
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.oi,
        ]


@dataclass
class IntervalState:
    """Running aggregate for a single (instrument, interval) pair."""
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    oi: float

    def to_payload(self) -> list:
        return [
            self.start.isoformat(),
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.oi,
        ]


class CandleAggregator:
    """
    Aggregates Upstox's official I1 (1-minute) candles into OHLCVOI candles
    on the requested interval. One instance is shared across the app.

    Emit policy — completed candles only:
        * The first I1 seen for an instrument becomes "pending".
        * Ticks whose I1 ts matches the pending one only refresh the
          pending snapshot; nothing is emitted.
        * A tick whose I1 ts is greater than the pending one closes the
          pending minute. For interval=1 the pending I1 is emitted
          immediately; for larger intervals it is merged into the current
          bucket and the bucket is emitted only when its boundary rolls.
    """

    def __init__(self) -> None:
        # instrument_key -> interval -> IntervalState
        self._states: dict[str, dict[int, IntervalState]] = {}

        # instrument_key -> interval -> number of connected clients
        self._refcounts: dict[str, dict[int, int]] = {}

        # instrument_key -> last-seen (still-forming) I1 candle
        self._pending_i1: dict[str, I1Candle] = {}

        logger.info(
            "CandleAggregator initialized | valid_intervals=%s",
            sorted(VALID_INTERVALS),
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register(self, instrument_key: str, interval: int) -> None:
        if interval not in VALID_INTERVALS:
            raise ValueError(f"Invalid interval: {interval}")

        counts = self._refcounts.setdefault(instrument_key, {})
        counts[interval] = counts.get(interval, 0) + 1

        logger.info(
            "Candle aggregator registered | instrument_key=%s | interval=%s | clients=%s",
            instrument_key,
            interval,
            counts[interval],
        )

    def unregister(self, instrument_key: str, interval: int) -> None:
        counts = self._refcounts.get(instrument_key)
        if counts is None:
            return

        current = counts.get(interval, 0)
        if current <= 1:
            counts.pop(interval, None)

            states = self._states.get(instrument_key)
            if states is not None:
                states.pop(interval, None)
        else:
            counts[interval] = current - 1

        if not counts:
            self._refcounts.pop(instrument_key, None)
            self._states.pop(instrument_key, None)
            self._pending_i1.pop(instrument_key, None)

        logger.info(
            "Candle aggregator unregistered | instrument_key=%s | interval=%s",
            instrument_key,
            interval,
        )

    def active_intervals(self, instrument_key: str) -> set[int]:
        return set(self._refcounts.get(instrument_key, {}).keys())

    # ------------------------------------------------------------------
    # Update path
    # ------------------------------------------------------------------
    def update(
        self,
        instrument_key: str,
        feed: dict[str, Any],
    ) -> list[tuple[int, list]]:
        """
        Feed a single tick through the aggregator and return, for every
        active interval on this instrument, the (interval, candle_payload)
        that should be pushed to clients.

        Returns an empty list when no client is tracking this instrument,
        when the tick carries no I1 candle, when the tick is stale, or
        when the current minute is still forming.
        """
        counts = self._refcounts.get(instrument_key)
        if not counts:
            return []

        current = self._extract_i1(feed)
        if current is None:
            return []

        pending = self._pending_i1.get(instrument_key)

        # ---- First I1 ever seen for this instrument -------------------
        if pending is None:
            self._pending_i1[instrument_key] = current
            return []

        # ---- Same minute, still forming — refresh the pending snapshot
        if current.ts_ms == pending.ts_ms:
            self._pending_i1[instrument_key] = current
            return []

        # ---- Stale / out-of-order tick — ignore -----------------------
        if current.ts_ms < pending.ts_ms:
            return []

        gap_seconds = (current.ts_ms - pending.ts_ms) / 1000.0

        # ---- Session break / long outage — reset silently -------------
        if gap_seconds > MAX_I1_GAP_SECONDS:
            logger.info(
                "Candle aggregator reset after long gap | "
                "instrument_key=%s | gap_seconds=%.1f",
                instrument_key,
                gap_seconds,
            )
            self._pending_i1[instrument_key] = current
            return []

        # ---- Minute rolled over: pending is now COMPLETE --------------
        closed = pending
        self._pending_i1[instrument_key] = current

        results: list[tuple[int, list]] = []

        for interval in list(counts.keys()):
            if interval == 1:
                # The closed I1 is exactly a 1-minute candle.
                results.append((1, closed.to_payload()))
                continue

            completed = self._merge_into_interval(
                instrument_key, interval, closed
            )
            if completed is not None:
                results.append((interval, completed))

        return results

    def _merge_into_interval(
        self,
        instrument_key: str,
        interval: int,
        closed: I1Candle,
    ) -> list | None:
        states = self._states.setdefault(instrument_key, {})
        state = states.get(interval)

        bucket_start = align_to_interval(closed.ts_ist, interval)

        # ---- No bucket yet: start one silently ------------------------
        if state is None:
            states[interval] = IntervalState(
                start=bucket_start,
                open=closed.open,
                high=closed.high,
                low=closed.low,
                close=closed.close,
                volume=closed.volume,
                oi=closed.oi,
            )
            return None

        # ---- Same bucket: merge in-place, no emit ---------------------
        if bucket_start == state.start:
            if closed.high > state.high:
                state.high = closed.high
            if closed.low < state.low:
                state.low = closed.low
            state.close = closed.close
            state.volume += closed.volume
            state.oi = closed.oi
            return None

        # ---- Bucket boundary crossed: emit previous bucket, start new -
        completed = state.to_payload()

        states[interval] = IntervalState(
            start=bucket_start,
            open=closed.open,
            high=closed.high,
            low=closed.low,
            close=closed.close,
            volume=closed.volume,
            oi=closed.oi,
        )

        return completed

    # ------------------------------------------------------------------
    # I1 extraction
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_i1(feed: dict[str, Any]) -> I1Candle | None:
        """
        Pull the currently-forming I1 candle out of the Upstox feed.

        Handles:
            * fullFeed.marketFF.marketOHLC  (options, futures, equities)
            * fullFeed.indexFF.marketOHLC   (index instruments)

        OI is read from marketFF.oi (indices have no OI, so 0.0).
        """
        if not isinstance(feed, dict):
            return None

        try:
            full_feed = feed.get("fullFeed")
            if not isinstance(full_feed, dict):
                return None

            market_ff = full_feed.get("marketFF")
            if isinstance(market_ff, dict):
                entry = CandleAggregator._find_i1_entry(
                    market_ff.get("marketOHLC")
                )
                if entry is not None:
                    oi = _safe_float(market_ff.get("oi"), 0.0)
                    return CandleAggregator._build_i1(entry, oi)

            index_ff = full_feed.get("indexFF")
            if isinstance(index_ff, dict):
                entry = CandleAggregator._find_i1_entry(
                    index_ff.get("marketOHLC")
                )
                if entry is not None:
                    return CandleAggregator._build_i1(entry, 0.0)

            return None

        except Exception:
            logger.exception("Failed to extract I1 candle from feed")
            return None

    @staticmethod
    def _find_i1_entry(market_ohlc: Any) -> dict | None:
        if not isinstance(market_ohlc, dict):
            return None

        ohlc_list = market_ohlc.get("ohlc")
        if not isinstance(ohlc_list, list):
            return None

        for entry in ohlc_list:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("interval", "")).upper() == "I1":
                return entry

        return None

    @staticmethod
    def _build_i1(entry: dict, oi: float) -> I1Candle | None:
        ts_ms = _safe_int(entry.get("ts"))
        if ts_ms is None:
            return None

        try:
            return I1Candle(
                ts_ms=ts_ms,
                ts_ist=_to_ist_from_ms(ts_ms),
                open=_safe_float(entry.get("open"), 0.0),
                high=_safe_float(entry.get("high"), 0.0),
                low=_safe_float(entry.get("low"), 0.0),
                close=_safe_float(entry.get("close"), 0.0),
                volume=_safe_float(entry.get("vol"), 0.0),
                oi=oi,
            )
        except Exception:
            logger.exception("Failed to build I1 candle")
            return None