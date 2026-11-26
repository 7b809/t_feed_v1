import json
from datetime import datetime
from zoneinfo import ZoneInfo

from services.isolated_pivot_manager import (
    IsolatedPivotLevelsService,
    latest_completed_candle_start,
    parse_pivot_intervals,
)

IST = ZoneInfo("Asia/Kolkata")


def _record():
    return {
        "trade_date_ist": "2026-11-23",
        "isolation_id": "2026-11-23__NSE_FO|12345",
        "instrument_key": "NSE_FO|12345",
        "trading_symbol": "NIFTY 25500 CE",
        "strike": 25500,
        "option_type": "CE",
        "expiry": "2026-11-26",
        "opposite_leg": {
            "instrument_key": "NSE_FO|12346",
            "trading_symbol": "NIFTY 25500 PE",
            "strike": 25500,
            "option_type": "PE",
            "expiry": "2026-11-26",
        },
    }


def _candle(timestamp, high=120.0, low=100.0):
    return {
        "timestamp": timestamp,
        "open": 110.0,
        "high": high,
        "low": low,
        "close": 115.0,
        "volume": 1000,
        "open_interest": 0,
    }


def test_default_intervals_are_one_and_fifteen_minutes(monkeypatch):
    monkeypatch.delenv("ISOLATED_PIVOT_INTERVALS", raising=False)
    assert parse_pivot_intervals() == [1, 15]


def test_interval_parser_ignores_unsupported_and_duplicates():
    assert parse_pivot_intervals("15m,1,15,5,abc") == [1, 15]


def test_completed_candles_align_to_0915_session_anchor():
    now = datetime(2026, 11, 23, 9, 30, 5, tzinfo=IST)
    assert latest_completed_candle_start("2026-11-23", 1, now) == datetime(
        2026, 11, 23, 9, 29, tzinfo=IST
    )
    assert latest_completed_candle_start("2026-11-23", 15, now) == datetime(
        2026, 11, 23, 9, 15, tzinfo=IST
    )


def test_no_candle_is_due_before_first_bar_closes():
    now = datetime(2026, 11, 23, 9, 15, 3, tzinfo=IST)
    assert latest_completed_candle_start("2026-11-23", 1, now) is None
    assert latest_completed_candle_start("2026-11-23", 15, now) is None


def test_calculates_both_intervals_for_isolated_and_opposite_legs(tmp_path):
    calls = []

    class FakeFetcher:
        def fetch_intraday_candles(self, key, unit, interval):
            calls.append((key, unit, interval))
            if interval == "1":
                stamp = "2026-11-23T09:29:00+05:30"
            else:
                stamp = "2026-11-23T09:15:00+05:30"
            return [_candle(stamp)]

    service = IsolatedPivotLevelsService(
        "test-token", root=tmp_path, fetcher=FakeFetcher(),
        intervals=[1, 15], requests_per_second=0, max_retries=0,
    )
    now = datetime(2026, 11, 23, 9, 30, 5, tzinfo=IST)
    result = service.process_cycle(_record(), now=now)

    assert result["state"] == "completed"
    assert result["intervals"]["1m"]["computed"] == 2
    assert result["intervals"]["15m"]["computed"] == 2
    assert {(key, interval) for key, _, interval in calls} == {
        ("NSE_FO|12345", "1"),
        ("NSE_FO|12345", "15"),
        ("NSE_FO|12346", "1"),
        ("NSE_FO|12346", "15"),
    }

    expected = [
        tmp_path / "pivots" / "20261123" / "isolated" / "NSE_FO_12345" / "pivot_levels_1m.json",
        tmp_path / "pivots" / "20261123" / "isolated" / "NSE_FO_12345" / "pivot_levels_15m.json",
        tmp_path / "pivots" / "20261123" / "opposite" / "NSE_FO_12346" / "pivot_levels_1m.json",
        tmp_path / "pivots" / "20261123" / "opposite" / "NSE_FO_12346" / "pivot_levels_15m.json",
    ]
    for path in expected:
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["instrument_key"] in {"NSE_FO|12345", "NSE_FO|12346"}
        assert document["interval_label"] in {"1m", "15m"}
        assert set(document["levels"]) == {
            "R3", "R2", "R1", "AVG", "S1", "S2", "S3",
            "R3_Threshold", "S3_Threshold",
        }
        assert document["thresholds"] == {
            "R3_Threshold": 135.0,
            "S3_Threshold": 85.0,
        }
        assert document["levels"]["R3_Threshold"] == 135.0
        assert document["levels"]["S3_Threshold"] == 85.0
        assert document["source_candle"]["high"] == 120.0


def test_existing_completed_interval_file_is_not_fetched_again(tmp_path):
    class FailFetcher:
        def fetch_intraday_candles(self, *args, **kwargs):
            raise AssertionError("valid existing file should be reused")

    service = IsolatedPivotLevelsService(
        "test-token", root=tmp_path, fetcher=FailFetcher(),
        intervals=[15], requests_per_second=0, max_retries=0,
    )
    now = datetime(2026, 11, 23, 9, 30, 5, tzinfo=IST)
    leg = {
        "leg_role": "isolated",
        "instrument_key": "NSE_FO|12345",
    }
    target = datetime(2026, 11, 23, 9, 15, tzinfo=IST)
    path = service._folder(leg, "2026-11-23") / "pivot_levels_15m.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "trade_date_ist": "2026-11-23",
        "instrument_key": "NSE_FO|12345",
        "interval_minutes": 15,
        "source_candle_timestamp": target.isoformat(),
        "levels": {
            name: 1 for name in (
                "R3", "R2", "R1", "AVG", "S1", "S2", "S3",
                "R3_Threshold", "S3_Threshold",
            )
        },
    }), encoding="utf-8")

    assert service._process_leg(leg, "2026-11-23", 15, target) == "already_present"
