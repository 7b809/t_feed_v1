from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from services import daily_profit_guard_service as guard_module


class FakeLockCollection:
    def __init__(self):
        self.documents = {}

    def find_one(self, query):
        return self.documents.get(query["_id"])

    def update_one(self, query, update, upsert=False):
        key = query["_id"]
        if key not in self.documents and upsert:
            self.documents[key] = dict(update["$setOnInsert"])
        return None


def fixed_clock(day=23):
    return lambda: datetime(2026, 11, day, 12, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


def configure_guard(monkeypatch, *, target=2000.0, include_unrealised=True,
                    enabled=True, lock_until_next_day=True, fail_closed=True):
    monkeypatch.setattr(guard_module, "DAILY_PROFIT_TARGET", target)
    monkeypatch.setattr(guard_module, "DAILY_PROFIT_INCLUDE_UNREALISED", include_unrealised)
    monkeypatch.setattr(guard_module, "DAILY_PROFIT_GUARD_ENABLED", enabled)
    monkeypatch.setattr(guard_module, "DAILY_PROFIT_LOCKOUT_UNTIL_NEXT_DAY", lock_until_next_day)
    monkeypatch.setattr(guard_module, "DAILY_PROFIT_GUARD_FAIL_CLOSED", fail_closed)


def test_aggregates_all_positions_including_closed_zero_quantity_rows(monkeypatch):
    configure_guard(monkeypatch)
    rows = [
        {"quantity": 0, "realised": 2500, "unrealised": 0},
        {"quantity": 65, "realised": 100, "unrealised": -700},
    ]
    service = guard_module.DailyProfitGuardService(
        positions_loader=lambda: rows,
        lock_collection=FakeLockCollection(),
        clock=fixed_clock(),
    )
    result = service.get_today_pnl()
    assert result["realised_pnl"] == 2600
    assert result["unrealised_pnl"] == -700
    assert result["total_pnl"] == 1900
    assert result["new_entries_allowed"] is True
    assert result["positions_count"] == 2


def test_target_hit_persists_daily_lock_even_after_unrealised_pnl_drops(monkeypatch):
    configure_guard(monkeypatch, target=2000)
    collection = FakeLockCollection()
    rows = [{"quantity": 0, "realised": 2100, "unrealised": 0}]
    service = guard_module.DailyProfitGuardService(
        positions_loader=lambda: rows,
        lock_collection=collection,
        clock=fixed_clock(),
    )
    first = service.get_today_pnl()
    assert first["target_reached"] is True
    assert first["lockout_active"] is True
    assert first["new_entries_allowed"] is False
    assert "2026-11-23" in collection.documents

    rows[:] = [{"quantity": 65, "realised": 1000, "unrealised": -900}]
    later = service.get_today_pnl()
    assert later["total_pnl"] == 100
    assert later["lockout_active"] is True
    assert later["new_entries_allowed"] is False


def test_unrealised_can_be_excluded_from_guard_threshold(monkeypatch):
    configure_guard(monkeypatch, target=2000, include_unrealised=False)
    service = guard_module.DailyProfitGuardService(
        positions_loader=lambda: [{"realised": 500, "unrealised": 5000}],
        lock_collection=FakeLockCollection(),
        clock=fixed_clock(),
    )
    result = service.get_today_pnl()
    assert result["total_pnl"] == 5500
    assert result["guard_pnl"] == 500
    assert result["new_entries_allowed"] is True


def test_lock_is_scoped_to_ist_calendar_date(monkeypatch):
    configure_guard(monkeypatch, target=2000)
    collection = FakeLockCollection()
    collection.documents["2026-11-23"] = {"_id": "2026-11-23", "locked": True}
    service = guard_module.DailyProfitGuardService(
        positions_loader=lambda: [{"realised": 0, "unrealised": 0}],
        lock_collection=collection,
        clock=fixed_clock(day=24),
    )
    result = service.get_today_pnl()
    assert result["trade_date"] == "2026-11-24"
    assert result["new_entries_allowed"] is True


def test_invalid_pnl_value_fails_instead_of_assuming_zero(monkeypatch):
    configure_guard(monkeypatch)
    service = guard_module.DailyProfitGuardService(
        positions_loader=lambda: [{"realised": "unknown", "unrealised": 0}],
        lock_collection=FakeLockCollection(),
        clock=fixed_clock(),
    )
    with pytest.raises(guard_module.DailyProfitGuardError):
        service.get_today_pnl()


def test_target_reached_blocks_entry_with_status_403(monkeypatch):
    configure_guard(monkeypatch, target=100, lock_until_next_day=False)
    service = guard_module.DailyProfitGuardService(
        positions_loader=lambda: [{"realised": 150, "unrealised": 0}],
        lock_collection=FakeLockCollection(),
        clock=fixed_clock(),
    )
    with pytest.raises(guard_module.DailyProfitGuardError) as error:
        service.assert_new_entries_allowed()
    assert error.value.code == "DAILY_PROFIT_TARGET_REACHED"
    assert error.value.status_code == 403


def test_disabled_guard_does_not_request_positions(monkeypatch):
    configure_guard(monkeypatch, enabled=False)
    service = guard_module.DailyProfitGuardService(
        positions_loader=lambda: (_ for _ in ()).throw(AssertionError("must not load")),
        lock_collection=FakeLockCollection(),
        clock=fixed_clock(),
    )
    assert service.assert_new_entries_allowed()["status"] == "disabled"
