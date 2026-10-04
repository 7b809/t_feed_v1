"""
Opening Range level computation.

Levels are derived from the FIRST 1-minute candle of the trading day
(09:15–09:16 IST on NSE / BSE).

Formulas
--------
    H   = opening range high
    L   = opening range low
    A   = (H + L) / 2
    D_H = |H - A|
    D_L = |A - L|

    R1 = A + D_H / 2
    S1 = A - D_L / 2
    R2 = H + D_H
    S2 = L - D_L
    R3 = R2 + D_H
    S3 = S2 - D_L
    R3_Threshold = (R2 + R3) / 2
    S3_Threshold = (S2 + S3) / 2
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def compute_opening_range_levels(first_candle: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """
    Given the day's first candle, return the full opening-range level map.

    Returns None when the candle is missing high/low or they are non-numeric.
    """
    try:
        H = float(first_candle["high"])
        L = float(first_candle["low"])
    except (KeyError, TypeError, ValueError):
        return None

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
        "H": H,
        "L": L,
        "A": A,
        "D_H": D_H,
        "D_L": D_L,
        "R1": R1,
        "S1": S1,
        "R2": R2,
        "S2": S2,
        "R3": R3,
        "S3": S3,
        "R3_Threshold": R3_Threshold,
        "S3_Threshold": S3_Threshold,
    }