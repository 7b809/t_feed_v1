# app/services/sr_calculator.py
"""
Support / Resistance level calculator (pure math; no I/O).

Config format: "<left_bars>/<right_bars>"
  left_bars  : how many bars to the LEFT of the pivot to compare against
  right_bars : how many bars to the RIGHT to confirm the pivot
               (confirmation delay — the pivot is only confirmed once
                right_bars bars have formed after it)

Canonical defaults: 15/3, 15/15, 15/30.
  * 15/3   — reactive  : fast confirmation, more pivots detected
  * 15/15  — balanced  : moderate confirmation
  * 15/30  — strong    : slow confirmation, fewer but more reliable pivots

Pivot rules (asymmetric):
  Pivot high at index i:
      high[i] >= high[j]   for all j in [i - left_bars, i - 1]
      high[i] >  high[j]   for all j in [i + 1, i + right_bars]
  Pivot low  at index i:
      low[i]  <= low[j]    for all j in [i - left_bars, i - 1]
      low[i]  <  low[j]    for all j in [i + 1, i + right_bars]

  Confirmation bar index: i + right_bars
  Confirmation time     : timestamp of the confirmation bar
  Confirmation price    : close of the confirmation bar

Each returned level carries:
    level                : "support" | "resistance"
    price                : pivot high (resistance) or pivot low (support)
    pivot_bar_index      : index in the candle list
    pivot_time           : ISO timestamp of the pivot bar
    confirmed_bar_index  : pivot_bar_index + right_bars
    confirmed_time       : ISO timestamp of the confirmation bar
    confirmed_time_price : close of the confirmation bar
    left_bars            : left window used
    right_bars           : right (confirmation) window used

Sides returned are split against the latest close:
  * resistances — pivot highs >= current price, sorted ascending (nearest first)
  * supports    — pivot lows  <= current price, sorted descending (nearest first)
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_CONFIGS: tuple[str, ...] = ("15/3", "15/15", "15/30")

_CONFIG_RE = re.compile(r"^\s*(\d+)\s*[/_\-]\s*(\d+)\s*$")


class SrCalculationError(ValueError):
    """Raised on invalid config or impossible input."""


# --------------------------------------------------------------------------- #
# Config parsing
# --------------------------------------------------------------------------- #
def parse_config(config: str) -> tuple[int, int]:
    """'15/3' | '15_3' | '15-3' | '15 / 3' → (left_bars=15, right_bars=3)."""
    if not isinstance(config, str):
        raise SrCalculationError("config must be a string like '15/3'")
    match = _CONFIG_RE.match(config)
    if not match:
        raise SrCalculationError(
            f"invalid SR config {config!r}; expected "
            f"'<left_bars>/<right_bars>' e.g. '15/3'"
        )
    left_bars = int(match.group(1))
    right_bars = int(match.group(2))
    if left_bars < 1 or left_bars > 1000:
        raise SrCalculationError("left_bars must be in 1..1000")
    if right_bars < 1 or right_bars > 500:
        raise SrCalculationError("right_bars must be in 1..500")
    return left_bars, right_bars


def config_slug(config: str) -> str:
    """Canonical '15/3' form used in storage keys and channel tags."""
    left_bars, right_bars = parse_config(config)
    return f"{left_bars}/{right_bars}"


def config_token(config: str) -> str:
    """URL-safe form for path segments: '15/3' -> '15_3'."""
    left_bars, right_bars = parse_config(config)
    return f"{left_bars}_{right_bars}"


# --------------------------------------------------------------------------- #
# Pivot detection
# --------------------------------------------------------------------------- #
def find_pivots(
    candles: list[dict],
    left_bars: int,
    right_bars: int,
) -> list[dict]:
    """
    Return all confirmed pivot highs and lows with full metadata.

    A pivot at index ``i`` is confirmed at index ``i + right_bars``. Only
    bars that have at least ``right_bars`` following bars are candidates
    (the most recent ``right_bars`` bars cannot be pivots yet).
    """
    if left_bars < 1 or right_bars < 1:
        return []
    n = len(candles)
    if n < (left_bars + right_bars + 1):
        return []

    pivots: list[dict] = []

    for i in range(left_bars, n - right_bars):
        try:
            high = float(candles[i]["high"])
            low = float(candles[i]["low"])
        except (KeyError, TypeError, ValueError):
            continue

        is_pivot_high = (
            all(
                high >= float(candles[j]["high"])
                for j in range(i - left_bars, i)
            )
            and all(
                high > float(candles[j]["high"])
                for j in range(i + 1, i + right_bars + 1)
            )
        )

        is_pivot_low = (
            all(
                low <= float(candles[j]["low"])
                for j in range(i - left_bars, i)
            )
            and all(
                low < float(candles[j]["low"])
                for j in range(i + 1, i + right_bars + 1)
            )
        )

        if not (is_pivot_high or is_pivot_low):
            continue

        confirmed_idx = i + right_bars
        confirmed_bar = candles[confirmed_idx]

        confirmed_close: float | None
        try:
            confirmed_close = round(float(confirmed_bar["close"]), 4)
        except (KeyError, TypeError, ValueError):
            confirmed_close = None

        if is_pivot_high:
            pivots.append({
                "level": "resistance",
                "price": round(high, 4),
                "pivot_bar_index": i,
                "pivot_time": candles[i].get("timestamp"),
                "confirmed_bar_index": confirmed_idx,
                "confirmed_time": confirmed_bar.get("timestamp"),
                "confirmed_time_price": confirmed_close,
                "left_bars": left_bars,
                "right_bars": right_bars,
            })

        if is_pivot_low:
            pivots.append({
                "level": "support",
                "price": round(low, 4),
                "pivot_bar_index": i,
                "pivot_time": candles[i].get("timestamp"),
                "confirmed_bar_index": confirmed_idx,
                "confirmed_time": confirmed_bar.get("timestamp"),
                "confirmed_time_price": confirmed_close,
                "left_bars": left_bars,
                "right_bars": right_bars,
            })

    return pivots


# --------------------------------------------------------------------------- #
# Compute
# --------------------------------------------------------------------------- #
def compute_sr(
    candles: list[dict],
    config: str,
    *,
    max_per_side: int = 5,
) -> dict[str, Any]:
    """
    Compute SR levels for a config against the given candles.

    Returns:
        {
          "config": "15/3",
          "left_bars": 15,
          "right_bars": 3,
          "current_price": 22500.0,
          "last_ts": "2026-11-09T15:29:00+05:30",
          "candle_count": 3375,
          "resistances": [<level>, ...],   # ascending (nearest first)
          "supports":    [<level>, ...]    # descending (nearest first)
        }

    Each <level> is:
        {
          "level": "support" | "resistance",
          "price": 43.25,
          "pivot_bar_index": 445,
          "pivot_time": "2026-11-09T09:15:00+05:30",
          "confirmed_bar_index": 448,
          "confirmed_time": "2026-11-09T09:18:00+05:30",
          "confirmed_time_price": 87.85,
          "left_bars": 15,
          "right_bars": 3,
        }
    """
    left_bars, right_bars = parse_config(config)

    if not candles:
        raise SrCalculationError(f"no candles available for config {config}")
    if len(candles) < (left_bars + right_bars + 1):
        raise SrCalculationError(
            f"need at least {left_bars + right_bars + 1} candles for "
            f"config {config}; have {len(candles)}"
        )

    current_price = float(candles[-1]["close"])
    last_ts = candles[-1].get("timestamp")

    pivots = find_pivots(candles, left_bars, right_bars)

    resistances = [
        p for p in pivots
        if p["level"] == "resistance" and p["price"] >= current_price
    ]
    supports = [
        p for p in pivots
        if p["level"] == "support" and p["price"] <= current_price
    ]

    # Nearest-first ordering.
    resistances.sort(key=lambda p: p["price"])        # closest above first
    supports.sort(key=lambda p: -p["price"])          # closest below first

    return {
        "config": f"{left_bars}/{right_bars}",
        "left_bars": left_bars,
        "right_bars": right_bars,
        "current_price": round(current_price, 4),
        "last_ts": last_ts,
        "candle_count": len(candles),
        "resistances": resistances[:max_per_side],
        "supports": supports[:max_per_side],
    }