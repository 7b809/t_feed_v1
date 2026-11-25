import json

from services.isolation_supertrend_backfill_service import (
    load_isolated_supertrend_backfill,
)


def _record():
    return {
        "instrument_key": "NSE_FO|47835",
        "strike": 25700.0,
        "option_type": "CE",
        "trade_date_ist": "2026-11-25",
    }


def _document():
    return {
        "indicator": "supertrend",
        "parameters": {
            "atr_period": 10,
            "atr_multiplier": 3.0,
            "change_atr": True,
            "source": "hl2",
        },
        "today_ist_date": "2026-11-25",
        "data_scope": "today_only",
        "signals": [
            {
                "signal": "buy",
                "price": 168.8954,
                "confirmed_time": "2026-11-25T09:15:00+05:30",
                "confirmed_time_price": 246.75,
            }
        ],
        "state": {"trend": -1, "supertrend": 310.2539},
        "signal_count": 1,
        "instrument_key": "NSE_FO|47835",
    }


def test_loads_supertrend_backfill_for_isolated_contract(tmp_path, monkeypatch):
    import services.isolation_supertrend_backfill_service as loader

    contract = tmp_path / "25700_CE"
    contract.mkdir()
    (contract / "supertrend.json").write_text(
        json.dumps(_document()), encoding="utf-8"
    )
    monkeypatch.setattr(loader, "ISOLATION_SUPERTREND_BACKFILL_DIR", str(tmp_path))

    result = load_isolated_supertrend_backfill(_record())

    assert result["status"] == "loaded"
    assert result["is_backfill"] is True
    assert result["source"] == "mega_supertrend_json"
    assert result["folder"] == "25700_CE"
    assert result["signal_count"] == 1
    assert result["data"]["signals"][0]["signal"] == "buy"


def test_missing_supertrend_file_is_non_fatal(tmp_path, monkeypatch):
    import services.isolation_supertrend_backfill_service as loader

    monkeypatch.setattr(loader, "ISOLATION_SUPERTREND_BACKFILL_DIR", str(tmp_path))

    result = load_isolated_supertrend_backfill(_record())

    assert result["status"] == "missing"
    assert result["data"] is None
    assert result["signal_count"] == 0


def test_rejects_wrong_instrument_data(tmp_path, monkeypatch):
    import services.isolation_supertrend_backfill_service as loader

    contract = tmp_path / "25700_CE"
    contract.mkdir()
    document = _document()
    document["instrument_key"] = "NSE_FO|DIFFERENT"
    (contract / "supertrend.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    monkeypatch.setattr(loader, "ISOLATION_SUPERTREND_BACKFILL_DIR", str(tmp_path))

    result = load_isolated_supertrend_backfill(_record())

    assert result["status"] == "instrument_mismatch"
    assert result["data"] is None
