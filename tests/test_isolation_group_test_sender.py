from __future__ import annotations

from services import isolation_telegram_service as telegram


def test_existing_isolation_test_is_labelled_and_group_only(monkeypatch):
    monkeypatch.setattr(telegram, "TELEGRAM_GROUP_ENABLED", True)
    monkeypatch.setattr(telegram, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(telegram, "TELEGRAM_GROUP_CHAT_ID", "-100123")
    monkeypatch.setattr(telegram, "TELEGRAM_CHAT_ID", "legacy-chat")
    record = {
        "trade_date_ist": "2026-11-20",
        "isolation_id": "2026-11-20__NSE_FO|64473",
        "status": "ACTIVE",
        "bias": "CE",
        "trigger": {
            "index": "NSE_INDEX|Nifty 50",
            "canonical": "NIFTY",
            "side": "R3",
            "level_name": "R3_Threshold",
            "level": 25475.925,
            "index_ltp": 25485.1,
        },
        "timings": {
            "touch_time_ist": "2026-11-20T09:18:50.846790+05:30",
            "created_at_ist": "2026-11-20T09:18:51.436893+05:30",
            "execution_mode_decided_at": "2026-11-20T09:18:50.846790+05:30",
        },
        "execution": {"mode": "STANDARD", "reason": "price in range"},
        "market_context": {"nifty_ltp": 25485.1},
        "instruments": {
            "isolated": {
                "instrument_key": "NSE_FO|64473",
                "trading_symbol": "NIFTY 25500 CE 23 NOV 26",
                "exchange_token": "64473",
                "strike": 25500,
                "option_type": "CE",
                "expiry": "2026-11-23",
                "lot_size": 65,
                "tick_size": 5,
                "exchange": "NSE",
                "segment": "NSE_FO",
                "ltp_at_isolation": 119.5,
            },
            "opposite": {
                "instrument_key": "NSE_FO|64474",
                "trading_symbol": "NIFTY 25500 PE 23 NOV 26",
                "exchange_token": "64474",
                "strike": 25500,
                "option_type": "PE",
                "expiry": "2026-11-23",
                "lot_size": 65,
                "tick_size": 5,
                "exchange": "NSE",
                "segment": "NSE_FO",
            },
        },
        "source_record": {"isolation_backfilled": False},
    }
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

    def fake_post(url, json, timeout):
        captured.update(url=url, payload=json)
        return Response()

    monkeypatch.setattr(telegram.requests, "post", fake_post)

    assert telegram.send_existing_isolation_test(record) is True
    assert captured["payload"]["chat_id"] == "-100123"
    assert captured["payload"]["text"].startswith(
        "🧪 <b>TEST MESSAGE · EXISTING ISOLATION DATA</b>"
    )
    assert "NIFTY 25500 CE 23 NOV 26" in captured["payload"]["text"]
    assert "NIFTY 25500 PE 23 NOV 26" in captured["payload"]["text"]
    assert "legacy-chat" not in str(captured["payload"])


def test_existing_isolation_test_respects_group_disabled(monkeypatch):
    monkeypatch.setattr(telegram, "TELEGRAM_GROUP_ENABLED", False)
    monkeypatch.setattr(telegram, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(telegram, "TELEGRAM_GROUP_CHAT_ID", "-100123")
    calls = []
    monkeypatch.setattr(telegram.requests, "post", lambda *a, **k: calls.append(True))

    assert telegram.send_existing_isolation_test({"source_record": {}}) is False
    assert calls == []
