from __future__ import annotations

from services import isolation_telegram_service as telegram


def _record():
    return {
        "trade_date_ist": "2026-11-20",
        "instrument_key": "NSE_FO|64473",
        "trading_symbol": "NIFTY 25500 CE 23 NOV 26",
        "exchange_token": "64473",
        "name": "NIFTY",
        "strike": 25500.0,
        "option_type": "CE",
        "expiry": "2026-11-23",
        "lot_size": 65,
        "tick_size": 5.0,
        "exchange": "NSE",
        "segment": "NSE_FO",
        "bias": "CE",
        "trigger_index": "NSE_INDEX|Nifty 50",
        "trigger_index_canonical": "NIFTY",
        "trigger_side": "R3",
        "trigger_level_name": "R3_Threshold",
        "trigger_level": 25475.925,
        "trigger_value": 25485.1,
        "nifty_ltp_at_isolation": 25485.1,
        "nifty_ltp_source": "feed_ltp",
        "isolation_touch_time": "2026-11-20T09:18:50.846790+05:30",
        "isolation_created_at": "2026-11-20T09:18:51.436893+05:30",
        "execution_mode_decided_at": "2026-11-20T09:18:50.846790+05:30",
        "standard_execution_mode": "STANDARD",
        "execution_mode_reason": "price 119.50 inside [100.0, 250.0]",
        "isolated_option_ltp": 119.5,
        "isolated_option_ltp_source": "feed_ltp",
        "status": "ACTIVE",
        "opposite_leg": {
            "role": "opposite",
            "instrument_key": "NSE_FO|64474",
            "exchange_token": "64474",
            "trading_symbol": "NIFTY 25500 PE 23 NOV 26",
            "name": "NIFTY",
            "strike": 25500.0,
            "option_type": "PE",
            "expiry": "2026-11-23",
            "lot_size": 65,
            "tick_size": 5.0,
            "exchange": "NSE",
            "segment": "NSE_FO",
        },
        "isolation_backfilled": False,
        "explanation": {
            "trend": {
                "trend": "RISING",
                "ema": 25383.70589569161,
                "reason": "EMA slope inconclusive; last_close > day_open",
            }
        },
    }


def test_isolation_announcement_uses_group_chat_and_12_hour_ist(monkeypatch):
    monkeypatch.setattr(telegram, "TELEGRAM_GROUP_ENABLED", True)
    monkeypatch.setattr(telegram, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(telegram, "TELEGRAM_GROUP_CHAT_ID", "-100123456789")
    monkeypatch.setattr(telegram, "TELEGRAM_ENABLED", True)
    monkeypatch.setattr(telegram, "TELEGRAM_CHAT_ID", "private-chat")
    monkeypatch.setattr(telegram, "CHART_BASE_URL", "https://ng7.up.railway.app")
    monkeypatch.setattr(telegram, "TELEGRAM_CHAT_ID", "private-chat-must-not-be-used")
    captured = {}
    delivered_chats = []

    class Response:
        def raise_for_status(self):
            return None

    def fake_post(url, json, timeout):
        captured.update(url=url, payload=json, timeout=timeout)
        delivered_chats.append(json["chat_id"])
        return Response()

    monkeypatch.setattr(telegram.requests, "post", fake_post)

    assert telegram.notify_isolation_created(_record()) is True
    assert delivered_chats == ["-100123456789", "private-chat"]
    assert "private-chat-must-not-be-used" not in str(captured["payload"])
    message = captured["payload"]["text"]
    assert "09:18:50 AM IST" in message
    assert "09:18:51 AM IST" in message
    assert "NIFTY 25500 CE 23 NOV 26" in message
    assert "NSE_FO|64473" in message
    assert "NIFTY 25500 PE 23 NOV 26" in message
    assert "25485.10" in message
    assert "R3_Threshold" in message
    assert "https://ng7.up.railway.app/chart/NSE_FO%7C64473" in message
    assert "https://ng7.up.railway.app/chart/NSE_FO%7C64474" in message
    assert "https://ng7.up.railway.app/chart/NSE_INDEX%7CNifty%2050" in message
    assert len(captured) > 0
    assert captured["payload"]["parse_mode"] == "HTML"


def test_group_notification_is_skipped_without_group_chat_id(monkeypatch):
    monkeypatch.setattr(telegram, "TELEGRAM_GROUP_ENABLED", True)
    monkeypatch.setattr(telegram, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(telegram, "TELEGRAM_GROUP_CHAT_ID", "")
    called = []
    monkeypatch.setattr(telegram.requests, "post", lambda *args, **kwargs: called.append(True))

    assert telegram.notify_isolation_created(_record()) is False
    assert called == []


def test_archive_document_shape_is_supported():
    archive = {
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
            "archived_at_ist": "2026-11-20T09:18:51.463163+05:30",
        },
        "execution": {"mode": "STANDARD", "reason": "price inside range"},
        "market_context": {"nifty_ltp": 25485.1, "nifty_ltp_source": "feed_ltp"},
        "instruments": {
            "isolated": {
                "role": "isolated",
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
                "ltp_source": "feed_ltp",
            },
            "opposite": {
                "role": "opposite",
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
        "source_record": _record(),
    }
    message = telegram._build_isolation_message(archive)
    assert "Local archive saved: 20 Nov 2026, 09:18:51 AM IST" in message
    assert "NSE_FO|64474" in message
    assert "BACKFILLED" not in message
