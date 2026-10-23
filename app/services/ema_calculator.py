# app/services/ema_calculator.py
#
# Pure EMA math + cross detection. No I/O, no Upstox, no MEGA.

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable


# ---------------------------------------------------------------------------
def ema_step(prev: float | None, value: float, period: int) -> float:
    """One EMA step. Returns ``value`` on the first call (seed)."""
    if prev is None:
        return float(value)
    k = 2.0 / (period + 1.0)
    return float(value) * k + float(prev) * (1.0 - k)


def parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def candle_ts(candle: dict) -> datetime | None:
    return parse_ts(candle.get("timestamp"))


# ---------------------------------------------------------------------------
def detect_crosses(
    candles: Iterable[dict],
    fast_period: int,
    slow_period: int,
) -> tuple[list[dict], dict]:
    """
    Walk the candle list and return (crosses, final_state).

    Cross records carry the candle ``close`` so downstream payloads can
    report the actual price at which the cross happened.
    """
    crosses: list[dict] = []
    prev_fast: float | None = None
    prev_slow: float | None = None
    prev_diff: float | None = None
    last_ts = None
    last_close = None

    for candle in candles:
        close = float(candle["close"])
        ts = candle.get("timestamp")
        cur_fast = ema_step(prev_fast, close, fast_period)
        cur_slow = ema_step(prev_slow, close, slow_period)
        diff = cur_fast - cur_slow

        if prev_diff is not None:
            if prev_diff <= 0.0 < diff:
                crosses.append(_cross_record(candle, "bullish", cur_fast, cur_slow, diff))
            elif prev_diff >= 0.0 > diff:
                crosses.append(_cross_record(candle, "bearish", cur_fast, cur_slow, diff))

        prev_fast, prev_slow, prev_diff = cur_fast, cur_slow, diff
        last_ts = ts
        last_close = close

    state = {
        "ema_fast": prev_fast,
        "ema_slow": prev_slow,
        "last_ts": last_ts,
        "last_close": last_close,
    }
    return crosses, state


def _cross_record(candle: dict, kind: str, fast: float, slow: float, diff: float) -> dict:
    return {
        "timestamp": candle.get("timestamp"),
        "type": kind,
        "fast_ema": round(fast, 4),
        "slow_ema": round(slow, 4),
        "close": round(float(candle["close"]), 4),
        "volume": float(candle.get("volume") or 0.0),
        "volume_diff": 0.0,
        "ema_diff": round(diff, 4),
        "score": 0,
        "max_score": 0,
        "valid": True,
        "filters": {},
    }


def update_one_candle(
    state: dict,
    candle: dict,
    fast_period: int,
    slow_period: int,
) -> dict | None:
    """
    Apply one candle to the persisted EMA state.
    Returns a cross record if a cross occurred, else ``None``.
    """
    close = float(candle["close"])
    prev_fast = state.get("ema_fast")
    prev_slow = state.get("ema_slow")

    cur_fast = ema_step(prev_fast, close, fast_period)
    cur_slow = ema_step(prev_slow, close, slow_period)

    cross = None
    if prev_fast is not None and prev_slow is not None:
        prev_diff = prev_fast - prev_slow
        cur_diff = cur_fast - cur_slow
        if prev_diff <= 0.0 < cur_diff:
            cross = _cross_record(candle, "bullish", cur_fast, cur_slow, cur_diff)
        elif prev_diff >= 0.0 > cur_diff:
            cross = _cross_record(candle, "bearish", cur_fast, cur_slow, cur_diff)

    state["ema_fast"] = cur_fast
    state["ema_slow"] = cur_slow
    state["last_ts"] = candle.get("timestamp")
    state["last_close"] = close
    return cross