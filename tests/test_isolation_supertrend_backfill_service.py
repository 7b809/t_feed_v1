import json

from services.isolation_supertrend_backfill_service import (
    evaluate_pre_isolation_price_trend,
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



def _loaded_backfill(signals):
    return {
        "status": "loaded",
        "data": {"signals": signals},
    }


def test_price_recovery_uses_confirmed_market_price_and_isolation_candle():
    backfill = _loaded_backfill([
        {
            "signal": "buy",
            "price": 168.8954,
            "confirmed_time": "2026-11-25T09:15:00+05:30",
            "confirmed_time_price": 246.75,
        },
        {
            "signal": "sell",
            "price": 292.2288,
            "confirmed_time": "2026-11-25T10:10:00+05:30",
            "confirmed_time_price": 257.15,
        },
    ])
    result = evaluate_pre_isolation_price_trend(
        backfill,
        "2026-11-25T10:00:00+05:30",
        [
            {"timestamp": "2026-11-25T09:59:00+05:30", "close": 260.0},
            {"timestamp": "2026-11-25T10:00:00+05:30", "close": 250.0},
        ],
    )
    assert result["status"] == "valid_bullish"
    assert result["direction"] == "BULLISH"
    assert result["eligible_signal_count"] == 1
    assert result["baseline"]["confirmed_time_price"] == 246.75
    assert result["isolation_candle"]["close"] == 260.0
    assert result["signal_evidence"][0]["supertrend_line_price"] == 168.8954


def test_price_recovery_classifies_negative_net_move_as_bearish():
    result = evaluate_pre_isolation_price_trend(
        _loaded_backfill([{
            "signal": "sell",
            "price": 300.0,
            "confirmed_time": "2026-11-25T09:20:00+05:30",
            "confirmed_time_price": 250.0,
        }]),
        "2026-11-25T10:00:00+05:30",
        [{"timestamp": "2026-11-25T09:59:00+05:30", "close": 240.0}],
    )
    assert result["status"] == "valid_bearish"
    assert result["direction"] == "BEARISH"


def test_price_recovery_requires_fully_completed_candle():
    result = evaluate_pre_isolation_price_trend(
        _loaded_backfill([{
            "signal": "buy",
            "confirmed_time": "2026-11-25T09:15:00+05:30",
            "confirmed_time_price": 246.75,
        }]),
        "2026-11-25T10:00:00+05:30",
        [{"timestamp": "2026-11-25T10:00:00+05:30", "close": 260.0}],
    )
    assert result["status"] == "candle_unavailable"
    assert result["direction"] is None


def test_price_recovery_rejects_future_only_signals():
    result = evaluate_pre_isolation_price_trend(
        _loaded_backfill([{
            "signal": "sell",
            "confirmed_time": "2026-11-25T10:10:00+05:30",
            "confirmed_time_price": 257.15,
        }]),
        "2026-11-25T10:00:00+05:30",
        [{"timestamp": "2026-11-25T09:59:00+05:30", "close": 260.0}],
    )
    assert result["status"] == "no_pre_isolation_signals"
    assert result["direction"] is None
