from services.isolation_supertrend_backfill_service import evaluate_pre_isolation_price_trend


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



def test_price_recovery_uses_net_move_not_signal_majority_or_last_signal():
    result = evaluate_pre_isolation_price_trend(
        _loaded_backfill([
            {
                "signal": "buy",
                "confirmed_time": "2026-11-25T09:15:00+05:30",
                "confirmed_time_price": 246.75,
            },
            {
                "signal": "sell",
                "confirmed_time": "2026-11-25T09:35:00+05:30",
                "confirmed_time_price": 250.0,
            },
            {
                "signal": "sell",
                "confirmed_time": "2026-11-25T09:48:00+05:30",
                "confirmed_time_price": 255.0,
            },
        ]),
        "2026-11-25T10:00:00+05:30",
        [{"timestamp": "2026-11-25T09:59:00+05:30", "close": 260.0}],
    )
    assert result["eligible_signal_count"] == 3
    assert result["latest_signal_direction"] == "sell"
    assert result["direction"] == "BULLISH"
    assert result["price_change"] == 13.25


def test_price_recovery_neutral_threshold_blocks_small_move():
    result = evaluate_pre_isolation_price_trend(
        _loaded_backfill([{
            "signal": "buy",
            "confirmed_time": "2026-11-25T09:15:00+05:30",
            "confirmed_time_price": 250.0,
        }]),
        "2026-11-25T10:00:00+05:30",
        [{"timestamp": "2026-11-25T09:59:00+05:30", "close": 250.2}],
        min_move_percent=0.1,
    )
    assert result["status"] == "no_valid_signal"
    assert result["direction"] is None


def test_websocket_snapshot_cache_exact_key_and_deduplication():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from services.supertrend_snapshot_cache import clear_supertrend_snapshot_cache, get_supertrend_snapshot, update_supertrend_snapshot_cache
    clear_supertrend_snapshot_cache()
    day = datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat()
    snap = {'event':'snapshot','instrument_key':'NSE_FO|1','indicator':'supertrend','data_scope':'today_only','today_ist_date':day,'signals':[{'signal':'buy','confirmed_time':day+'T09:15:00+05:30','confirmed_time_price':100}]}
    assert update_supertrend_snapshot_cache('NSE_FO|1', snap)
    sig = {'event':'supertrend_signal','instrument_key':'NSE_FO|1','data_scope':'today_only','today_ist_date':day,'signal':'sell','confirmed_time':day+'T09:20:00+05:30','confirmed_time_price':110}
    update_supertrend_snapshot_cache('NSE_FO|1', sig)
    update_supertrend_snapshot_cache('NSE_FO|1', sig)
    assert get_supertrend_snapshot('NSE_FO|1',day)['signal_count'] == 2
    assert get_supertrend_snapshot('NSE_FO|2',day)['status'] == 'snapshot_unavailable'
    clear_supertrend_snapshot_cache()
