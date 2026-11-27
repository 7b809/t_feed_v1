from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from services.time_window_supertrend_service import (
    _is_completed,
    _normalise_candles,
    _parse_timestamp,
    _signal_id,
    calculate_supertrend,
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


