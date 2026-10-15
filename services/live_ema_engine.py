"""Live EMA-cross engine fed by the Upstox full-mode WebSocket.

Responsibilities
----------------
* Keep a rolling 1-minute candle series per instrument, updated from
  every feed tick.
* Finalise a candle exactly when the minute rolls over (default
  "on 1-min close") and feed it to the EMA detector.
* When a cross is detected, build the slim cross record and hand it
  to the cross store, then broadcast it on the internal hub.
* Support **seeding** from a historical candle series (backup +
  intraday) so a mid-day restart does not lose prior crosses and does
  not need 20+ minutes to warm the EMA again.
* Expose the *current* EMA state (fast/slow EMA, diff, ATR, volume
  baseline) per instrument through ``get_ema_snapshot`` /
  ``get_ema_series`` so routes can serve it on demand.

The engine is deliberately independent of the transport layer — it
takes normalised candle dicts and returns cross dicts. This makes it
trivially unit-testable and lets a future tick-based test harness
drive it without a real WebSocket.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from core import config
from core.logger import get_logger
from services.instrument_registry import instrument_registry
from services.live_cross_store import live_cross_store

logger = get_logger(__file__)

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
    # Seeding (mid-day restart support)                                  #
    # ------------------------------------------------------------------ #
    def seed(
        self,
        instrument_key: str,
        candles: list[dict[str, Any]],
    ) -> None:
        """Replace the rolling candle buffer for ``instrument_key``.

        ``candles`` must be ascending by minute. Each entry must contain
        at least ``minute`` and ``timestamp``; ``open``, ``high``,
        ``low``, ``close`` are read if present, ``volume`` defaults to
        ``0.0``.

        Also clears any forming candle for this key so the live feed
        starts a fresh bar on its next tick.
        """
        key = str(instrument_key or "").strip()
        if not key:
            return

        normalised: list[dict[str, Any]] = []
        for c in candles or []:
            if not isinstance(c, dict):
                continue
            minute = c.get("minute")
            if not minute:
                continue
            normalised.append({
                "minute": str(minute),
                "timestamp": c.get("timestamp") or str(minute),
                "open": c.get("open"),
                "high": c.get("high"),
                "low": c.get("low"),
                "close": c.get("close"),
                "volume": float(c.get("volume") or 0.0),
            })

        with self._lock:
            self._candles[key] = normalised[-self._max_candles:]
            self._forming.pop(key, None)
            if normalised:
                self._last_minute[key] = normalised[-1]["minute"]

    def scan_crosses(
        self,
        instrument_key: str,
        since_minute_prefix: str | None = None,
    ) -> int:
        """Scan the seeded candle series for EMA crosses.

        Emits via the configured cross handler. Only crosses whose
        candle minute is >= ``since_minute_prefix`` (lexicographic
        compare, valid because minutes are zero-padded ISO) are
        emitted, so we don't re-emit crosses already recorded in the
        backup.

        Returns the number of crosses emitted.
        """
        key = str(instrument_key or "").strip()
        entry = instrument_registry.get(key)
        if entry is None:
            return 0

        with self._lock:
            candles = list(self._candles.get(key) or [])

        if len(candles) < config.EMA_SLOW_PERIOD + 2:
            return 0

        valid = [c for c in candles if c.get("close") is not None]
        if len(valid) < config.EMA_SLOW_PERIOD + 2:
            return 0

        closes = [float(c["close"]) for c in valid]
        fast = _ema_series(closes, config.EMA_FAST_PERIOD)
        slow = _ema_series(closes, config.EMA_SLOW_PERIOD)
        atr_arr = _atr(valid, config.EMA_ATR_PERIOD)
        volumes = [float(c.get("volume") or 0.0) for c in valid]
        avg_vol_arr = _sma(volumes, config.EMA_VOLUME_LOOKBACK)

        emitted = 0
        for i in range(1, len(closes)):
            minute = str(valid[i].get("minute") or "")
            if since_minute_prefix and minute < since_minute_prefix:
                continue
            cross = self._compute_cross(
                entry, valid, i, fast, slow, atr_arr, avg_vol_arr, volumes
            )
            if cross is None:
                continue
            emitted += 1
            if self._cross_handler is not None:
                try:
                    self._cross_handler(entry, cross)
                except Exception:
                    logger.exception("Cross handler failed for %s during scan", key)
        return emitted

    # ------------------------------------------------------------------ #
    # Read-only accessors (used by routes)                               #
    # ------------------------------------------------------------------ #
    def get_candles(
        self,
        instrument_key: str,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Return the rolling 1-min candle buffer for an instrument.

        ``limit`` trims to the most recent N candles (0 / None = all).
        """
        key = str(instrument_key or "").strip()
        entry = instrument_registry.get(key)
        if entry is None:
            return {
                "instrument_key": key,
                "ready": False,
                "reason": "not_registered",
                "count": 0,
                "candles": [],
            }

        with self._lock:
            candles = list(self._candles.get(key) or [])
            forming = self._forming.get(key)

        if limit and limit > 0 and len(candles) > limit:
            candles = candles[-limit:]

        payload = {
            "instrument_key": key,
            "underlying": entry.get("underlying"),
            "strike_price": entry.get("strike_price"),
            "strike_type": entry.get("strike_type"),
            "trading_symbol": entry.get("trading_symbol"),
            "ready": bool(candles),
            "count": len(candles),
            "candles": candles,
        }
        if forming:
            payload["forming"] = forming
        return payload

    def get_ema_snapshot(self, instrument_key: str) -> dict[str, Any]:
        """Return the *current* EMA state for an instrument.

        Safe to call from any thread. Returns a small status dict with
        ``ready=False`` when the engine does not yet have enough
        candles to produce an EMA (e.g. just after startup).
        """
        key = str(instrument_key or "").strip()
        entry = instrument_registry.get(key)
        if entry is None:
            return {
                "instrument_key": key,
                "ready": False,
                "reason": "not_registered",
            }

        with self._lock:
            candles = list(self._candles.get(key) or [])
            forming = self._forming.get(key)

        required = config.EMA_SLOW_PERIOD + 2
        base = {
            "instrument_key": key,
            "underlying": entry.get("underlying"),
            "strike_price": entry.get("strike_price"),
            "strike_type": entry.get("strike_type"),
            "trading_symbol": entry.get("trading_symbol"),
            "candle_count": len(candles),
            "required_candles": required,
        }

        if len(candles) < required:
            return {
                **base,
                "ready": False,
                "reason": "insufficient_candles",
            }

        valid = [c for c in candles if c.get("close") is not None]
        if len(valid) < required:
            return {
                **base,
                "ready": False,
                "reason": "insufficient_valid_candles",
                "candle_count": len(valid),
            }

        closes = [float(c["close"]) for c in valid]
        fast = _ema_series(closes, config.EMA_FAST_PERIOD)
        slow = _ema_series(closes, config.EMA_SLOW_PERIOD)
        atr_arr = _atr(valid, config.EMA_ATR_PERIOD)
        volumes = [float(c.get("volume") or 0.0) for c in valid]
        avg_vol_arr = _sma(volumes, config.EMA_VOLUME_LOOKBACK)

        i = len(closes) - 1
        f_now = fast[i]
        s_now = slow[i]
        ema_diff = (
            (f_now - s_now)
            if (f_now is not None and s_now is not None)
            else None
        )

        last = valid[i]
        trend = None
        if ema_diff is not None:
            trend = "bullish" if ema_diff > 0 else "bearish"

        return {
            **base,
            "ready": True,
            "minute": last.get("minute"),
            "timestamp": last.get("timestamp"),
            "close": last.get("close"),
            "volume": last.get("volume"),
            "fast_period": config.EMA_FAST_PERIOD,
            "slow_period": config.EMA_SLOW_PERIOD,
            "atr_period": config.EMA_ATR_PERIOD,
            "fast_ema": f_now,
            "slow_ema": s_now,
            "ema_diff": ema_diff,
            "trend": trend,
            "atr": atr_arr[i] if i < len(atr_arr) else None,
            "avg_volume": avg_vol_arr[i] if i < len(avg_vol_arr) else None,
            "forming": forming,
        }

    def get_ema_series(
        self,
        instrument_key: str,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Return the EMA series (close, fast EMA, slow EMA) per minute.

        ``limit`` trims to the most recent N rows. Set 0 for all.
        """
        key = str(instrument_key or "").strip()
        entry = instrument_registry.get(key)
        if entry is None:
            return {
                "instrument_key": key,
                "ready": False,
                "reason": "not_registered",
                "count": 0,
                "series": [],
            }

        with self._lock:
            candles = list(self._candles.get(key) or [])

        valid = [c for c in candles if c.get("close") is not None]
        if not valid:
            return {
                "instrument_key": key,
                "underlying": entry.get("underlying"),
                "strike_price": entry.get("strike_price"),
                "strike_type": entry.get("strike_type"),
                "trading_symbol": entry.get("trading_symbol"),
                "ready": False,
                "reason": "no_candles",
                "count": 0,
                "series": [],
            }

        closes = [float(c["close"]) for c in valid]
        fast = _ema_series(closes, config.EMA_FAST_PERIOD)
        slow = _ema_series(closes, config.EMA_SLOW_PERIOD)

        series: list[dict[str, Any]] = []
        for idx, c in enumerate(valid):
            series.append({
                "minute": c.get("minute"),
                "timestamp": c.get("timestamp"),
                "close": closes[idx],
                "fast_ema": fast[idx],
                "slow_ema": slow[idx],
            })

        if limit and limit > 0 and len(series) > limit:
            series = series[-limit:]

        return {
            "instrument_key": key,
            "underlying": entry.get("underlying"),
            "strike_price": entry.get("strike_price"),
            "strike_type": entry.get("strike_type"),
            "trading_symbol": entry.get("trading_symbol"),
            "ready": True,
            "fast_period": config.EMA_FAST_PERIOD,
            "slow_period": config.EMA_SLOW_PERIOD,
            "count": len(series),
            "series": series,
        }

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

        valid = [c for c in candles if c.get("close") is not None]
        if len(valid) < config.EMA_SLOW_PERIOD + 2:
            return None

        closes = [float(c["close"]) for c in valid]
        i = len(closes) - 1

        fast = _ema_series(closes, config.EMA_FAST_PERIOD)
        slow = _ema_series(closes, config.EMA_SLOW_PERIOD)
        atr_arr = _atr(valid, config.EMA_ATR_PERIOD)
        volumes = [float(c.get("volume") or 0.0) for c in valid]
        avg_vol_arr = _sma(volumes, config.EMA_VOLUME_LOOKBACK)

        return self._compute_cross(
            entry, valid, i, fast, slow, atr_arr, avg_vol_arr, volumes
        )

    def _compute_cross(
        self,
        entry: dict[str, Any],
        candles: list[dict[str, Any]],
        i: int,
        fast: list[float | None],
        slow: list[float | None],
        atr_arr: list[float | None],
        avg_vol_arr: list[float | None],
        volumes: list[float],
    ) -> dict[str, Any] | None:
        """Return a cross record if a cross exists at index ``i``, else None.

        All indicator arrays must be aligned with ``candles`` (same length
        and same positional indices).
        """
        if i < 1 or i >= len(candles):
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
        atr_val = atr_arr[i] if i < len(atr_arr) else None
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