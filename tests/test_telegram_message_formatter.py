from __future__ import annotations

from app.services.telegram_message_formatter import (
    format_isolation_notification,
    format_order_completion_notification,
    format_order_entry_notification,
    format_recovery_notification,
)


def test_isolation_notification_shows_symbols_and_keeps_keys_as_secondary_details():
    message = format_isolation_notification(
        "2026-11-17__NSE_FO_45404",
        {
            "instrument_key": "NSE_FO|45404",
            "trading_symbol": "NIFTY 25400 CE 17 NOV 26",
            "opposite_leg": {
                "instrument_key": "NSE_FO|45405",
                "trading_symbol": "NIFTY 25400 PE 17 NOV 26",
            },
            "trade_date_ist": "2026-11-17",
            "isolation_time": "2026-11-17T09:18:51.331181+05:30",
        },
    )

    assert "📥 INSTRUMENT ISOLATED" in message
    assert "NIFTY 25400 CE 17 NOV 26" in message
    assert "NIFTY 25400 PE 17 NOV 26" in message
    assert "NSE_FO|45404" in message
    assert "NSE_FO|45405" in message
    assert "Trade date: 2026-11-17 (IST)" in message
    assert len(message) < 1200


def test_entry_notification_is_focused_on_only_the_new_order():
    message = format_order_entry_notification(
        {"isolation_id": "2026-11-17__NSE_FO_45404"},
        {
            "indicator": "SUPERTREND",
            "status": "DUMMY_PLACED",
            "signal_direction": "BEARISH",
            "trading_symbol": "NIFTY 25400 PE 17 NOV 26",
            "target_instrument_key": "NSE_FO|45405",
            "source_trading_symbol": "NIFTY 25400 CE 17 NOV 26",
            "source_instrument_key": "NSE_FO|45404",
            "entry_price": 63.5,
            "quantity": 1,
            "lot_size": 50,
            "quantity_units": 50,
            "target_price": 78.5,
            "stop_loss_price": 44.45,
            "started_at_ist": "2026-11-17T09:24:00+05:30",
            "trigger": {"confirmed_time": "2026-11-17T09:22:00+05:30"},
        },
    )

    assert "🟢 SIMULATED ENTRY" in message
    assert "NIFTY 25400 PE 17 NOV 26" in message
    assert "SUPERTREND • BUY" in message
    assert "Signal: Bearish" in message
    assert "Entry: ₹63.50" in message
    assert "Qty: 1 lot × 50 (50 units)" in message
    assert "Target: ₹78.50" in message
    assert "Stop: ₹44.45" in message
    assert "NIFTY 25400 CE 17 NOV 26" in message
    assert "SIMULATION ONLY" in message
    assert "WAITING FOR FIRST CONFIRMED SIGNAL" not in message


def test_unpriced_signal_is_not_reported_as_a_filled_entry():
    message = format_order_entry_notification(
        {},
        {
            "indicator": "SR",
            "status": "DUMMY_SIGNAL_RECORDED_NO_QUOTE",
            "signal_direction": "SUPPORT",
            "trading_symbol": "NIFTY 25400 CE 17 NOV 26",
            "target_instrument_key": "NSE_FO|45404",
            "trigger": {
                "level_price": 33.65,
                "confirmed_time": "2026-11-17T09:30:00+05:30",
            },
        },
    )

    assert "SIGNAL RECORDED • ENTRY WAITING" in message
    assert "No simulated fill was recorded" in message
    assert "SR level: ₹33.65" in message
    assert "Entry:" not in message


def test_completion_notification_prioritises_outcome_and_net_pnl():
    message = format_order_completion_notification(
        {},
        "supertrend",
        {
            "indicator": "SUPERTREND",
            "exit_reason": "TARGET",
            "trading_symbol": "NIFTY 25400 PE 17 NOV 26",
            "target_instrument_key": "NSE_FO|45405",
            "entry_price": 63.5,
            "exit_price": 78.5,
            "pnl_net": 750.0,
            "duration_seconds": 780,
            "completed_at_ist": "2026-11-17T09:37:00+05:30",
        },
    )

    assert "SIMULATED ORDER CLOSED" in message
    assert "Target reached" in message
    assert "Entry: ₹63.50 → Exit: ₹78.50" in message
    assert "P&L: +₹750.00" in message
    assert "Duration: 13m 0s" in message


def test_recovery_notification_is_a_short_summary():
    message = format_recovery_notification(
        {
            "isolated_instrument": {"trading_symbol": "NIFTY 25400 CE 17 NOV 26"},
            "legs": {"isolated": "NSE_FO|45404"},
            "supertrend_startup_recovery": {
                "signals_found": 4,
                "orders_replayed": 3,
                "orders_completed": 2,
                "daily_target_locked": True,
            },
            "orders": {},
        }
    )

    assert "STARTUP RECOVERY COMPLETE" in message
    assert "Signals found: 4" in message
    assert "Entries replayed: 3" in message
    assert "Closed: 2" in message
    assert "further Supertrend entries locked" in message
    assert len(message) < 700
