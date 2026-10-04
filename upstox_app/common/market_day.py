"""
market_day.py

Helpers for reasoning about the "last market day".

Without a full trading-holiday calendar, we only handle weekends:
Saturday and Sunday are treated as non-market days. National holidays
are not accounted for — a holiday day simply has zero candles in the
historical response, and the crossover logic skips empty days naturally.

The historical Upstox endpoint only returns data up to yesterday, so the
"last market day" for historical purposes is the most recent weekday
strictly before today.
"""
from datetime import date, timedelta
from typing import Optional


def is_weekend(d: date) -> bool:
    """Saturday=5, Sunday=6."""
    return d.weekday() >= 5


def last_market_day(ref: Optional[date] = None) -> date:
    """
    Most recent market day strictly before `ref` (default today).

    Examples (ref = today):
      Mon -> Fri (skips Sun)
      Tue -> Mon
      Sat -> Fri
      Sun -> Fri
    """
    if ref is None:
        ref = date.today()
    d = ref - timedelta(days=1)
    while is_weekend(d):
        d -= timedelta(days=1)
    return d


def last_market_day_on_or_before(ref: Optional[date] = None) -> date:
    """
    Most recent market day on or before `ref` (default today).
    Used when we want to know if `ref` itself is a trading day.
    """
    if ref is None:
        ref = date.today()
    d = ref
    while is_weekend(d):
        d -= timedelta(days=1)
    return d