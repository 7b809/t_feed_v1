# app/services/levels_calculator.py
#
# Pure math for first-candle pivot levels. No I/O.
#
# Formulas (source: user spec):
#   fc = first 1-min candle (09:15 → 09:16 IST)
#   H  = fc.high
#   L  = fc.low
#   A  = (H + L) / 2
#   D_H = |H - A|
#   D_L = |A - L|
#   R1 = A + D_H / 2
#   S1 = A - D_L / 2
#   R2 = H + D_H
#   S2 = L - D_L
#   R3 = R2 + D_H
#   S3 = S2 - D_L
#   R3_Threshold = (R2 + R3) / 2
#   S3_Threshold = (S2 + S3) / 2

from __future__ import annotations

from typing import Any


class LevelsCalculationError(ValueError):
    """Raised when the inputs cannot produce valid pivot levels."""


def calculate_levels(high: float, low: float) -> dict[str, float]:
    """
    Compute all pivot levels from the first-candle high/low.

    Returns a dict with keys:
        H, L, A, D_H, D_L,
        R1, S1, R2, S2, R3, S3,
        R3_Threshold, S3_Threshold
    """
    try:
        H = float(high)
        L = float(low)
    except (TypeError, ValueError) as exc:
        raise LevelsCalculationError(
            f"first-candle high/low must be numeric (high={high!r}, low={low!r})"
        ) from exc

    if H < L:
        raise LevelsCalculationError(
            f"first-candle high ({H}) cannot be less than low ({L})"
        )
    if H <= 0 or L <= 0:
        raise LevelsCalculationError(
            f"first-candle high/low must be positive (high={H}, low={L})"
        )

    A = (H + L) / 2.0
    D_H = abs(H - A)
    D_L = abs(A - L)

    R1 = A + D_H / 2.0
    S1 = A - D_L / 2.0
    R2 = H + D_H
    S2 = L - D_L
    R3 = R2 + D_H
    S3 = S2 - D_L
    R3_Threshold = (R2 + R3) / 2.0
    S3_Threshold = (S2 + S3) / 2.0

    return {
        "H": round(H, 4),
        "L": round(L, 4),
        "A": round(A, 4),
        "D_H": round(D_H, 4),
        "D_L": round(D_L, 4),
        "R1": round(R1, 4),
        "S1": round(S1, 4),
        "R2": round(R2, 4),
        "S2": round(S2, 4),
        "R3": round(R3, 4),
        "S3": round(S3, 4),
        "R3_Threshold": round(R3_Threshold, 4),
        "S3_Threshold": round(S3_Threshold, 4),
    }


def summarize_levels(levels: dict[str, Any]) -> dict[str, Any]:
    """Compact summary used for heartbeat-style payloads and logs."""
    return {
        "A": levels.get("A"),
        "R1": levels.get("R1"),
        "R2": levels.get("R2"),
        "R3": levels.get("R3"),
        "S1": levels.get("S1"),
        "S2": levels.get("S2"),
        "S3": levels.get("S3"),
        "R3_Threshold": levels.get("R3_Threshold"),
        "S3_Threshold": levels.get("S3_Threshold"),
    }