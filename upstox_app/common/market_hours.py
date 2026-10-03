"""
upstox_app/market_hours.py
Indian equity market hours helper (IST).

Used to distinguish "socket closed because market is closed"
from "socket closed because something is broken".
"""
import os
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# IST = UTC+05:30
IST = timezone(timedelta(hours=5, minutes=30))


def _parse_hhmm(value: str, fallback: time) -> time:
    try:
        hh, mm = value.strip().split(":")
        return time(int(hh), int(mm))
    except Exception:  # noqa: BLE001
        return fallback


# Defaults: 09:00 → 15:40 IST, Mon–Fri
MARKET_OPEN: time = _parse_hhmm(os.getenv("MARKET_OPEN_TIME", "09:00"), time(9, 0))
MARKET_CLOSE: time = _parse_hhmm(os.getenv("MARKET_CLOSE_TIME", "15:40"), time(15, 40))


def now_ist() -> datetime:
    """Current IST time."""
    return datetime.now(IST)


def is_weekday(now: Optional[datetime] = None) -> bool:
    """Mon–Fri only (Sat=5, Sun=6)."""
    n = now or now_ist()
    return n.weekday() < 5


def is_market_open(now: Optional[datetime] = None) -> bool:
    """
    True if the market is currently open (Mon–Fri, within [MARKET_OPEN, MARKET_CLOSE] IST).
    Holidays are NOT handled — callers should treat upstream errors as
    "possibly holiday" and use this only as a hint for logging.
    """
    n = now or now_ist()
    if not is_weekday(n):
        return False
    t = n.timetz().replace(tzinfo=None)
    return MARKET_OPEN <= t <= MARKET_CLOSE


def market_state_text(now: Optional[datetime] = None) -> str:
    """Human-readable reason for the current market state."""
    n = now or now_ist()
    if not is_weekday(n):
        return f"weekend ({n.strftime('%A')})"
    if n.timetz().replace(tzinfo=None) < MARKET_OPEN:
        return f"pre-market (opens at {MARKET_OPEN.strftime('%H:%M')} IST)"
    if n.timetz().replace(tzinfo=None) > MARKET_CLOSE:
        return f"post-market (closed at {MARKET_CLOSE.strftime('%H:%M')} IST)"
    return "regular session"