from datetime import datetime

from services import option_pivot_levels_service as pivots


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


def test_computes_and_saves_only_seven_opening_pivot_levels(monkeypatch, tmp_path):
    class FakeFetcher:
        def __init__(self, token):
            assert token == "test-token"

        def fetch_intraday_candles(self, instrument_key, unit, interval):
            assert instrument_key == "NSE_FO|12345"
            assert unit == "minutes"
            assert interval == "1"
            # Deliberately reverse ordered: the earliest candle must be selected.
            return [
                {
                    "timestamp": "2026-11-23T09:16:00+05:30",
                    "open": 22010, "high": 22020, "low": 21980, "close": 22000,
                },
                {
                    "timestamp": "2026-11-23T09:15:00+05:30",
                    "open": 21960, "high": 22000, "low": 21900, "close": 21980,
                },
            ]

    monkeypatch.setattr(pivots, "CandleFetcher", FakeFetcher)
    service = pivots.OptionInstrumentPivotLevelsService(
        root=tmp_path, requests_per_second=0, max_retries=0
    )
    result = service.process_contracts([_contract()], "test-token", "2026-11-23")

    assert result["computed"] == 1
    assert result["failed"] == 0
    path = next(tmp_path.rglob("pivot_levels.json"))
    import json
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert saved["source_candle_timestamp"] == "2026-11-23T09:15:00+05:30"
    assert saved["levels"] == {
        "R3": 22100.0,
        "R2": 22050.0,
        "R1": 22000.0,
        "AVG": 21950.0,
        "S1": 21900.0,
        "S2": 21850.0,
        "S3": 21800.0,
    }
    assert set(saved["levels"]) == set(pivots.LEVEL_KEYS)
    assert "candles" not in saved


def test_existing_today_pivot_is_not_fetched_again(monkeypatch, tmp_path):
    calls = []

    class FakeFetcher:
        def __init__(self, token):
            pass

        def fetch_intraday_candles(self, *args, **kwargs):
            calls.append(args)
            raise AssertionError("existing pivot should be skipped")

    monkeypatch.setattr(pivots, "CandleFetcher", FakeFetcher)
    service = pivots.OptionInstrumentPivotLevelsService(
        root=tmp_path, requests_per_second=0, max_retries=0
    )
    contract = _contract()
    folder = pivots._contract_folder(tmp_path, contract, "20261123")
    folder.mkdir(parents=True)
    (folder / "pivot_levels.json").write_text(
        '{"trade_date_ist":"2026-11-23","instrument_key":"NSE_FO|12345",'
        '"levels":{"R3":1,"R2":2,"R1":3,"AVG":4,"S1":5,"S2":6,"S3":7}}',
        encoding="utf-8",
    )

    result = service.process_contracts([contract], "test-token", "2026-11-23")
    assert result["already_present"] == 1
    assert calls == []


def test_rejects_yesterdays_candle_for_today_file(monkeypatch, tmp_path):
    class FakeFetcher:
        def __init__(self, token):
            pass

        def fetch_intraday_candles(self, *args, **kwargs):
            return [{
                "timestamp": "2026-11-20T09:15:00+05:30",
                "open": 100, "high": 110, "low": 90, "close": 105,
            }]

    monkeypatch.setattr(pivots, "CandleFetcher", FakeFetcher)
    service = pivots.OptionInstrumentPivotLevelsService(
        root=tmp_path, requests_per_second=0, max_retries=0
    )
    result = service.process_contracts([_contract()], "test-token", "2026-11-23")

    assert result["failed"] == 1
    assert list(tmp_path.rglob("pivot_levels.json")) == []


def test_one_contract_failure_does_not_stop_remaining_contracts(monkeypatch, tmp_path):
    class FakeFetcher:
        def __init__(self, token):
            pass

        def fetch_intraday_candles(self, instrument_key, *args, **kwargs):
            if instrument_key == "NSE_FO|bad":
                raise pivots.CandleFetchError("provider returned no candles")
            return [{
                "timestamp": "2026-11-23T09:15:00+05:30",
                "open": 100, "high": 110, "low": 90, "close": 105,
            }]

    monkeypatch.setattr(pivots, "CandleFetcher", FakeFetcher)
    service = pivots.OptionInstrumentPivotLevelsService(
        root=tmp_path, requests_per_second=0, max_retries=0
    )
    result = service.process_contracts(
        [_contract("NSE_FO|bad"), _contract("NSE_FO|good")],
        "test-token",
        "2026-11-23",
    )

    assert result["failed"] == 1
    assert result["computed"] == 1
