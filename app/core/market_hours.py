from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from app.core.config import Settings

def market_tz(settings: Settings) -> ZoneInfo:
    return ZoneInfo(settings.market_timezone)

def local_now(settings: Settings) -> datetime:
    return datetime.now(market_tz(settings))

def _clock(value: str) -> time:
    return time.fromisoformat(value)

def is_market_hours(settings: Settings, now: datetime | None = None) -> bool:
    now = now or local_now(settings)
    if now.tzinfo is None: now = now.replace(tzinfo=market_tz(settings))
    else: now = now.astimezone(market_tz(settings))
    return now.weekday() < 5 and _clock(settings.market_open_time) <= now.time().replace(tzinfo=None) <= _clock(settings.market_close_time)

def next_open(settings: Settings, now: datetime | None = None) -> datetime:
    tz = market_tz(settings)
    now = (now or datetime.now(tz)).astimezone(tz)
    opening = _clock(settings.market_open_time)
    candidate = datetime.combine(now.date(), opening, tz)
    if now >= candidate: candidate += timedelta(days=1)
    while candidate.weekday() >= 5: candidate += timedelta(days=1)
    return candidate

def seconds_until_close(settings: Settings, now: datetime | None = None) -> float:
    tz=market_tz(settings); now=(now or datetime.now(tz)).astimezone(tz)
    close=datetime.combine(now.date(), _clock(settings.market_close_time), tz)
    return max(0.0, (close-now).total_seconds())
