from __future__ import annotations

from services import isolation_telegram_service as isolation_telegram
from sandbox_services.supertrend_signal_service import SandboxSupertrendSignalService


def _isolation_record():
    return {
        "trade_date_ist": "2026-11-27",
        "instrument_key": "NSE_FO|47855",
        "trading_symbol": "NIFTY 25900 CE 01 DEC 26",
        "isolation_created_at": "2026-11-27T09:41:00+05:30",
        "status": "ACTIVE",
        "trigger_index": "NSE_INDEX|NIFTY MID SELECT",
        "trigger_index_canonical": "MIDCPNIFTY",
        "trigger_side": "R3",
        "trigger_level_name": "R3_Threshold",
        "trigger_level": 14459.56,
        "trigger_value": 14463.70,
        "nifty_ltp_at_isolation": 25877.60,
        "nifty_ltp_source": "feed_ltp",
        "bias": "CE",
        "standard_execution_mode": "STANDARD",
        "execution_mode_reason": "price 161 inside configured range",
        "explanation": {"trend": {"trend": "RISING", "ema": 25849.02, "reason": "positive slope"}},
        "opposite_leg": {
            "instrument_key": "NSE_FO|47892",
            "trading_symbol": "NIFTY 25900 PE 01 DEC 26",
            "option_type": "PE",
        },
    }


def test_isolation_uses_short_group_and_detailed_private_message(monkeypatch):
    monkeypatch.setattr(isolation_telegram, "CHART_BASE_URL", "https://example.test")
    monkeypatch.setattr(isolation_telegram, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(isolation_telegram, "TELEGRAM_GROUP_ENABLED", True)
    monkeypatch.setattr(isolation_telegram, "TELEGRAM_GROUP_CHAT_ID", "-100group")
    monkeypatch.setattr(isolation_telegram, "TELEGRAM_ENABLED", True)
    monkeypatch.setattr(isolation_telegram, "TELEGRAM_CHAT_ID", "private")
    sent = []
    monkeypatch.setattr(
        isolation_telegram,
        "_post_html",
        lambda chat, message, label: sent.append((chat, message, label)) or True,
    )

    assert isolation_telegram.notify_isolation_created(_isolation_record()) is True
    assert [item[0] for item in sent] == ["-100group", "private"]
    group_message, private_message = sent[0][1], sent[1][1]

    assert "NIFTY 25900 CE 01 DEC 26" in group_message
    assert "NIFTY 25900 PE 01 DEC 26" in group_message
    assert "Isolated instrument chart" in group_message
    assert "Opposite instrument chart" in group_message
    for secret in ("R3_Threshold", "14,459.56", "14,463.70", "25,877.60",
                   "Execution mode", "EMA", "Isolation ID"):
        assert secret not in group_message

    assert "R3_Threshold" in private_message
    assert "Execution mode" in private_message
    assert "Isolated instrument chart" in private_message
    assert "Opposite instrument chart" in private_message


def test_supertrend_group_hides_trade_plan_but_private_keeps_details(monkeypatch, tmp_path):
    service = SandboxSupertrendSignalService(root=tmp_path)
    captured = {}
    monkeypatch.setattr(
        "sandbox_services.supertrend_signal_service.format_option_chart_links",
        lambda isolated, opposite: [
            '<a href="https://example.test/chart/isolated">📈 Isolated instrument chart</a>',
            '<a href="https://example.test/chart/opposite">📉 Opposite instrument chart</a>',
        ],
    )
    monkeypatch.setattr(
        "sandbox_services.supertrend_signal_service.send_isolation_notifications",
        lambda group, private: captured.update(group=group, private=private) or True,
    )
    document = {
        "interval": "1m",
        "first_signal": {
            "signal": "sell",
            "confirmed_time": "2026-11-27T10:09:00+05:30",
            "confirmed_time_price": 172.0,
        },
        "signal_selection": {
            "isolation_time_ist": "2026-11-27T09:41:00+05:30",
            "eligible_signal_count": 1,
            "ignored_pre_isolation_signal_count": 1,
        },
        "signal_source": {
            "trading_symbol": "NIFTY 25900 PE 01 DEC 26",
            "instrument_key": "NSE_FO|47892",
        },
        "instruments": {
            "isolated": {"instrument_key": "NSE_FO|47855", "trading_symbol": "NIFTY 25900 CE 01 DEC 26"},
            "opposite": {"instrument_key": "NSE_FO|47892", "trading_symbol": "NIFTY 25900 PE 01 DEC 26"},
        },
        "source_snapshots": {
            "isolated": {"instrument_key": "NSE_FO|47855"},
            "opposite": {"instrument_key": "NSE_FO|47892"},
        },
        "target": {
            "leg_role": "isolated",
            "trading_symbol": "NIFTY 25900 CE 01 DEC 26",
            "instrument_key": "NSE_FO|47855",
            "entry_price": 147.40,
        },
        "calculation": {
            "stop_loss": 103.18,
            "target_price": 197.40,
            "percentage_stop_loss": 103.18,
            "nearest_support": {"level": "S1", "value": 140.85},
            "stop_loss_basis": "30_percent_stop_is_lower_than_nearest_support",
            "target_band": "100 < entry <= 200",
            "target_offset": 50,
        },
    }

    assert service._notify(document) is True
    group = captured["group"]
    private = captured["private"]

    assert "SUPERTREND SIGNAL DETECTED" in group
    assert "Bearish (SELL)" in group
    assert "NIFTY 25900 PE 01 DEC 26" in group
    assert "NIFTY 25900 CE 01 DEC 26" in group
    assert "Isolated instrument chart" in group
    assert "Opposite instrument chart" in group
    for secret in ("172.00", "147.40", "103.18", "197.40", "S1", "target increase",
                   "Selected leg", "Why accepted"):
        assert secret not in group

    assert "Live entry price" in private
    assert "Stop loss" in private
    assert "Target price" in private
    assert "Nearest support below entry" in private
    assert "Isolated instrument chart" in private
    assert "Opposite instrument chart" in private
