"""Shared EMA math used by the batch and live EMA cross jobs."""

from __future__ import annotations

from typing import Any


def to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compute_ema(values: list[float], period: int) -> list[float | None]:
    """SMA-seeded EMA. Returns ``None`` for indices before warm-up."""
    n = len(values)
    if n < period or period <= 0:
        return [None] * n

    k = 2.0 / (period + 1.0)
    emas: list[float | None] = [None] * n

    seed = sum(values[:period]) / period
    emas[period - 1] = seed

    for i in range(period, n):
        prev = emas[i - 1]
        emas[i] = values[i] * k + float(prev) * (1.0 - k)

    return emas


def classify_cross(prev_diff: float, curr_diff: float) -> str | None:
    """Return 'bullish', 'bearish', or None given fast-minus-slow diffs."""
    if prev_diff <= 0 and curr_diff > 0:
        return "bullish"
    if prev_diff >= 0 and curr_diff < 0:
        return "bearish"
    return None