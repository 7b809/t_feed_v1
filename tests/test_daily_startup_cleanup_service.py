from datetime import datetime
from zoneinfo import ZoneInfo

from services.daily_startup_cleanup_service import (
    _configured_targets,
    run_daily_startup_cleanup,
)

IST = ZoneInfo("Asia/Kolkata")


def test_cleanup_is_skipped_outside_configured_startup_window(monkeypatch):
    monkeypatch.setenv("DAILY_CLEANUP_ENABLED", "true")
    result = run_daily_startup_cleanup(
        datetime(2026, 11, 18, 9, 1, tzinfo=IST)
    )

    assert result["state"] == "outside_cleanup_window"


def test_cleanup_targets_reject_traversal_and_source_directories(monkeypatch):
    monkeypatch.setenv(
        "DAILY_CLEANUP_DIRS",
        "data,logs,../,services,core,.git,../../outside",
    )

    names = {path.name for path in _configured_targets()}

    # DATA_DIR remains protected until a verified MEGA backup completes.
    assert names == {"logs"}


def test_cleanup_can_be_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("DAILY_CLEANUP_ENABLED", "false")

    result = run_daily_startup_cleanup(
        datetime(2026, 11, 18, 2, 0, tzinfo=IST)
    )

    assert result == {"enabled": False, "state": "disabled"}
