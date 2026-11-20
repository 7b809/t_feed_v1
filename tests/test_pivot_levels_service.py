from __future__ import annotations

import json

from app.services.pivot_levels_service import PivotLevelsService


def test_calculate_opening_range_pivots_and_previous_session_midpoint():
    today = [
        {"ts": "2026-11-20T09:16:00+05:30", "open": 95, "high": 98, "low": 94, "close": 97},
        {"ts": "2026-11-20T09:15:00+05:30", "open": 92, "high": 100, "low": 90, "close": 95},
    ]
    previous = [
        {"ts": "2026-11-19T09:15:00+05:30", "high": 105, "low": 92},
        {"ts": "2026-11-19T09:16:00+05:30", "high": 110, "low": 95},
    ]

    record = PivotLevelsService.calculate(
        "NSE_FO|123", "2026-11-20", today, "2026-11-19", previous
    )

    assert record is not None
    assert record["opening_range_candle"]["timestamp"] == "2026-11-20T09:15:00+05:30"
    assert record["pivot_levels"] == {
        "R1": 100.0, "R2": 105.0, "R3": 110.0,
        "AVG": 95.0, "S1": 90.0, "S2": 85.0, "S3": 80.0,
        "R1_Diff": 5.0, "S1_Diff": 5.0, "range": 10.0,
    }
    assert record["previous_session"]["high"] == 110.0
    assert record["previous_session"]["low"] == 92.0
    assert record["yesterday_avg"] == 101.0
    assert record["yesterday_avg_points"] == 101.0
    assert record["previous_session"]["average_points"] == 101.0


def test_update_merges_instruments_and_reads_local_pivot_levels(tmp_path):
    service = PivotLevelsService(tmp_path)
    first = {
        "instrument_key": "ISO", "trade_date_ist": "2026-11-20",
        "pivot_levels": {"S1": 90}, "yesterday_avg": 100,
    }
    second = {
        "instrument_key": "OPP", "trade_date_ist": "2026-11-20",
        "pivot_levels": {"S1": 50}, "yesterday_avg": 60,
    }

    service.update(first)
    service.update(second)

    saved = json.loads((tmp_path / "pivot_levels.json").read_text(encoding="utf-8"))
    instruments = saved["trade_dates"]["2026-11-20"]["instruments"]
    assert set(instruments) == {"ISO", "OPP"}
    assert service.get("ISO", "2026-11-20")["yesterday_avg"] == 100
    assert service.get("OPP", "2026-11-20")["pivot_levels"]["S1"] == 50
