from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from services.time_window_supertrend_service import (
    _is_completed,
    _normalise_candles,
    _parse_timestamp,
    _signal_id,
    calculate_supertrend,
    TimeWindowSupertrendService,
)

IST = ZoneInfo("Asia/Kolkata")


def test_normalise_candles_sorts_and_deduplicates():
    rows = [
        ["2026-11-27T15:01:00+05:30", 101, 103, 100, 102, 10, 0],
        ["2026-11-27T15:00:00+05:30", 100, 102, 99, 101, 9, 0],
        ["2026-11-27T15:01:00+05:30", 101, 104, 100, 103, 11, 0],
    ]
    candles = _normalise_candles(rows)
    assert len(candles) == 2
    assert candles[0]["close"] == 101
    assert candles[1]["close"] == 103


def test_completed_candle_grace_excludes_partial_bar():
    candle = {"timestamp": "2026-11-27T15:00:00+05:30"}
    assert _is_completed(candle, 1, datetime(2026, 11, 27, 15, 1, 10, tzinfo=IST))
    assert not _is_completed(candle, 1, datetime(2026, 11, 27, 15, 1, 5, tzinfo=IST))


def test_signal_id_is_stable_and_interval_specific():
    signal_time = datetime(2026, 11, 27, 15, 5, tzinfo=IST)
    first = _signal_id("NSE_FO|123", "5m", signal_time, "BUY")
    assert first == _signal_id("NSE_FO|123", "5m", signal_time, "BUY")
    assert first != _signal_id("NSE_FO|123", "1m", signal_time, "BUY")


def test_supertrend_returns_one_trend_per_candle_after_minimum_history():
    candles = []
    price = 100.0
    for index in range(30):
        price += 0.5 if index < 20 else -1.0
        candles.append({
            "timestamp": (datetime(2026, 11, 27, 9, 15, tzinfo=IST) + timedelta(minutes=index)).isoformat(),
            "open": price,
            "high": price + 0.6,
            "low": price - 0.6,
            "close": price,
        })
    trends = calculate_supertrend(candles, period=10, multiplier=3.0, use_atr=True)
    assert trends is not None
    assert len(trends) == len(candles)
    assert set(trends).issubset({-1, 1})



def test_supertrend_detects_a_bearish_trend_flip():
    candles = []
    for index in range(12):
        price = 100.0 if index < 11 else 90.0
        candles.append({
            "timestamp": (datetime(2026, 11, 27, 9, 15, tzinfo=IST) + timedelta(minutes=index)).isoformat(),
            "open": price,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price,
        })
    trends = calculate_supertrend(candles, period=10, multiplier=3.0, use_atr=True)
    assert trends is not None
    assert trends[-2] == 1
    assert trends[-1] == -1


def test_notification_messages_include_both_chart_links_and_split_detail(monkeypatch, tmp_path):
    import services.time_window_supertrend_service as module

    monkeypatch.setattr(
        module,
        "format_option_chart_links",
        lambda isolated, opposite: [
            '<a href="https://example.test/isolated">Isolated instrument chart</a>',
            '<a href="https://example.test/opposite">Opposite instrument chart</a>',
        ],
    )
    service = TimeWindowSupertrendService(root=str(tmp_path))
    record = {
        "instrument_key": "NSE_FO|123",
        "trading_symbol": "NIFTY TEST CE",
        "leg_role": "isolated",
        "interval": "1m",
        "signal": "BUY",
        "signal_time_ist": "2026-11-27T15:01:00+05:30",
        "close_price": 125.5,
        "detected_at_ist": "2026-11-27T15:01:10+05:30",
        "signal_id": "abc123",
        "instruments": {
            "isolated": {"instrument_key": "NSE_FO|123"},
            "opposite": {"instrument_key": "NSE_FO|456"},
        },
    }
    group_message, private_message = service._make_notifications(record)
    assert "Isolated instrument chart" in group_message
    assert "Opposite instrument chart" in group_message
    assert "125.50" not in group_message
    assert "125.50" in private_message
    assert "Isolated instrument chart" in private_message
    assert "Opposite instrument chart" in private_message

def test_signal_persistence_is_idempotent(tmp_path):
    from datetime import date
    import json

    service = TimeWindowSupertrendService(root=str(tmp_path))
    day_root = service._day_root(date(2026, 11, 27))
    state = service._load_state(day_root)
    record = {
        "signal_id": "stable-signal-id",
        "interval": "1m",
        "trade_date_ist": "2026-11-27",
        "signal": "BUY",
    }
    assert service._persist_signal(day_root, state, record) is True
    assert service._persist_signal(day_root, state, record) is False

    index = json.loads((day_root / "signals.json").read_text(encoding="utf-8"))
    saved = day_root / "signals" / "1m" / "stable-signal-id.json"
    assert len(index["signals"]) == 1
    assert json.loads(saved.read_text(encoding="utf-8"))["signal_id"] == "stable-signal-id"

def test_normalise_candles_discards_non_finite_ohlc():
    rows = [
        ["2026-11-27T15:00:00+05:30", 100, 101, 99, 100, 1, 0],
        ["2026-11-27T15:01:00+05:30", 100, float("nan"), 99, 100, 1, 0],
    ]
    candles = _normalise_candles(rows)
    assert len(candles) == 1
    assert candles[0]["close"] == 100
