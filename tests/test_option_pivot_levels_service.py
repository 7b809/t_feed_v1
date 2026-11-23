import json
from datetime import datetime
from zoneinfo import ZoneInfo

from services import option_pivot_levels_service as pivots

IST = ZoneInfo("Asia/Kolkata")


def _contract(key="NSE_FO|12345"):
    return {
        "instrument_key": key,
        "trading_symbol": "NIFTY 25500 CE",
        "strike_price": 25500,
        "instrument_type": "CE",
        "index_folder": "nifty",
        "index_instrument_key": "NSE_INDEX|Nifty 50",
        "index_canonical": "NIFTY",
    }


def _candle(timestamp, high=22000, low=21900):
    return {
        "timestamp": timestamp,
        "open": low + 60,
        "high": high,
        "low": low,
        "close": low + 80,
    }


def test_default_intervals_are_one_and_fifteen_minutes(monkeypatch):
    monkeypatch.delenv("OPTION_PIVOT_INTERVALS", raising=False)
    assert pivots.parse_intervals() == [1, 15]


def test_interval_parser_validates_deduplicates_and_sorts():
    assert pivots.parse_intervals("15,1,5m,15,99,abc") == [1, 5, 15]


def test_completed_candle_alignment_uses_0915_session_anchor():
    now = datetime(2026, 11, 23, 9, 30, 5, tzinfo=IST)
    assert pivots.latest_completed_candle_start("2026-11-23", 15, now) == datetime(
        2026, 11, 23, 9, 15, tzinfo=IST
    )
    assert pivots.latest_completed_candle_start("2026-11-23", 1, now) == datetime(
        2026, 11, 23, 9, 29, tzinfo=IST
    )


def test_no_interval_is_due_before_first_candle_closes():
    now = datetime(2026, 11, 23, 9, 15, 3, tzinfo=IST)
    assert pivots.latest_completed_candle_start("2026-11-23", 1, now) is None
    assert pivots.latest_completed_candle_start("2026-11-23", 15, now) is None


def test_computes_one_and_fifteen_minute_files_independently(monkeypatch, tmp_path):
    calls = []

    class FakeFetcher:
        def __init__(self, token):
            assert token == "test-token"

        def fetch_intraday_candles(self, instrument_key, unit, interval):
            calls.append((instrument_key, unit, interval))
            if interval == "1":
                return [
                    _candle("2026-11-23T09:29:00+05:30", 22020, 21980),
                    _candle("2026-11-23T09:15:00+05:30", 22000, 21900),
                ]
            assert interval == "15"
            return [
                _candle("2026-11-23T09:15:00+05:30", 22100, 21800),
            ]

    monkeypatch.setattr(pivots, "CandleFetcher", FakeFetcher)
    service = pivots.OptionInstrumentPivotLevelsService(
        root=tmp_path, requests_per_second=0, max_retries=0, intervals=[1, 15]
    )
    now = datetime(2026, 11, 23, 9, 30, 5, tzinfo=IST)
    result = service.process_contracts(
        [_contract()], "test-token", "2026-11-23", now=now
    )

    opening = json.loads(next(tmp_path.rglob("pivot_levels.json")).read_text(encoding="utf-8"))
    one = json.loads(next(tmp_path.rglob("pivot_levels_1m.json")).read_text(encoding="utf-8"))
    fifteen_path = next(tmp_path.rglob("pivot_levels_15m.json"))
    fifteen = json.loads(fifteen_path.read_text(encoding="utf-8"))

    assert result["intervals"]["1m"]["computed"] == 1
    assert result["intervals"]["15m"]["computed"] == 1
    # The original opening-candle file remains stable for compatibility.
    assert opening["source_candle_timestamp"] == "2026-11-23T09:15:00+05:30"
    assert one["interval_minutes"] == 1
    assert one["source_candle_timestamp"] == "2026-11-23T09:29:00+05:30"
    assert fifteen["interval_minutes"] == 15
    assert fifteen["source_candle_timestamp"] == "2026-11-23T09:15:00+05:30"
    assert set(one["levels"]) == set(pivots.LEVEL_KEYS)
    assert set(fifteen["levels"]) == set(pivots.LEVEL_KEYS)
    assert {call[2] for call in calls} == {"1", "15"}


def test_does_not_replace_levels_with_an_incomplete_or_missing_candle(monkeypatch, tmp_path):
    class FakeFetcher:
        def __init__(self, token):
            pass

        def fetch_intraday_candles(self, instrument_key, unit, interval):
            # Target is 09:30 for a 15m interval; only an older candle exists.
            return [_candle("2026-11-23T09:15:00+05:30")]

    monkeypatch.setattr(pivots, "CandleFetcher", FakeFetcher)
    service = pivots.OptionInstrumentPivotLevelsService(
        root=tmp_path, requests_per_second=0, max_retries=0, intervals=[15]
    )
    now = datetime(2026, 11, 23, 9, 46, 0, tzinfo=IST)
    result = service.process_contracts(
        [_contract()], "test-token", "2026-11-23", now=now
    )
    assert result["intervals"]["15m"]["failed"] == 1
    assert list(tmp_path.rglob("pivot_levels_15m.json")) == []


def test_existing_interval_timestamp_is_not_fetched_again(monkeypatch, tmp_path):
    calls = []

    class FakeFetcher:
        def __init__(self, token):
            pass

        def fetch_intraday_candles(self, *args, **kwargs):
            calls.append(args)
            raise AssertionError("existing interval file should be skipped")

    monkeypatch.setattr(pivots, "CandleFetcher", FakeFetcher)
    contract = _contract()
    folder = pivots._contract_folder(tmp_path, contract, "20261123")
    folder.mkdir(parents=True)
    target = datetime(2026, 11, 23, 9, 15, tzinfo=IST)
    doc = {
        "trade_date_ist": "2026-11-23",
        "instrument_key": "NSE_FO|12345",
        "interval_minutes": 15,
        "source_candle_timestamp": target.isoformat(),
        "levels": {name: 1 for name in pivots.LEVEL_KEYS},
    }
    (folder / "pivot_levels_15m.json").write_text(json.dumps(doc), encoding="utf-8")

    service = pivots.OptionInstrumentPivotLevelsService(
        root=tmp_path, requests_per_second=0, max_retries=0, intervals=[15]
    )
    now = datetime(2026, 11, 23, 9, 30, 5, tzinfo=IST)
    result = service.process_contracts(
        [contract], "test-token", "2026-11-23", now=now
    )
    assert result["intervals"]["15m"]["already_present"] == 1
    assert calls == []


def test_one_contract_failure_does_not_stop_remaining_contracts(monkeypatch, tmp_path):
    class FakeFetcher:
        def __init__(self, token):
            pass

        def fetch_intraday_candles(self, instrument_key, *args, **kwargs):
            if instrument_key == "NSE_FO|bad":
                raise pivots.CandleFetchError("provider returned no candles")
            return [_candle("2026-11-23T09:15:00+05:30")]

    monkeypatch.setattr(pivots, "CandleFetcher", FakeFetcher)
    service = pivots.OptionInstrumentPivotLevelsService(
        root=tmp_path, requests_per_second=0, max_retries=0, intervals=[15]
    )
    now = datetime(2026, 11, 23, 9, 30, 5, tzinfo=IST)
    result = service.process_contracts(
        [_contract("NSE_FO|bad"), _contract("NSE_FO|good")],
        "test-token", "2026-11-23", now=now,
    )
    assert result["intervals"]["15m"]["failed"] == 1
    assert result["intervals"]["15m"]["computed"] == 1


def test_contracts_are_processed_concurrently(monkeypatch, tmp_path):
    import threading
    import time

    lock = threading.Lock()
    active = 0
    max_active = 0

    class FakeFetcher:
        def __init__(self, token):
            pass

        def fetch_intraday_candles(self, instrument_key, *args, **kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            return [_candle("2026-11-23T09:15:00+05:30")]

    monkeypatch.setattr(pivots, "CandleFetcher", FakeFetcher)
    service = pivots.OptionInstrumentPivotLevelsService(
        root=tmp_path, requests_per_second=0, max_retries=0,
        max_workers=4, intervals=[15],
    )
    contracts = [_contract(f"NSE_FO|{index}") for index in range(8)]
    now = datetime(2026, 11, 23, 9, 30, 5, tzinfo=IST)
    result = service.process_contracts(
        contracts, "test-token", "2026-11-23", now=now
    )
    assert result["intervals"]["15m"]["computed"] == 8
    assert max_active > 1
