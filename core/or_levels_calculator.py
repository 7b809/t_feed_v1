# core/or_levels_calculator.py
"""Pure Opening Range (OR) level calculator.

Input  : the first 1-minute candle of the day (09:15 IST).
Output : dict with R1..R3, S1..S3, AVG, thresholds, deltas.

Formula (from docs/range_levels.txt):

    H, L       = first-candle high, low
    R1         = H
    S1         = L
    AVG        = (H + L) / 2
    R1_Diff    = (H - L) / 2
    S1_Diff    = (H - L) / 2       # equal to R1_Diff
    R2         = H + R1_Diff
    R3         = R2 + R1_Diff      # == H + (H - L)
    S2         = L - S1_Diff
    S3         = S2 - S1_Diff      # == L - (H - L)
    R3_Threshold = (R2 + R3) / 2
    S3_Threshold = (S2 + S3) / 2

D_H = R3 - R2   (upper-half delta — persisted for shape compatibility)
D_L = S2 - S3   (lower-half delta)
"""

from __future__ import annotations

from typing import Optional


class ORLevelCalculationError(ValueError):
    """Raised when the input candle is missing or malformed."""


def compute_or_levels(first_candle: dict) -> dict:
    """Compute OR levels from the first 1-minute candle of the day."""
    if not first_candle:
        raise ORLevelCalculationError("first_candle is empty")

    try:
        high = float(first_candle["high"])
        low = float(first_candle["low"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ORLevelCalculationError(
            f"first_candle missing high/low: {first_candle}"
        ) from exc

    if high <= 0 or low <= 0 or high < low:
        raise ORLevelCalculationError(
            f"Invalid candle high={high} low={low}"
        )

    rng = high - low
    avg = (high + low) / 2.0
    r1_diff = rng / 2.0
    s1_diff = rng / 2.0

    r1 = high
    s1 = low
    r2 = high + r1_diff
    r3 = r2 + r1_diff
    s2 = low - s1_diff
    s3 = s2 - s1_diff
    r3_threshold = (r2 + r3) / 2.0
    s3_threshold = (s2 + s3) / 2.0

    return {
        "H": high,
        "L": low,
        "A": avg,
        "R1": r1,
        "S1": s1,
        "R2": r2,
        "S2": s2,
        "R3": r3,
        "S3": s3,
        "R3_Threshold": r3_threshold,
        "S3_Threshold": s3_threshold,
        "D_H": r3 - r2,
        "D_L": s2 - s3,
        "range": rng,
        "avg": avg,
        "r1_diff": r1_diff,
        "s1_diff": s1_diff,
    }


def pick_first_candle(candles: list[dict]) -> Optional[dict]:
    """Return the chronologically-earliest candle (defensive sort)."""
    if not candles:
        return None
    return min(candles, key=lambda c: c.get("timestamp", ""))