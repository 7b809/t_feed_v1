"""Shared NSE market-session clock for all downstream WebSocket clients.

All timestamps are interpreted in Asia/Kolkata. Weekends and dates listed in
MARKET_HOLIDAYS (comma-separated YYYY-MM-DD values) are treated as closed.
This is an operational gate, not an exchange calendar feed: keep the holiday
list current in deployment configuration.
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _holiday_dates() -> set[date]:
    result: set[date] = set()
    for raw in os.getenv("MARKET_HOLIDAYS", "").split(","):
        value = raw.strip()
        if not value:
            continue
        try:
            result.add(date.fromisoformat(value))
        except ValueError:
            continue
    return result


def _minutes(config) -> tuple[int, int]:
    start = int(getattr(config, "live_feed_start_hour", 9)) * 60 + int(
        getattr(config, "live_feed_start_minute", 15)
    )
    end = int(getattr(config, "live_feed_end_hour", 15)) * 60 + int(
        getattr(config, "live_feed_end_minute", 30)
    )
    if not 0 <= start < end <= 24 * 60:
        raise ValueError("Invalid market session window")
    return start, end


def is_trading_day(day: date, holidays: set[date] | None = None) -> bool:
    return day.weekday() < 5 and day not in (holidays if holidays is not None else _holiday_dates())


def is_market_session_open(config, now: datetime | None = None) -> bool:
    local = (now or datetime.now(IST)).astimezone(IST)
    if not is_trading_day(local.date()):
        return False
    start, end = _minutes(config)
    minute = local.hour * 60 + local.minute
    return start <= minute < end


def seconds_until_session_boundary(config, now: datetime | None = None) -> float:
    """Seconds until close when open, otherwise until the next trading open."""
    local = (now or datetime.now(IST)).astimezone(IST)
    start, end = _minutes(config)
    if is_market_session_open(config, local):
        boundary = local.replace(
            hour=end // 60, minute=end % 60, second=0, microsecond=0
        )
        return max(0.1, (boundary - local).total_seconds())

    holidays = _holiday_dates()
    candidate = local.date()
    opening = local.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
    if candidate <= local.date() and opening <= local:
        candidate += timedelta(days=1)
    for _ in range(370):
        if is_trading_day(candidate, holidays):
            target = datetime.combine(
                candidate, time(start // 60, start % 60), tzinfo=IST
            )
            if target > local:
                return max(0.1, (target - local).total_seconds())
        candidate += timedelta(days=1)
    return 24 * 60 * 60


def market_session_status(config, now: datetime | None = None) -> dict:
    local = (now or datetime.now(IST)).astimezone(IST)
    start, end = _minutes(config)
    opened = is_market_session_open(config, local)
    if opened:
        state, label, reason = "LIVE_SESSION", "Live market session", "Trading session is open"
    elif not is_trading_day(local.date()):
        state, label, reason = "RECOVERY_MODE", "Non-trading day", "Weekend or configured market holiday"
    elif local.hour * 60 + local.minute < start:
        state, label, reason = "STANDBY", "Pre-market standby", "Waiting for the market session to open"
    else:
        state, label, reason = "RECOVERY_MODE", "Market closed · recovery mode", "Live connections are paused after market close"

    return {
        "state": state,
        "label": label,
        "reason": reason,
        "is_open": opened,
        "trade_date_ist": local.date().isoformat(),
        "current_time_ist": local.isoformat(),
        "timezone": "Asia/Kolkata",
        "session_start": f"{start // 60:02d}:{start % 60:02d}",
        "session_end": f"{end // 60:02d}:{end % 60:02d}",
        "seconds_until_boundary": round(seconds_until_session_boundary(config, local), 1),
        "holiday_calendar_source": "MARKET_HOLIDAYS environment variable + weekday rule",
    }
