"""
Detect opening-range touches from a finalized candle.

The opening-range levels belong to the OPTION contract itself (each
contract has its own first-candle-derived OR). A touch is when:

    Resistance (r1 / r2 / r3):
        previous close < level <= candle.high
        i.e. price approached the level from below and reached it this candle

    Support (s1 / s2 / s3):
        previous close > level >= candle.low
        i.e. price approached the level from above and reached it this candle

Returns a list of touch descriptors (one per level hit).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def detect_touches(
    candle: Dict[str, Any],
    prev_close: Optional[float],
    levels: Dict[str, float],
) -> List[Dict[str, Any]]:
    """
    Return the list of touch descriptors for a single candle.

    Each descriptor:
        {
            "level": "r1" | "r2" | "r3" | "s1" | "s2" | "s3",
            "level_value": float,
            "trigger_field": "high" | "low",
            "trigger_price": float,
        }
    """
    if not isinstance(candle, dict) or not levels or prev_close is None:
        return []

    try:
        high = float(candle["high"])
        low = float(candle["low"])
    except (KeyError, TypeError, ValueError):
        return []

    touches: List[Dict[str, Any]] = []

    # Resistance touches (approach from below)
    for name in ("r1", "r2", "r3"):
        lvl = levels.get(name)
        if lvl is None:
            continue
        try:
            lvl_f = float(lvl)
        except (TypeError, ValueError):
            continue
        if prev_close < lvl_f <= high:
            touches.append(
                {
                    "level": name,
                    "level_value": lvl_f,
                    "trigger_field": "high",
                    "trigger_price": high,
                }
            )

    # Support touches (approach from above)
    for name in ("s1", "s2", "s3"):
        lvl = levels.get(name)
        if lvl is None:
            continue
        try:
            lvl_f = float(lvl)
        except (TypeError, ValueError):
            continue
        if prev_close > lvl_f >= low:
            touches.append(
                {
                    "level": name,
                    "level_value": lvl_f,
                    "trigger_field": "low",
                    "trigger_price": low,
                }
            )

    return touches