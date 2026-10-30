# services/ema_calc_service.py
"""On-demand, stateless EMA cross calculation — LIVE mode.

Mirrors the design of ``services/live_sr_calc_service.py`` so the two
on-demand calculators behave identically from a caller's perspective:

    * NO per-instrument state.
    * NEVER reads ``data/<...>/ema_crosses.json`` or ``live_crosses.json``.
    * NEVER writes anything to disk.
    * Every call recomputes from fresh data and returns the result.

Data sources (merged by minute; intraday wins on collision):

    1. Historical 1-min candles from the backup tree::

           data/index/<underlying>/candles.json          (indexes)
           data/<underlying>/<strike>_<TYPE>/candles.json (options)

    2. Today's intraday 1-min candles from Upstox::

           upstox_client.HistoryV3Api.get_intra_day_candle_data(
               instrument_key, "minutes", "1"
           )

EMA-cross detection
-------------------
Per instrument, the calculator:

    1. Computes EMA(fast=9) and EMA(slow=21) series.
    2. Computes ATR(14) (Wilder's smoothing) for the separation and
       distance filters.
    3. Computes volume SMA(20) for the volume filter.
    4. Scans the merged series for crosses of fast vs slow:
         * bullish  -> fast was <= slow on bar i-1, and >  slow on bar i
         * bearish  -> fast was >= slow on bar i-1, and <  slow on bar i
    5. For every cross, applies the SAME six filters the retired
       ``live_ema_engine`` used:

           trend       — fast and slow aligned in the cross direction
           separation  — |fast − slow| / ATR >= EMA_SEP_ATR_MIN
           slope       — fast EMA moving in the cross direction over
                         EMA_SLOPE_LOOKBACK bars
           volume      — bar volume >= volume-SMA * EMA_VOLUME_MULT
           body        — candle body direction matches the cross
           distance    — |close − slow| / ATR <= EMA_DISTANCE_ATR_MAX

       The score is ``sum(filters.values())``; ``valid`` is true when
       ``score >= EMA_MIN_VALIDATION_SCORE`` (unless
       ``EMA_ENABLE_VALIDATION`` is false, in which case every cross is
       valid).

Intraday-by-default
-------------------
``only_today`` defaults to **True** — the endpoint behaves like an
"intraday EMA-cross" API, returning only crosses whose bar timestamp
falls on today's IST calendar date. Pass ``only_today=false`` to see
crosses from the whole merged window.

Public loader helpers (shared with other services)
--------------------------------------------------
    ``load_historical_candles_for_entry(entry, lookback_days)``
    ``fetch_intraday_candles_for_entry(instrument_key)``
    ``merge_candles(historical, intraday)``

Upstox instrument-key resolution
--------------------------------
Borrowed verbatim from ``live_sr_calc_service`` — a bare friendly name
such as ``"NIFTY"`` is transparently mapped to ``"NSE_INDEX|Nifty 50"``
before the HTTP call, so UDAPI1021 "Instrument key is of invalid
format" errors cannot occur.

Rate-limit / log hygiene
------------------------
Per-resolved-key exponential backoff (15 s → 30 s → 60 s → … capped at
300 s) suppresses repeated identical intraday-fetch failures. The
backoff resets on the first successful fetch. This is the only mutable
state on this service; it never affects the returned candles.

Never raises. Every failure mode lands in a ``status`` field:

    "ok"                — crosses computed
    "not_found"         — selector does not resolve
    "no_data"           — no candles from either source
    "insufficient_data" — fewer candles than the EMA(21) warm-up
    "invalid_params"    — lookback / periods exceed EMA_CALC_MAX_CANDLES
    "error"             — unexpected exception during calc
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger
from services.instrument_registry import instrument_registry
from services.normalization import (
    normalize_strike,
    normalize_strike_type,
    normalize_underlying,
)

logger = get_logger(__file__)

# --------------------------------------------------------------------------- #
# Accepted field-name variants (mirrors live_sr_calc_service)                #
# --------------------------------------------------------------------------- #
_TIME_FIELDS = ("timestamp", "datetime", "ts", "start_time", "t", "time")
_OPEN_FIELDS = ("open", "o")
_HIGH_FIELDS = ("high", "h")
_LOW_FIELDS = ("low", "l")
_CLOSE_FIELDS = ("close", "c")
_VOLUME_FIELDS = ("volume", "v", "vol", "vtt")

# --------------------------------------------------------------------------- #
# Friendly-name → Upstox instrument_key map                                   #
# --------------------------------------------------------------------------- #
_FRIENDLY_INSTRUMENT_KEY_MAP: dict[str, str] = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "NIFTY 50": "NSE_INDEX|Nifty 50",
    "NIFTY50": "NSE_INDEX|Nifty 50",
    "NIFTY BANK": "NSE_INDEX|Nifty Bank",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "NIFTY MID SELECT": "NSE_INDEX|NIFTY MID SELECT",
    "NIFTY MIDCAP SELECT": "NSE_INDEX|NIFTY MID SELECT",
    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
    "NIFTY FIN SERVICE": "NSE_INDEX|Nifty Fin Service",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
    "SENSEX": "BSE_INDEX|SENSEX",
    "BANKEX": "BSE_INDEX|BSE BANKEX",
}

_INTRADAY_BACKOFF_BASE_SEC: float = 15.0
_INTRADAY_BACKOFF_MAX_SEC: float = 300.0
_INTRADAY_ERROR_LOG_MAX_CHARS: int = 500

# --------------------------------------------------------------------------- #
# Time / path helpers                                                         #
# --------------------------------------------------------------------------- #
def _market_tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.MARKET_TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Kolkata")

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _today_iso() -> str:
    return datetime.now(_market_tz()).date().isoformat()

def _parse_time(ts_val: Any) -> datetime | None:
    """Parse a timestamp value into a tz-aware datetime, or return None."""
    if ts_val is None or ts_val == "":
        return None

    try:
        n = float(ts_val)
        if n > 1e10:
            return datetime.fromtimestamp(n / 1000.0, tz=_market_tz())
        if n > 1e9:
            return datetime.fromtimestamp(n, tz=_market_tz())
    except (TypeError, ValueError):
        pass

    try:
        dt = datetime.fromisoformat(str(ts_val).replace("Z", "+00:00"))
    except Exception:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_market_tz())
    return dt

def _first_present(raw: dict[str, Any], fields: tuple[str, ...]) -> Any:
    for f in fields:
        if f in raw and raw[f] is not None:
            return raw[f]
    return None

def _extract_timestamp_from_dict(raw: dict[str, Any]) -> Any:
    for field in _TIME_FIELDS:
        v = raw.get(field)
        if v is not None:
            if field == "time":
                date_v = raw.get("date")
                if date_v is not None:
                    date_s = str(date_v)
                    if "T" not in date_s and " " not in date_s:
                        return f"{date_s}T{str(v)}"
            return v

    date_v = raw.get("date")
    time_v = raw.get("time")
    if date_v is not None and time_v is not None:
        date_s = str(date_v)
        if "T" in date_s or " " in date_s:
            return date_s
        return f"{date_s}T{str(time_v)}"

    if date_v is not None:
        return date_v
    if time_v is not None:
        return time_v
    return None

def _normalise_candle(raw: Any) -> dict[str, Any] | None:
    """Return ``{time, ts_ms, bucket, open, high, low, close, volume}``."""
    # ---- List-of-lists form ----------------------------------------- #
    if isinstance(raw, (list, tuple)) and len(raw) >= 5:
        dt = _parse_time(raw[0])
        if dt is None:
            return None
        try:
            return {
                "time": dt.isoformat(),
                "ts_ms": int(dt.timestamp() * 1000),
                "bucket": int(dt.timestamp() // 60),
                "open": float(raw[1]),
                "high": float(raw[2]),
                "low": float(raw[3]),
                "close": float(raw[4]),
                "volume": float(raw[5]) if len(raw) > 5 else 0.0,
            }
        except (IndexError, TypeError, ValueError):
            return None

    # ---- Dict form -------------------------------------------------- #
    if isinstance(raw, dict):
        ts_val = _extract_timestamp_from_dict(raw)
        dt = _parse_time(ts_val)
        if dt is None:
            return None

        o = _first_present(raw, _OPEN_FIELDS)
        h = _first_present(raw, _HIGH_FIELDS)
        l = _first_present(raw, _LOW_FIELDS)
        c = _first_present(raw, _CLOSE_FIELDS)
        v = _first_present(raw, _VOLUME_FIELDS)
        if v is None:
            v = 0.0

        if any(x is None for x in (o, h, l, c)):
            return None

        try:
            return {
                "time": dt.isoformat(),
                "ts_ms": int(dt.timestamp() * 1000),
                "bucket": int(dt.timestamp() // 60),
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(v),
            }
        except (TypeError, ValueError):
            return None

    return None

def _describe_raw(raw: Any) -> dict[str, Any]:
    try:
        if isinstance(raw, dict):
            return {
                "type": "dict",
                "keys": [str(k) for k in list(raw.keys())[:20]],
            }
        if isinstance(raw, (list, tuple)):
            return {"type": "list", "length": len(raw)}
        return {"type": type(raw).__name__}
    except Exception:
        return {"type": "unknown"}

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

# --------------------------------------------------------------------------- #
# Pure indicator math                                                         #
# --------------------------------------------------------------------------- #
def compute_ema_series(
    candles: list[dict[str, Any]],
    period: int,
) -> list[dict[str, Any]]:
    """Return ``[{time, ts_ms, value}]`` EMA series for ``candles``.

    Standard EMA: ``k = 2 / (period + 1)``; first value seeds at the
    first candle's close. Rows that lack a numeric close are skipped.
    The returned list has one entry per valid candle, in candle order.
    """
    if not candles or period <= 0:
        return []
    k = 2.0 / (period + 1.0)
    out: list[dict[str, Any]] = []
    prev: float | None = None
    for c in candles:
        try:
            close = float(c["close"])
        except (KeyError, TypeError, ValueError):
            continue
        prev = close if prev is None else (close * k + prev * (1.0 - k))
        out.append({
            "time": c.get("time"),
            "ts_ms": c.get("ts_ms"),
            "value": prev,
        })
    return out

def compute_atr_series(
    candles: list[dict[str, Any]],
    period: int = 14,
) -> list[dict[str, Any]]:
    """Return ``[{time, ts_ms, value}]`` Wilder ATR series for ``candles``."""
    n = len(candles)
    if n == 0 or period <= 0:
        return []

    trs: list[float] = [0.0] * n
    try:
        trs[0] = float(candles[0]["high"]) - float(candles[0]["low"])
    except (KeyError, TypeError, ValueError):
        trs[0] = 0.0

    for i in range(1, n):
        try:
            h = float(candles[i]["high"])
            l = float(candles[i]["low"])
            pc = float(candles[i - 1]["close"])
        except (KeyError, TypeError, ValueError):
            trs[i] = 0.0
            continue
        trs[i] = max(h - l, abs(h - pc), abs(l - pc))

    out_vals: list[float] = [0.0] * n
    if n < period:
        running = 0.0
        for i, tr in enumerate(trs):
            running += tr
            out_vals[i] = running / (i + 1)
    else:
        first_sum = sum(trs[:period])
        out_vals[period - 1] = first_sum / period
        for i in range(period, n):
            out_vals[i] = (out_vals[i - 1] * (period - 1) + trs[i]) / period
        running = 0.0
        for i in range(period - 1):
            running += trs[i]
            out_vals[i] = running / (i + 1)

    return [
        {
            "time": candles[i].get("time"),
            "ts_ms": candles[i].get("ts_ms"),
            "value": out_vals[i],
        }
        for i in range(n)
    ]

def compute_volume_sma(
    candles: list[dict[str, Any]],
    period: int = 20,
) -> list[float]:
    """Return a list of volume SMAs, one per candle (in candle order)."""
    n = len(candles)
    out = [0.0] * n
    if n == 0 or period <= 0:
        return out
    for i in range(n):
        start = max(0, i - period + 1)
        window = [
            _safe_float(candles[j].get("volume"), 0.0)
            for j in range(start, i + 1)
        ]
        if window:
            out[i] = sum(window) / len(window)
    return out

# --------------------------------------------------------------------------- #
# EMA-cross computation with the 6-filter scoring                             #
# --------------------------------------------------------------------------- #
def _compute_filter_scores(
    candles: list[dict[str, Any]],
    i: int,
    cross_type: str,
    fast_series: list[dict[str, Any]],
    slow_series: list[dict[str, Any]],
    atr_series: list[dict[str, Any]],
    vol_sma: list[float],
) -> dict[str, bool]:
    """Return the six filter booleans for the cross at bar index ``i``."""
    is_bull = cross_type == "bullish"
    c = candles[i]
    close = _safe_float(c.get("close"))
    open_p = _safe_float(c.get("open"))
    volume = _safe_float(c.get("volume"))

    fast_i = float(fast_series[i]["value"]) if i < len(fast_series) else 0.0
    slow_i = float(slow_series[i]["value"]) if i < len(slow_series) else 0.0
    atr_i = float(atr_series[i]["value"]) if i < len(atr_series) else 0.0

    sep_atr_min = float(getattr(config, "EMA_SEP_ATR_MIN", 0.15))
    vol_mult = float(getattr(config, "EMA_VOLUME_MULT", 1.0))
    dist_max = float(getattr(config, "EMA_DISTANCE_ATR_MAX", 1.5))
    slope_look = int(getattr(config, "EMA_SLOPE_LOOKBACK", 3))

    filters: dict[str, bool] = {}

    # 1. Trend — fast and slow aligned in the cross direction
    filters["trend"] = (fast_i > slow_i) if is_bull else (fast_i < slow_i)

    # 2. Separation — |fast − slow| / ATR
    if atr_i > 0:
        sep = abs(fast_i - slow_i) / atr_i
        filters["separation"] = sep >= sep_atr_min
    else:
        filters["separation"] = False

    # 3. Slope — fast EMA moving in the cross direction
    if i >= slope_look and (i - slope_look) < len(fast_series):
        prev_fast = float(fast_series[i - slope_look]["value"])
        filters["slope"] = (
            fast_i > prev_fast if is_bull else fast_i < prev_fast
        )
    else:
        filters["slope"] = False

    # 4. Volume — bar volume vs volume SMA
    vol_avg = vol_sma[i] if i < len(vol_sma) else 0.0
    if vol_avg > 0:
        filters["volume"] = volume >= vol_avg * vol_mult
    else:
        filters["volume"] = True

    # 5. Body — candle body direction matches the cross
    body = close - open_p
    filters["body"] = (body > 0) if is_bull else (body < 0)

    # 6. Distance — |close − slow| / ATR
    if atr_i > 0:
        dist = abs(close - slow_i) / atr_i
        filters["distance"] = dist <= dist_max
    else:
        filters["distance"] = True

    return filters

def compute_ema_crosses(
    candles: list[dict[str, Any]],
    fast_period: int | None = None,
    slow_period: int | None = None,
    atr_period: int | None = None,
    volume_lookback: int | None = None,
) -> list[dict[str, Any]]:
    """Return every EMA(fast) × EMA(slow) cross detected in ``candles``.

    Each cross record carries the six filter scores the retired
    ``live_ema_engine`` used, plus a ``valid`` flag driven by
    ``EMA_MIN_VALIDATION_SCORE``.
    """
    fast_p = int(fast_period or getattr(config, "EMA_FAST_PERIOD", 9))
    slow_p = int(slow_period or getattr(config, "EMA_SLOW_PERIOD", 21))
    atr_p = int(atr_period or getattr(config, "EMA_ATR_PERIOD", 14))
    vol_look = int(volume_lookback or getattr(config, "EMA_VOLUME_LOOKBACK", 20))

    n = len(candles)
    warmup = max(slow_p, atr_p) + 1
    if n < warmup:
        return []

    fast_series = compute_ema_series(candles, fast_p)
    slow_series = compute_ema_series(candles, slow_p)
    atr_series = compute_atr_series(candles, atr_p)
    vol_sma = compute_volume_sma(candles, vol_look)

    # Guard: all three series must be index-aligned with candles.
    # They are by construction, but check defensively.
    if not (len(fast_series) == n and len(slow_series) == n and len(atr_series) == n):
        return []

    enable_validation = bool(getattr(config, "EMA_ENABLE_VALIDATION", True))
    min_score = int(getattr(config, "EMA_MIN_VALIDATION_SCORE", 3))

    crosses: list[dict[str, Any]] = []
    for i in range(1, n):
        prev_diff = fast_series[i - 1]["value"] - slow_series[i - 1]["value"]
        curr_diff = fast_series[i]["value"] - slow_series[i]["value"]

        is_bull = prev_diff <= 0 and curr_diff > 0
        is_bear = prev_diff >= 0 and curr_diff < 0
        if not (is_bull or is_bear):
            continue

        cross_type = "bullish" if is_bull else "bearish"
        c = candles[i]
        filters = _compute_filter_scores(
            candles, i, cross_type, fast_series, slow_series, atr_series, vol_sma,
        )
        score = sum(1 for v in filters.values() if v)
        max_score = len(filters)
        valid = (not enable_validation) or (score >= min_score)

        atr_i = float(atr_series[i]["value"]) if i < len(atr_series) else 0.0
        fast_i = float(fast_series[i]["value"])
        slow_i = float(slow_series[i]["value"])
        close = _safe_float(c.get("close"))

        sep_atr = (abs(fast_i - slow_i) / atr_i) if atr_i > 0 else None
        dist_atr = (abs(close - slow_i) / atr_i) if atr_i > 0 else None

        crosses.append({
            "type": cross_type,
            "timestamp": c.get("time"),
            "index": i,
            "fast_ema": round(fast_i, 6),
            "slow_ema": round(slow_i, 6),
            "ema_diff": round(curr_diff, 6),
            "close": round(close, 6),
            "open": round(_safe_float(c.get("open")), 6),
            "high": round(_safe_float(c.get("high")), 6),
            "low": round(_safe_float(c.get("low")), 6),
            "volume": round(_safe_float(c.get("volume")), 6),
            "atr": round(atr_i, 6),
            "ema_separation_atr": round(sep_atr, 4) if sep_atr is not None else None,
            "distance_from_slow_atr": round(dist_atr, 4) if dist_atr is not None else None,
            "filters": filters,
            "score": score,
            "max_score": max_score,
            "valid": valid,
            "is_today": False,
        })

    return crosses

def _mark_is_today(crosses: list[dict[str, Any]]) -> None:
    """Set ``is_today`` on every cross based on its bar timestamp."""
    today = datetime.now(_market_tz()).date()
    for cross in crosses:
        try:
            dt = datetime.fromisoformat(
                str(cross["timestamp"]).replace("Z", "+00:00")
            )
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_market_tz())
            cross["is_today"] = (dt.astimezone(_market_tz()).date() == today)
        except Exception:
            cross["is_today"] = False

# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class EMACalcService:
    """On-demand EMA-cross calculator. Stateless for candle data."""

    def __init__(self) -> None:
        self._intraday_fail_until: dict[str, float] = {}
        self._intraday_fail_count: dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # Public entry point                                                 #
    # ------------------------------------------------------------------ #
    def calculate(
        self,
        *,
        instrument_key: str | None = None,
        underlying: str | None = None,
        strike: str | int | float | None = None,
        strike_type: str | None = None,
        fast_period: int | None = None,
        slow_period: int | None = None,
        atr_period: int | None = None,
        volume_lookback: int | None = None,
        lookback_days: int | None = None,
        only_today: bool = True,
        only_valid: bool = False,
        include_candles: bool = False,
        include_ema_series: bool = False,
        debug: bool = False,
    ) -> dict[str, Any]:
        # ---- 1. Resolve entry --------------------------------------- #
        entry, selector_source = self._resolve_entry(
            instrument_key, underlying, strike, strike_type,
        )
        if entry is None:
            return {
                "status": "not_found",
                "error": "instrument_not_found",
                "selector": {
                    "instrument_key": str(instrument_key) if instrument_key else None,
                    "underlying": normalize_underlying(underlying) or underlying,
                    "strike": normalize_strike(strike) or strike,
                    "type": normalize_strike_type(strike_type) or strike_type,
                },
                "generated_at": _now_iso(),
            }

        # ---- 2. Resolve params -------------------------------------- #
        fast_p = max(1, min(200, int(
            fast_period if fast_period is not None
            else getattr(config, "EMA_FAST_PERIOD", 9)
        )))
        slow_p = max(2, min(400, int(
            slow_period if slow_period is not None
            else getattr(config, "EMA_SLOW_PERIOD", 21)
        )))
        atr_p = max(1, min(200, int(
            atr_period if atr_period is not None
            else getattr(config, "EMA_ATR_PERIOD", 14)
        )))
        vol_look = max(2, min(200, int(
            volume_lookback if volume_lookback is not None
            else getattr(config, "EMA_VOLUME_LOOKBACK", 20)
        )))
        lb = max(1, min(
            30,
            lookback_days if lookback_days is not None
            else int(getattr(config, "EMA_CALC_DEFAULT_LOOKBACK_DAYS", 11)),
        ))

        if fast_p >= slow_p:
            return {
                "status": "invalid_params",
                "error": (
                    f"fast_period ({fast_p}) must be strictly less than "
                    f"slow_period ({slow_p})"
                ),
                "params": {"fast_period": fast_p, "slow_period": slow_p},
                "generated_at": _now_iso(),
            }

        max_candles = int(getattr(config, "EMA_CALC_MAX_CANDLES", 3000))
        if slow_p + atr_p + 1 > max_candles:
            return {
                "status": "invalid_params",
                "error": (
                    f"slow_period + atr_period + 1 = {slow_p + atr_p + 1} "
                    f"exceeds EMA_CALC_MAX_CANDLES ({max_candles})"
                ),
                "params": {
                    "slow_period": slow_p,
                    "atr_period": atr_p,
                    "max_candles": max_candles,
                },
                "generated_at": _now_iso(),
            }

        # ---- 3. Fetch both sources ---------------------------------- #
        historical, historical_meta = self._load_historical_candles(
            entry, lb, debug=debug,
        )
        intraday, intraday_meta = self._fetch_intraday_candles(entry["instrument_key"])

        # ---- 4. Merge ----------------------------------------------- #
        merged = self._merge_by_minute(historical, intraday)
        if not merged:
            return {
                "status": "no_data",
                "error": "no_candles_from_either_source",
                "instrument_key": entry["instrument_key"],
                "mode": "intraday" if only_today else "full",
                "today_iso": _today_iso(),
                "params": {
                    "fast_period": fast_p,
                    "slow_period": slow_p,
                    "atr_period": atr_p,
                    "volume_lookback": vol_look,
                    "lookback_days": lb,
                    "only_today": only_today,
                    "only_valid": only_valid,
                    "include_candles": include_candles,
                    "include_ema_series": include_ema_series,
                    "debug": debug,
                    "max_candles": max_candles,
                },
                "sources": {
                    "historical": historical_meta,
                    "intraday": intraday_meta,
                },
                "generated_at": _now_iso(),
            }

        # ---- 5. Cap ------------------------------------------------- #
        merged_before_cap = len(merged)
        if len(merged) > max_candles:
            merged = merged[-max_candles:]

        warmup_needed = max(slow_p, atr_p) + 1
        if len(merged) < warmup_needed:
            return {
                "status": "insufficient_data",
                "error": (
                    f"not enough candles for EMA warm-up — need "
                    f"{warmup_needed}, have {len(merged)}"
                ),
                "instrument_key": entry["instrument_key"],
                "mode": "intraday" if only_today else "full",
                "today_iso": _today_iso(),
                "candle_count": len(merged),
                "candles_needed": warmup_needed,
                "params": {
                    "fast_period": fast_p,
                    "slow_period": slow_p,
                    "atr_period": atr_p,
                    "volume_lookback": vol_look,
                    "lookback_days": lb,
                    "only_today": only_today,
                    "only_valid": only_valid,
                    "include_candles": include_candles,
                    "include_ema_series": include_ema_series,
                    "debug": debug,
                    "max_candles": max_candles,
                },
                "sources": {
                    "historical": historical_meta,
                    "intraday": intraday_meta,
                },
                "generated_at": _now_iso(),
            }

        # ---- 6. Compute crosses ------------------------------------- #
        all_crosses = compute_ema_crosses(
            merged,
            fast_period=fast_p,
            slow_period=slow_p,
            atr_period=atr_p,
            volume_lookback=vol_look,
        )
        _mark_is_today(all_crosses)

        total_before_filters = len(all_crosses)

        # ---- 7. Apply caller filters -------------------------------- #
        crosses = all_crosses
        if only_today:
            crosses = [c for c in crosses if c.get("is_today")]
        if only_valid:
            crosses = [c for c in crosses if c.get("valid")]

        # ---- 8. Current EMA state ----------------------------------- #
        fast_series_full = compute_ema_series(merged, fast_p)
        slow_series_full = compute_ema_series(merged, slow_p)
        current_ema: dict[str, Any] | None = None
        if fast_series_full and slow_series_full:
            try:
                cur_fast = float(fast_series_full[-1]["value"])
                cur_slow = float(slow_series_full[-1]["value"])
                current_ema = {
                    "time": fast_series_full[-1]["time"],
                    "fast_ema": round(cur_fast, 6),
                    "slow_ema": round(cur_slow, 6),
                    "ema_diff": round(cur_fast - cur_slow, 6),
                    "state": "bullish" if cur_fast > cur_slow else "bearish",
                }
            except (KeyError, TypeError, ValueError):
                current_ema = None

        # ---- 9. Nearest crosses (full unfiltered view) -------------- #
        last_bull: dict[str, Any] | None = None
        last_bear: dict[str, Any] | None = None
        for c in reversed(all_crosses):
            if last_bull is None and c["type"] == "bullish":
                last_bull = c
            if last_bear is None and c["type"] == "bearish":
                last_bear = c
            if last_bull is not None and last_bear is not None:
                break

        # ---- 10. Optional candle / EMA series ----------------------- #
        candles_out = merged if include_candles else None
        ema_series_out = None
        if include_ema_series:
            ema_series_out = {
                "fast_period": fast_p,
                "slow_period": slow_p,
                "fast": fast_series_full,
                "slow": slow_series_full,
            }

        # ---- 11. Warnings ------------------------------------------- #
        warnings: list[str] = []
        if historical_meta.get("error"):
            warnings.append(f"historical: {historical_meta['error']}")
        if intraday_meta.get("error"):
            warnings.append(f"intraday: {intraday_meta['error']}")
        if merged_before_cap > max_candles:
            warnings.append(
                f"merged candles capped from {merged_before_cap} to {max_candles}"
            )
        if historical_meta.get("count_mismatch"):
            warnings.append(
                f"historical: declared_count={historical_meta.get('declared_count')} "
                f"but file contains {historical_meta.get('candles_raw')} entries"
            )
        if only_today and len(crosses) == 0:
            if total_before_filters == 0:
                warnings.append(
                    "intraday mode: no EMA crosses detected in the merged window"
                )
            else:
                warnings.append(
                    f"intraday mode: 0 crosses today, {total_before_filters} "
                    f"historical cross(es) available (pass only_today=false)"
                )
        elif only_today and len(crosses) < total_before_filters:
            warnings.append(
                f"intraday mode: filtered out "
                f"{total_before_filters - len(crosses)} historical cross(es)"
            )

        # ---- 12. Response ------------------------------------------- #
        return {
            "status": "ok",
            "mode": "intraday" if only_today else "full",
            "today_iso": _today_iso(),
            "instrument_key": entry["instrument_key"],
            "trading_symbol": entry.get("trading_symbol"),
            "underlying": entry.get("underlying"),
            "strike": entry.get("strike_price"),
            "type": entry.get("strike_type"),
            "expiry": entry.get("expiry"),
            "is_index": bool(entry.get("is_index")),
            "selector_source": selector_source,
            "params": {
                "fast_period": fast_p,
                "slow_period": slow_p,
                "atr_period": atr_p,
                "volume_lookback": vol_look,
                "lookback_days": lb,
                "only_today": only_today,
                "only_valid": only_valid,
                "include_candles": include_candles,
                "include_ema_series": include_ema_series,
                "debug": debug,
                "max_candles": max_candles,
            },
            "sources": {
                "historical": historical_meta,
                "intraday": intraday_meta,
            },
            "merged": {
                "candle_count": len(merged),
                "candle_count_before_cap": merged_before_cap,
                "first_candle": merged[0]["time"] if merged else None,
                "last_candle": merged[-1]["time"] if merged else None,
            },
            "current_ema": current_ema,
            "crosses": crosses,
            "counts": {
                "total": len(crosses),
                "total_unfiltered": total_before_filters,
                "bullish": sum(1 for c in crosses if c["type"] == "bullish"),
                "bearish": sum(1 for c in crosses if c["type"] == "bearish"),
                "valid": sum(1 for c in crosses if c["valid"]),
                "today": sum(1 for c in crosses if c["is_today"]),
            },
            "nearest": {
                "last_bullish": last_bull,
                "last_bearish": last_bear,
            },
            "warnings": warnings,
            "candles": candles_out,
            "ema_series": ema_series_out,
            "generated_at": _now_iso(),
        }

    # ------------------------------------------------------------------ #
    # Public loader helpers (parity with live_sr_calc_service)           #
    # ------------------------------------------------------------------ #
    def load_historical_candles_for_entry(
        self,
        entry: dict[str, Any],
        lookback_days: int,
    ) -> list[dict[str, Any]]:
        """Return the candle list for ``entry``; diagnostics dropped."""
        candles, _meta = self._load_historical_candles(entry, lookback_days)
        return candles

    def fetch_intraday_candles_for_entry(
        self,
        instrument_key: str,
    ) -> list[dict[str, Any]]:
        """Return today's intraday candles; ``[]`` on failure."""
        candles, _meta = self._fetch_intraday_candles(instrument_key)
        return candles

    def merge_candles(
        self,
        historical: list[dict[str, Any]],
        intraday: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge by minute; intraday wins on collision."""
        return self._merge_by_minute(historical, intraday)

    # ------------------------------------------------------------------ #
    # Upstox instrument-key resolution                                   #
    # ------------------------------------------------------------------ #
    def _resolve_upstox_instrument_key(self, raw: Any) -> str | None:
        if raw is None:
            return None
        key = str(raw).strip()
        if not key:
            return None
        if "|" in key:
            return key
        mapped = _FRIENDLY_INSTRUMENT_KEY_MAP.get(key.upper())
        if mapped:
            return mapped

        for meth_name in (
            "get_by_symbol", "find_by_symbol", "by_symbol",
            "lookup_symbol", "find",
        ):
            fn = getattr(instrument_registry, meth_name, None)
            if not callable(fn):
                continue
            try:
                found = fn(key)
            except Exception:
                continue
            if not found:
                continue
            ik: Any = None
            if isinstance(found, dict):
                ik = found.get("instrument_key") or found.get("upstox_instrument_key")
            else:
                ik = getattr(found, "instrument_key", None) or getattr(
                    found, "upstox_instrument_key", None,
                )
            if ik and "|" in str(ik):
                return str(ik)
        return None

    # ------------------------------------------------------------------ #
    # Selector resolution                                                #
    # ------------------------------------------------------------------ #
    def _resolve_entry(
        self,
        instrument_key: str | None,
        underlying: str | None,
        strike: str | int | float | None,
        strike_type: str | None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        key = str(instrument_key).strip() if instrument_key else ""
        if key:
            entry = instrument_registry.get(key)
            if entry is None:
                entry = instrument_registry.register_adhoc(key)
            return entry, "instrument_key"

        u = normalize_underlying(underlying)
        s = normalize_strike(strike)
        t = normalize_strike_type(strike_type)
        if not (u and s and t):
            return None, None

        entry = instrument_registry.resolve(u, s, t)
        if entry is not None:
            return entry, "triple"

        for alt in (f"{u} 50", f"{u}50", u.split(" ", 1)[0]):
            alt_norm = normalize_underlying(alt)
            if not alt_norm or alt_norm == u:
                continue
            entry = instrument_registry.resolve(alt_norm, s, t)
            if entry is not None:
                return entry, "triple"

        return None, None

    # ------------------------------------------------------------------ #
    # Historical candles                                                 #
    # ------------------------------------------------------------------ #
    def _historical_paths(self, entry: dict[str, Any]) -> list[Path]:
        candidates: list[Path] = []
        raw = str(entry.get("candles_path") or "").strip()
        if raw:
            candidates.append(Path(raw))

        underlying = str(entry.get("underlying") or "").strip().lower()
        strike = entry.get("strike_price")
        strike_type = str(entry.get("strike_type") or "").upper()
        is_index = bool(entry.get("is_index")) or strike is None

        if is_index and underlying:
            candidates.append(
                Path(config.LIVE_DATA_DIR) / "index" / underlying / "candles.json"
            )
        elif underlying and strike is not None and strike_type:
            try:
                strike_int = int(float(strike))
            except (TypeError, ValueError):
                strike_int = str(strike)
            candidates.append(
                Path(config.LIVE_DATA_DIR)
                / underlying
                / f"{strike_int}_{strike_type}"
                / "candles.json"
            )

        seen: set[str] = set()
        out: list[Path] = []
        for p in candidates:
            key = str(p)
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
        return out

    def _extract_candles_from_payload(self, payload: Any) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                candles = data.get("candles")
                if isinstance(candles, list):
                    return candles
            if isinstance(data, list):
                return data
            candles = payload.get("candles")
            if isinstance(candles, list):
                return candles
        return []

    def _load_historical_candles(
        self,
        entry: dict[str, Any],
        lookback_days: int,
        debug: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        meta: dict[str, Any] = {
            "path": None,
            "candles": 0,
            "candles_raw": 0,
            "candles_rejected_format": 0,
            "candles_rejected_cutoff": 0,
            "first_candle": None,
            "last_candle": None,
            "error": None,
        }

        chosen: Path | None = None
        for candidate in self._historical_paths(entry):
            if candidate.exists() and candidate.is_file():
                chosen = candidate
                break

        if chosen is None:
            meta["error"] = "file_missing"
            return [], meta

        meta["path"] = str(chosen)

        try:
            payload = json.loads(chosen.read_text(encoding="utf-8"))
        except Exception as exc:
            meta["error"] = f"unreadable: {type(exc).__name__}"
            logger.warning(
                "[ema-calc] cannot parse %s: %s: %s",
                chosen, type(exc).__name__, exc,
            )
            return [], meta

        if isinstance(payload, dict):
            if payload.get("updated_at") is not None:
                meta["file_updated_at"] = str(payload["updated_at"])
            if payload.get("relative_path") is not None:
                meta["relative_path"] = str(payload["relative_path"])
            if payload.get("count") is not None:
                try:
                    meta["declared_count"] = int(payload["count"])
                except (TypeError, ValueError):
                    meta["declared_count"] = payload["count"]

        raw_list = self._extract_candles_from_payload(payload)
        meta["candles_raw"] = len(raw_list)

        if not raw_list:
            meta["error"] = "empty_candles_list"
            return [], meta

        declared = meta.get("declared_count")
        if isinstance(declared, int) and declared != len(raw_list):
            meta["count_mismatch"] = True

        cutoff = datetime.now(_market_tz()).timestamp() - (lookback_days * 86400)

        out: list[dict[str, Any]] = []
        rejected_format = 0
        rejected_cutoff = 0

        for raw in raw_list:
            c = _normalise_candle(raw)
            if c is None:
                rejected_format += 1
                continue
            if c["ts_ms"] / 1000.0 < cutoff:
                rejected_cutoff += 1
                continue
            out.append(c)

        out.sort(key=lambda x: x["ts_ms"])

        meta["candles"] = len(out)
        meta["candles_rejected_format"] = rejected_format
        meta["candles_rejected_cutoff"] = rejected_cutoff
        meta["first_candle"] = out[0]["time"] if out else None
        meta["last_candle"] = out[-1]["time"] if out else None

        if not out:
            meta["sample_structure"] = [_describe_raw(r) for r in raw_list[:3]]
            if rejected_format == len(raw_list):
                meta["error"] = f"all_{rejected_format}_entries_failed_format_parsing"
            elif rejected_cutoff == len(raw_list):
                meta["error"] = f"all_{rejected_cutoff}_entries_older_than_{lookback_days}d_cutoff"
            elif rejected_format + rejected_cutoff == len(raw_list):
                meta["error"] = f"no_entries_passed_(format={rejected_format},cutoff={rejected_cutoff})"
        elif debug:
            meta["sample_structure"] = [_describe_raw(r) for r in raw_list[:3]]

        return out, meta

    # ------------------------------------------------------------------ #
    # Intraday candles                                                   #
    # ------------------------------------------------------------------ #
    def _fetch_intraday_candles(
        self,
        instrument_key: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        meta: dict[str, Any] = {
            "api": "HistoryV3Api.get_intra_day_candle_data",
            "requested_instrument_key": instrument_key,
            "resolved_instrument_key": None,
            "candles": 0,
            "first_candle": None,
            "last_candle": None,
            "error": None,
        }

        resolved = self._resolve_upstox_instrument_key(instrument_key)
        meta["resolved_instrument_key"] = resolved
        if resolved is None:
            meta["error"] = (
                f"invalid_instrument_key: {instrument_key!r} — could not "
                f"resolve to an Upstox EXCHANGE_SEGMENT|SYMBOL key"
            )
            logger.warning(
                "[ema-calc] cannot resolve Upstox instrument_key for %r — "
                "skipping intraday fetch (add it to "
                "_FRIENDLY_INSTRUMENT_KEY_MAP if it's a known index)",
                instrument_key,
            )
            return [], meta

        now = time.monotonic()
        fail_until = self._intraday_fail_until.get(resolved, 0.0)
        if now < fail_until:
            remaining = int(fail_until - now) + 1
            meta["error"] = (
                f"cooldown_active: {remaining}s remaining after "
                f"{self._intraday_fail_count.get(resolved, 0)} "
                f"consecutive failure(s)"
            )
            return [], meta

        try:
            import upstox_client  # type: ignore
        except ImportError:
            meta["error"] = "upstox_client_not_installed"
            return [], meta

        try:
            cfg = upstox_client.Configuration()
            try:
                from services.token_service import token_service
                token = token_service.get_access_token()
                if token:
                    cfg.access_token = token
            except Exception:
                pass

            api = upstox_client.HistoryV3Api(upstox_client.ApiClient(cfg))
            response = api.get_intra_day_candle_data(
                resolved, "minutes", "1",
            )
        except Exception as exc:
            count = self._intraday_fail_count.get(resolved, 0) + 1
            self._intraday_fail_count[resolved] = count
            delay = min(
                _INTRADAY_BACKOFF_MAX_SEC,
                _INTRADAY_BACKOFF_BASE_SEC * (2 ** (count - 1)),
            )
            self._intraday_fail_until[resolved] = time.monotonic() + delay

            meta["error"] = f"{type(exc).__name__}: {exc}"

            exc_str = str(exc)
            if len(exc_str) > _INTRADAY_ERROR_LOG_MAX_CHARS:
                exc_str = exc_str[:_INTRADAY_ERROR_LOG_MAX_CHARS] + "… (truncated)"

            logger.warning(
                "[ema-calc] intraday fetch failed for %r (resolved=%s): "
                "%s: %s — backing off %.0fs",
                instrument_key, resolved, type(exc).__name__, exc_str, delay,
            )
            return [], meta

        self._intraday_fail_count.pop(resolved, None)
        self._intraday_fail_until.pop(resolved, None)

        body: Any = response
        if hasattr(response, "to_dict"):
            try:
                body = response.to_dict()
            except Exception:
                body = response
        elif hasattr(response, "data"):
            body = response.data

        if not isinstance(body, dict):
            meta["error"] = "uncoercible_response"
            return [], meta

        raw_candles = None
        data = body.get("data")
        if isinstance(data, dict):
            maybe = data.get("candles")
            if isinstance(maybe, list):
                raw_candles = maybe
        if not isinstance(raw_candles, list):
            top = body.get("candles")
            if isinstance(top, list):
                raw_candles = top
        if not isinstance(raw_candles, list):
            raw_candles = []

        out: list[dict[str, Any]] = []
        for raw in raw_candles:
            c = _normalise_candle(raw)
            if c is not None:
                out.append(c)

        out.sort(key=lambda x: x["ts_ms"])

        meta["candles"] = len(out)
        meta["first_candle"] = out[0]["time"] if out else None
        meta["last_candle"] = out[-1]["time"] if out else None
        return out, meta

    # ------------------------------------------------------------------ #
    # Merge                                                              #
    # ------------------------------------------------------------------ #
    def _merge_by_minute(
        self,
        historical: list[dict[str, Any]],
        intraday: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_bucket: dict[int, dict[str, Any]] = {}
        for c in historical:
            by_bucket[c["bucket"]] = c
        for c in intraday:
            by_bucket[c["bucket"]] = c
        return [by_bucket[b] for b in sorted(by_bucket.keys())]

# --------------------------------------------------------------------------- #
# Singleton                                                                   #
# --------------------------------------------------------------------------- #
ema_calc_service = EMACalcService()