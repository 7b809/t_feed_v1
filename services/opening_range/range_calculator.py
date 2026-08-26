"""
Opening Range level calculation.

This module calculates Opening Range support and resistance levels from
the selected market-opening candles.

The formula remains compatible with the existing Pine Script logic.

This module is stateless and does not read or modify shared runtime
state.
"""

from math import isfinite
from typing import Any

from .candle_utils import safe_float

# ============================================================
# Result Helpers
# ============================================================


def _build_empty_result(
    message: str,
    status: str = "empty",
) -> dict:
    """Builds an empty or invalid Opening Range calculation result."""
    return {
        "status": status,
        "message": message,
        "range": None,
        "levels": None,
    }


def _round_price(
    value: Any,
    decimals: int = 4,
) -> float:
    """Safely rounds a numeric price value."""
    return round(
        safe_float(value, default=0.0),
        decimals,
    )


def _get_valid_price(
    value: Any,
) -> float | None:
    """
    Converts a value to a valid finite price.

    Returns None when the value is missing, invalid, not finite, or
    non-positive.
    """
    if value is None:
        return None

    try:
        numeric_value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None

    if not isfinite(numeric_value):
        return None

    if numeric_value <= 0:
        return None

    return numeric_value


def _normalize_selected_candles(
    selected_candles: list,
) -> list:
    """
    Validates selected candles before level calculation.

    A valid candle must contain positive and finite values for:

        open
        high
        low
        close

    The candle is also rejected when:

        high is lower than low
        high is lower than open or close
        low is higher than open or close
    """
    if not isinstance(selected_candles, list):
        return []

    normalized_candles = []

    for candle in selected_candles:
        if not isinstance(candle, dict):
            continue

        open_price = _get_valid_price(candle.get("open"))

        high_price = _get_valid_price(candle.get("high"))

        low_price = _get_valid_price(candle.get("low"))

        close_price = _get_valid_price(candle.get("close"))

        if (
            open_price is None
            or high_price is None
            or low_price is None
            or close_price is None
        ):
            continue

        if high_price < low_price:
            continue

        if high_price < max(open_price, close_price):
            continue

        if low_price > min(open_price, close_price):
            continue

        normalized_candle = dict(candle)

        normalized_candle["open"] = open_price
        normalized_candle["high"] = high_price
        normalized_candle["low"] = low_price
        normalized_candle["close"] = close_price

        normalized_candles.append(normalized_candle)

    return normalized_candles


# ============================================================
# Opening Range Formula
# ============================================================


def calculate_opening_range_levels(
    selected_candles: list,
) -> dict:
    """
    Calculates Opening Range levels using the Pine Script-compatible
    formula.

    Calculation:

        average = (range high + range low) / 2

        high difference = abs(range high - average)
        low difference = abs(average - range low)

        R1 = average + high difference / 2
        S1 = average - low difference / 2

        R2 = range high + high difference
        S2 = range low - low difference

        R3 = R2 + high difference
        S3 = S2 - low difference

        R3 threshold = (R2 + R3) / 2
        S3 threshold = (S2 + S3) / 2

    Return statuses:

        success:
            Levels were calculated successfully.

        empty:
            No candles were supplied.

        invalid:
            Supplied candles did not contain usable OHLC values.
    """
    if not selected_candles:
        return _build_empty_result(
            message="No opening range candles selected.",
            status="empty",
        )

    valid_candles = _normalize_selected_candles(selected_candles)

    if not valid_candles:
        return _build_empty_result(
            message=(
                "Selected opening range candles do not contain " "valid OHLC values."
            ),
            status="invalid",
        )

    range_open = valid_candles[0]["open"]
    range_close = valid_candles[-1]["close"]

    range_high = max(candle["high"] for candle in valid_candles)

    range_low = min(candle["low"] for candle in valid_candles)

    if range_high < range_low:
        return _build_empty_result(
            message=("Opening range high is lower than opening range low."),
            status="invalid",
        )

    range_average = (range_high + range_low) / 2.0

    high_average_difference = abs(range_high - range_average)

    low_average_difference = abs(range_average - range_low)

    sub_resistance = range_average + high_average_difference / 2.0

    sub_support = range_average - low_average_difference / 2.0

    resistance_2 = range_high + high_average_difference

    support_2 = range_low - low_average_difference

    resistance_3 = resistance_2 + high_average_difference

    support_3 = support_2 - low_average_difference

    resistance_3_threshold = (resistance_2 + resistance_3) / 2.0

    support_3_threshold = (support_2 + support_3) / 2.0

    return {
        "status": "success",
        "message": ("Opening range levels calculated successfully."),
        "range": {
            "open": _round_price(range_open),
            "high": _round_price(range_high),
            "low": _round_price(range_low),
            "close": _round_price(range_close),
            "average": _round_price(range_average),
            "selected_candles_count": len(valid_candles),
            "requested_candles_count": len(selected_candles),
            "invalid_candles_count": (len(selected_candles) - len(valid_candles)),
            "first_candle_time": (valid_candles[0].get("timestamp")),
            "last_candle_time": (valid_candles[-1].get("timestamp")),
        },
        "levels": {
            # Compact names used by APIs and EMA enrichment.
            "r1": _round_price(sub_resistance),
            "s1": _round_price(sub_support),
            "r2": _round_price(resistance_2),
            "s2": _round_price(support_2),
            "r3": _round_price(resistance_3),
            "s3": _round_price(support_3),
            # Descriptive names retained for compatibility.
            "sub_resistance": _round_price(sub_resistance),
            "sub_support": _round_price(sub_support),
            "resistance2": _round_price(resistance_2),
            "support2": _round_price(support_2),
            "resistance3": _round_price(resistance_3),
            "support3": _round_price(support_3),
            # Midpoints between second and third levels.
            "r3_threshold": _round_price(resistance_3_threshold),
            "s3_threshold": _round_price(support_3_threshold),
        },
    }


# ============================================================
# Public API
# ============================================================


__all__ = [
    "calculate_opening_range_levels",
]
