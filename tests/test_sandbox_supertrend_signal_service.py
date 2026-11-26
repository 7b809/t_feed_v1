from __future__ import annotations

import json

from sandbox_services.supertrend_signal_service import SandboxSupertrendSignalService


def _record():
    return {
        "trade_date_ist": "2026-11-27",
        "isolation_id": "2026-11-27__NSE_FO|111",
        "instrument_key": "NSE_FO|111",
        "trading_symbol": "NIFTY 26000 CE",
        "option_type": "CE",
        "strike": 26000,
        "expiry": "2026-12-03",
        "opposite_leg": {
            "instrument_key": "NSE_FO|222",
            "trading_symbol": "NIFTY 26000 PE",
            "option_type": "PE",
            "strike": 26000,
            "expiry": "2026-12-03",
        },
    }


def _snap(key, interval, signals):
    return {
        "instrument_key": key,
        "interval": interval,
        "today_ist_date": "2026-11-27",
        "signals": signals,
        "snapshot_updated_at_ist": "2026-11-27T09:31:05+05:30",
    }


def test_first_bullish_signal_targets_its_own_instrument_and_saves_once(tmp_path, monkeypatch):
    service = SandboxSupertrendSignalService(root=tmp_path)
    notified = []
    monkeypatch.setattr(service, "_notify", lambda document: notified.append(document) or True)

    record = _record()
    snapshots = {
        "isolated": _snap("NSE_FO|111", "1m", [{
            "signal": "buy",
            "confirmed_time": "2026-11-27T09:30:00+05:30",
            "confirmed_time_price": 260.5,
        }]),
        "opposite": _snap("NSE_FO|222", "1m", [{
            "signal": "sell",
            "confirmed_time": "2026-11-27T09:31:00+05:30",
            "confirmed_time_price": 150.0,
        }]),
    }
    pivot_path = (
        tmp_path / "pivots" / "20261127" / "isolated"
        / "NSE_FO_111" / "pivot_levels_1m.json"
    )
    pivot_path.parent.mkdir(parents=True)
    pivot_path.write_text(json.dumps({
        "source_candle_timestamp": "2026-11-27T09:29:00+05:30",
        "levels": {"S1": 230.0, "S2": 200.0, "S3": 170.0},
    }), encoding="utf-8")

    result = service.process_snapshots(
        record, "1m", snapshots,
        ltp_provider=lambda key: {
            "ltp": 250.0 if key == "NSE_FO|111" else 120.0,
            "stale": False,
            "age_seconds": 1,
            "received_at": "2026-11-27T09:31:06+05:30",
        },
    )
    assert result["state"] == "processed"
    assert result["target_instrument_key"] == "NSE_FO|111"
    assert result["notification_sent"] is True
    assert len(notified) == 1

    saved = json.loads((tmp_path / "sandbox_services" / "20261127" /
                        "2026-11-27__NSE_FO_111_1m.json").read_text())
    assert saved["first_signal"]["signal"] == "buy"
    assert saved["target"]["entry_price"] == 250.0
    assert saved["calculation"]["target_price"] == 310.0
    assert saved["calculation"]["stop_loss"] == 175.0
    assert saved["execution"]["order_placed"] is False

    again = service.process_snapshots(record, "1m", snapshots, ltp_provider=lambda key: 250.0)
    assert again["state"] == "already_processed"
    assert len(notified) == 1


def test_first_bearish_signal_targets_opposite_instrument(tmp_path, monkeypatch):
    service = SandboxSupertrendSignalService(root=tmp_path)
    monkeypatch.setattr(service, "_notify", lambda document: False)
    record = _record()
    snapshots = {
        "isolated": _snap("NSE_FO|111", "15m", [{
            "signal": "sell",
            "confirmed_time": "2026-11-27T09:30:00+05:30",
            "confirmed_time_price": 260.5,
        }]),
        "opposite": _snap("NSE_FO|222", "15m", []),
    }
    result = service.process_snapshots(
        record, "15m", snapshots,
        ltp_provider=lambda key: {"ltp": 150.0, "stale": False},
    )
    assert result["state"] == "processed"
    assert result["target_instrument_key"] == "NSE_FO|222"


def test_earliest_confirmed_signal_across_both_legs_wins(tmp_path, monkeypatch):
    service = SandboxSupertrendSignalService(root=tmp_path)
    monkeypatch.setattr(service, "_notify", lambda document: False)
    record = _record()
    snapshots = {
        "isolated": _snap("NSE_FO|111", "1m", [{
            "signal": "sell", "confirmed_time": "2026-11-27T09:30:00+05:30",
            "confirmed_time_price": 250,
        }]),
        "opposite": _snap("NSE_FO|222", "1m", [{
            "signal": "buy", "confirmed_time": "2026-11-27T09:29:00+05:30",
            "confirmed_time_price": 120,
        }]),
    }
    result = service.process_snapshots(
        record, "1m", snapshots,
        ltp_provider=lambda key: {"ltp": 120.0, "stale": False},
    )
    assert result["state"] == "processed"
    assert result["target_instrument_key"] == "NSE_FO|222"
