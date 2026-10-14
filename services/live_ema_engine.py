"""Live EMA-cross engine fed by the Upstox full-mode WebSocket.

Responsibilities
----------------
* Keep a rolling 1-minute candle series per instrument, updated from
  every feed tick.
* Finalise a candle exactly when the minute rolls over (default
  "on 1-min close") and feed it to the EMA detector.
* When a cross is detected, build the slim cross record and hand it
  to the cross store, then broadcast it on the internal hub.

The engine is deliberately independent of the transport layer — it
takes normalised candle dicts and returns cross dicts. This makes it
trivially unit-testable and lets a future tick-based test harness
drive it without a real WebSocket.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from core import config
from services.instrument_registry import instrument_registry
from services.live_cross_store import live_cross_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Minimal indicator math (kept in-file to avoid a hard import from candle-backup)
# ---------------------------------------------------------------------------

def _ema_series(values: list[float], period: int) -> list[float | None]:
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1.0)
    prev = seed
    for i in range(period, n):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def _atr(candles: list[dict[str, Any]], period: int) -> list[float | None]:
    n = len(candles)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    trs: list[float | None] = [None] * n
    prev_close: float | None = None
    for i, c in enumerate(candles):
        h, l, cl = c.get("high"), c.get("low"), c.get("close")
        if h is None or l is None:
            prev_close = cl or prev_close
            continue
        if prev_close is None:
            trs[i] = float(h) - float(l)
        else:
            trs[i] = max(
                float(h) - float(l),
                abs(float(h) - prev_close),
                abs(float(l) - prev_close),
            )
        prev_close = float(cl) if cl is not None else prev_close
    first = next((i for i, t in enumerate(trs) if t is not None), None)
    if first is None or (n - first) < period:
        return out
    seed_slice = [trs[first + j] for j in range(period)]
    if any(t is None for t in seed_slice):
        return out
    seed_idx = first + period - 1
    prev_atr = sum(t for t in seed_slice if t is not None) / period
    out[seed_idx] = prev_atr
    for i in range(seed_idx + 1, n):
        t = trs[i]
        if t is None:
            out[i] = prev_atr
            continue
        prev_atr = (prev_atr * (period - 1) + t) / period
        out[i] = prev_atr
    return out


def _sma(values: list[float], period: int) -> list[float | None]:
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    window: list[float] = []
    for i, v in enumerate(values):
        window.append(v)
        if len(window) > period:
            window.pop(0)
        if len(window) == period:
            out[i] = sum(window) / period
    return out


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class LiveEmaEngine:
    """Feed-driven EMA cross detector with per-instrument 1-min candles."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # instrument_key -> list of finalized 1-min candles (rolling)
        self._candles: dict[str, list[dict[str, Any]]] = {}
        # instrument_key -> forming 1-min candle (ts, o, h, l, c, v)
        self._forming: dict[str, dict[str, Any]] = {}
        # instrument_key -> last finalized minute timestamp (ISO)
        self._last_minute: dict[str, str] = {}
        # instrument_key -> rolling list of closes (mirror of _candles)
        self._max_candles = 500
        # Sink for detected crosses. Set by the broadcast layer.
        self._cross_handler = None

    # ------------------------------------------------------------------ #
    # Wiring                                                             #
    # ------------------------------------------------------------------ #
    def set_cross_handler(self, handler) -> None:
        """handler(entry, cross_payload) -> None"""
        self._cross_handler = handler

    # ------------------------------------------------------------------ #
    # Feed entry point                                                   #
    # ------------------------------------------------------------------ #
    def on_tick(
        self,
        instrument_key: str,
        *,
        candle_1m: dict[str, Any] | None = None,
        ltp: float | None = None,
        volume: float | None = None,
    ) -> None:
        """Accept a tick.

        Preferred input is ``candle_1m`` (the `I1` entry from the feed's
        `marketOHLC.ohlc` list). When it's missing (option feeds), we
        synthesise one from ``ltp`` + ``volume``.
        """
        entry = instrument_registry.get(instrument_key)
        if entry is None:
            return  # Not a registry instrument; ignore.

        with self._lock:
            self._ingest(entry, candle_1m, ltp, volume)

    # ------------------------------------------------------------------ #
    # Candle ingestion + finalisation                                    #
    # ------------------------------------------------------------------ #
    def _ingest(
        self,
        entry: dict[str, Any],
        candle_1m: dict[str, Any] | None,
        ltp: float | None,
        volume: float | None,
    ) -> None:
        key = entry["instrument_key"]

        # Normalise the incoming 1-min candle.
        ts, o, h, l, c, v = self._normalise(candle_1m, ltp, volume)
        if ts is None:
            return

        minute_key = ts[:16]  # "YYYY-MM-DDTHH:MM"

        forming = self._forming.get(key)

        if forming is None or forming["minute"] != minute_key:
            # Rollover: finalise the previous forming candle (if any).
            if forming is not None:
                self._finalise(entry, forming)

            self._forming[key] = {
                "minute": minute_key,
                "timestamp": ts,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v or 0.0,
            }
            return

        # Same minute — merge the update into the forming candle.
        forming["high"] = max(forming["high"], h) if h is not None else forming["high"]
        forming["low"] = min(forming["low"], l) if l is not None else forming["low"]
        if c is not None:
            forming["close"] = c
        if v is not None:
            forming["volume"] = max(forming["volume"], float(v))

    @staticmethod
    def _normalise(
        candle_1m: dict[str, Any] | None,
        ltp: float | None,
        volume: float | None,
    ) -> tuple[str | None, float | None, float | None, float | None, float | None, float | None]:
        """Return (iso_ts, o, h, l, c, v) from the best available source."""
        if candle_1m:
            ts_ms = candle_1m.get("ts")
            ts = LiveEmaEngine._ms_to_iso(ts_ms)
            o = LiveEmaEngine._flt(candle_1m.get("open"))
            h = LiveEmaEngine._flt(candle_1m.get("high"))
            l = LiveEmaEngine._flt(candle_1m.get("low"))
            c = LiveEmaEngine._flt(candle_1m.get("close"))
            # Feed's per-interval OHLC doesn't carry volume; fall through.
            v = LiveEmaEngine._flt(candle_1m.get("volume")) or volume
            return ts, o, h, l, c, v

        if ltp is None:
            return None, None, None, None, None, None

        # Synthesised candle from a bare LTP tick.
        now = datetime.now(timezone.utc).astimezone()
        ts = now.replace(second=0, microsecond=0).isoformat()
        return ts, ltp, ltp, ltp, ltp, volume

    @staticmethod
    def _ms_to_iso(ts_ms: Any) -> str | None:
        try:
            ts = float(ts_ms)
            if ts > 1e11:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat()
        except Exception:
            return None

    @staticmethod
    def _flt(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ #
    # Finalisation -> EMA detection                                      #
    # ------------------------------------------------------------------ #
    def _finalise(self, entry: dict[str, Any], candle: dict[str, Any]) -> None:
        key = entry["instrument_key"]
        candles = self._candles.setdefault(key, [])
        candles.append(candle)
        if len(candles) > self._max_candles:
            del candles[: len(candles) - self._max_candles]
        self._last_minute[key] = candle["minute"]

        cross = self._detect_cross(entry, candles)
        if cross and self._cross_handler is not None:
            try:
                self._cross_handler(entry, cross)
            except Exception:
                logger.exception("Cross handler failed for %s", key)

    def _detect_cross(
        self,
        entry: dict[str, Any],
        candles: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if len(candles) < config.EMA_SLOW_PERIOD + 2:
            return None

        closes = [c["close"] for c in candles if c.get("close") is not None]
        if len(closes) < config.EMA_SLOW_PERIOD + 2:
            return None

        fast = _ema_series(closes, config.EMA_FAST_PERIOD)
        slow = _ema_series(closes, config.EMA_SLOW_PERIOD)

        i = len(closes) - 1  # index of the just-closed candle
        if i < 1:
            return None
        f_now, s_now = fast[i], slow[i]
        f_prev, s_prev = fast[i - 1], slow[i - 1]
        if None in (f_now, s_now, f_prev, s_prev):
            return None

        prev_diff = f_prev - s_prev
        now_diff = f_now - s_now

        if prev_diff <= 0 < now_diff:
            kind = "bullish"
        elif prev_diff >= 0 > now_diff:
            kind = "bearish"
        else:
            return None

        # ---- metrics ---------------------------------------------------
        atr_arr = _atr(candles, config.EMA_ATR_PERIOD)
        atr_val = atr_arr[i] if i < len(atr_arr) else None

        volumes = [float(c.get("volume") or 0.0) for c in candles]
        avg_vol_arr = _sma(volumes, config.EMA_VOLUME_LOOKBACK)
        avg_vol = avg_vol_arr[i] if i < len(avg_vol_arr) else None
        cur_vol = volumes[i]
        volume_diff = (cur_vol - avg_vol) if avg_vol is not None else 0.0

        ema_diff = f_now - s_now

        # ---- six filters (mirrors candle-backup) ------------------------
        filters: dict[str, bool] = {}

        # 1. Trend — fast > slow on the same series (proxy for HTF)
        filters["trend"] = (
            (f_now > s_now) if kind == "bullish" else (f_now < s_now)
        )

        # 2. Separation vs ATR
        sep_ratio = (abs(ema_diff) / atr_val) if atr_val and atr_val > 0 else None
        filters["separation"] = bool(
            sep_ratio is not None and sep_ratio >= config.EMA_SEP_ATR_MIN
        )

        # 3. Slope agreement
        fast_slope = None
        slow_slope = None
        if i >= config.EMA_SLOPE_LOOKBACK:
            f_prev_slope = fast[i - config.EMA_SLOPE_LOOKBACK]
            s_prev_slope = slow[i - config.EMA_SLOPE_LOOKBACK]
            if f_prev_slope is not None:
                fast_slope = f_now - f_prev_slope
            if s_prev_slope is not None:
                slow_slope = s_now - s_prev_slope
        if fast_slope is not None and slow_slope is not None:
            if kind == "bullish":
                filters["slope"] = fast_slope > 0 and slow_slope > 0
            else:
                filters["slope"] = fast_slope < 0 and slow_slope < 0
        else:
            filters["slope"] = False

        # 4. Volume — treat missing/zero as neutral pass
        if cur_vol is None or cur_vol == 0:
            filters["volume"] = True
        elif avg_vol is not None and avg_vol > 0:
            filters["volume"] = (cur_vol / avg_vol) >= config.EMA_VOLUME_MULT
        else:
            filters["volume"] = False

        # 5. Body — close beyond slow EMA and in the cross direction
        candle = candles[i]
        o = candle.get("open")
        c = candle.get("close")
        if o is not None and c is not None:
            if kind == "bullish":
                filters["body"] = c > s_now and c > o
            else:
                filters["body"] = c < s_now and c < o
        else:
            filters["body"] = False

        # 6. Distance from slow EMA vs ATR
        if c is not None and atr_val and atr_val > 0:
            filters["distance"] = (abs(c - s_now) / atr_val) <= config.EMA_DISTANCE_ATR_MAX
        else:
            filters["distance"] = True

        score = sum(1 for v in filters.values() if v)
        max_score = len(filters)
        threshold = min(config.EMA_MIN_VALIDATION_SCORE, max_score)
        valid = (not config.EMA_ENABLE_VALIDATION) or (score >= threshold)

        return {
            "timestamp": candle["timestamp"],
            "type": kind,
            "fast_ema": round(f_now, 4),
            "slow_ema": round(s_now, 4),
            "volume": float(cur_vol) if cur_vol is not None else 0.0,
            "volume_diff": round(float(volume_diff or 0.0), 4),
            "ema_diff": round(ema_diff, 4),
            "score": score,
            "max_score": max_score,
            "valid": valid,
            "filters": filters,
        }


live_ema_engine = LiveEmaEngine()