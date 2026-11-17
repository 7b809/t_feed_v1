from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.services.market_session import (
    is_market_session_open,
    market_session_status,
    seconds_until_session_boundary,
)

IST = ZoneInfo("Asia/Kolkata")


def config(**overrides):
    values = {
        "live_feed_start_hour": 9,
        "live_feed_start_minute": 15,
        "live_feed_end_hour": 15,
        "live_feed_end_minute": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_market_session_start_is_inclusive_and_close_is_exclusive(monkeypatch):
    monkeypatch.setenv("MARKET_HOLIDAYS", "")
    cfg = config()
    assert not is_market_session_open(cfg, datetime(2026, 11, 17, 9, 14, 59, tzinfo=IST))
    assert is_market_session_open(cfg, datetime(2026, 11, 17, 9, 15, 0, tzinfo=IST))
    assert is_market_session_open(cfg, datetime(2026, 11, 17, 15, 29, 59, tzinfo=IST))
    assert not is_market_session_open(cfg, datetime(2026, 11, 17, 15, 30, 0, tzinfo=IST))


def test_weekends_and_configured_holidays_are_closed(monkeypatch):
    monkeypatch.setenv("MARKET_HOLIDAYS", "2026-11-17")
    cfg = config()
    assert not is_market_session_open(cfg, datetime(2026, 11, 14, 10, 0, tzinfo=IST))
    assert not is_market_session_open(cfg, datetime(2026, 11, 17, 10, 0, tzinfo=IST))


def test_after_close_status_is_recovery_mode(monkeypatch):
    monkeypatch.setenv("MARKET_HOLIDAYS", "")
    status = market_session_status(
        config(), datetime(2026, 11, 17, 17, 0, tzinfo=IST)
    )
    assert status["state"] == "RECOVERY_MODE"
    assert status["is_open"] is False
    assert status["session_start"] == "09:15"
    assert status["session_end"] == "15:30"


def test_next_open_skips_weekend(monkeypatch):
    monkeypatch.setenv("MARKET_HOLIDAYS", "")
    now = datetime(2026, 11, 13, 17, 0, tzinfo=IST)  # Friday
    assert seconds_until_session_boundary(config(), now) == (
        datetime(2026, 11, 16, 9, 15, tzinfo=IST) - now
    ).total_seconds()
