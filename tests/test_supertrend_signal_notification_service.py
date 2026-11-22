from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from services import supertrend_signal_notification_service as notifier

IST = ZoneInfo("Asia/Kolkata")


def _record():
    return {
        "trade_date_ist": "2026-11-23",
        "isolation_id": "2026-11-23__NSE_FO|64473",
        "instrument_key": "NSE_FO|64473",
        "trading_symbol": "NIFTY 25500 CE 23 NOV 26",
        "isolation_touch_time": "2026-11-23T09:18:00+05:30",
        "isolated_option_ltp": 120.0,
        "opposite_leg": {
            "instrument_key": "NSE_FO|64474",
            "trading_symbol": "NIFTY 25500 PE 23 NOV 26",
            "ltp_at_isolation": 110.0,
        },
    }


def _signal(at: str, direction="BULLISH"):
    return {
        "event": "supertrend_signal",
        "signal": direction,
        "timestamp": at,
        "timeframe": "1m",
    }


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("ISOLATED_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ISOLATION_SUPERTREND_SIGNAL_CUTOFF_HOUR", "9")
    monkeypatch.setenv("ISOLATION_SUPERTREND_SIGNAL_CUTOFF_MINUTE", "40")
    monkeypatch.setattr(notifier, "_latest_prices", lambda record, payload: {
        "isolated": 123.45, "opposite": 98.76,
    })
    sent = []
    monkeypatch.setattr(notifier, "send_group_html", lambda message: sent.append(message) or True)
    return sent


def test_sends_every_signal_from_isolation_through_0940_then_stops(monkeypatch, tmp_path):
    sent = _setup(monkeypatch, tmp_path)
    record = _record()

    assert notifier.notify_isolated_supertrend_signal(
        record, _signal("2026-11-23T09:20:00+05:30")
    )
    assert notifier.notify_isolated_supertrend_signal(
        record, _signal("2026-11-23T09:39:59+05:30", "BEARISH")
    )
    assert not notifier.notify_isolated_supertrend_signal(
        record, _signal("2026-11-23T09:40:01+05:30")
    )
    assert len(sent) == 2
    assert "NIFTY 25500 CE 23 NOV 26" in sent[0]
    assert "BULLISH" in sent[0]
    assert "09:20:00 AM IST" in sent[0]
    assert "123.45" in sent[0]
    assert "98.76" in sent[0]
    assert "BEARISH" in sent[1]


def test_sends_only_first_post_cutoff_signal_when_no_early_signal_was_sent(monkeypatch, tmp_path):
    sent = _setup(monkeypatch, tmp_path)
    record = _record()

    assert notifier.notify_isolated_supertrend_signal(
        record, _signal("2026-11-23T09:41:00+05:30", "BEARISH")
    )
    assert not notifier.notify_isolated_supertrend_signal(
        record, _signal("2026-11-23T09:42:00+05:30")
    )
    assert len(sent) == 1
    assert "BEARISH" in sent[0]
    assert "09:41:00 AM IST" in sent[0]


def test_ignores_signal_before_isolation_and_opposite_leg(monkeypatch, tmp_path):
    sent = _setup(monkeypatch, tmp_path)
    record = _record()

    assert not notifier.notify_isolated_supertrend_signal(
        record, _signal("2026-11-23T09:17:59+05:30")
    )
    assert not notifier.notify_isolated_supertrend_signal(
        record, _signal("2026-11-23T09:22:00+05:30"), role="opposite"
    )
    assert sent == []


def test_failed_early_delivery_does_not_block_first_post_cutoff_fallback(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    record = _record()
    calls = []

    def flaky_send(message):
        calls.append(message)
        return len(calls) > 1

    monkeypatch.setattr(notifier, "send_group_html", flaky_send)
    assert not notifier.notify_isolated_supertrend_signal(
        record, _signal("2026-11-23T09:30:00+05:30")
    )
    assert notifier.notify_isolated_supertrend_signal(
        record, _signal("2026-11-23T09:41:00+05:30")
    )
    assert not notifier.notify_isolated_supertrend_signal(
        record, _signal("2026-11-23T09:42:00+05:30")
    )
    assert len(calls) == 3


def test_duplicate_signal_is_not_sent_twice(monkeypatch, tmp_path):
    sent = _setup(monkeypatch, tmp_path)
    record = _record()
    signal = _signal("2026-11-23T09:25:00+05:30")

    assert notifier.notify_isolated_supertrend_signal(record, signal)
    assert not notifier.notify_isolated_supertrend_signal(record, signal)
    assert len(sent) == 1



def test_current_minute_archive_signal_before_isolation_is_sent_with_age(monkeypatch, tmp_path):
    sent = _setup(monkeypatch, tmp_path)
    record = _record()
    record["isolation_touch_time"] = "2026-11-23T09:21:40+05:30"
    contract_folder = tmp_path / "contract"
    contract_folder.mkdir()
    feed_file = contract_folder / "supertrend_feed.jsonl"
    feed_file.write_text(
        '{"received_at_ist":"2026-11-23T09:21:02+05:30",'
        '"instrument_key":"NSE_FO|64473",'
        '"payload":{"event":"supertrend_signal","signal":"BULLISH",'
        '"timestamp":"2026-11-23T09:21:00+05:30","timeframe":"1m"}}\\n',
        encoding="utf-8",
    )

    from services import option_chain_history
    monkeypatch.setattr(option_chain_history, "_find_contract_folder", lambda key, day: contract_folder)

    assert notifier.notify_current_minute_supertrend_signal(
        record, now=datetime.fromisoformat("2026-11-23T09:21:45+05:30")
    )
    assert len(sent) == 1
    assert "Signal time:" in sent[0]
    assert "Signal age: <b>45 seconds ago</b>" in sent[0]
    assert "Signal-to-isolation gap: <b>40 seconds before isolation</b>" in sent[0]
    assert "09:21:00 AM IST" in sent[0]

    # The same event arriving again on the live socket must be deduplicated.
    assert not notifier.notify_isolated_supertrend_signal(
        record, _signal("2026-11-23T09:21:00+05:30")
    )
    assert len(sent) == 1


def test_current_minute_catchup_does_not_replay_previous_minute(monkeypatch, tmp_path):
    sent = _setup(monkeypatch, tmp_path)
    record = _record()
    record["isolation_touch_time"] = "2026-11-23T09:21:40+05:30"
    contract_folder = tmp_path / "contract"
    contract_folder.mkdir()
    feed_file = contract_folder / "supertrend_feed.jsonl"
    feed_file.write_text(
        '{"received_at_ist":"2026-11-23T09:20:59+05:30",'
        '"instrument_key":"NSE_FO|64473",'
        '"payload":{"event":"supertrend_signal","signal":"BULLISH",'
        '"timestamp":"2026-11-23T09:20:00+05:30","timeframe":"1m"}}\\n',
        encoding="utf-8",
    )

    from services import option_chain_history
    monkeypatch.setattr(option_chain_history, "_find_contract_folder", lambda key, day: contract_folder)

    assert not notifier.notify_current_minute_supertrend_signal(
        record, now=datetime.fromisoformat("2026-11-23T09:21:45+05:30")
    )
    assert sent == []


def test_notification_wrapper_contains_unexpected_exceptions(monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("simulated formatting failure")

    monkeypatch.setattr(notifier, "_notify_isolated_supertrend_signal", explode)
    assert not notifier.notify_isolated_supertrend_signal(
        _record(), _signal("2026-11-23T09:20:00+05:30")
    )
