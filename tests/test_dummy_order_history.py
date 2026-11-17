from __future__ import annotations

from app.services.dummy_order_history import (
    filter_orders,
    flatten_daily_orders,
    summarize_orders,
)


def test_event_history_and_latest_snapshot_merge_without_duplicate_orders():
    document = {
        "_id": "2026-11-17",
        "orders_data": {
            "orders_history": [
                {
                    "isolation_id": "2026-11-17__NSE_FO_123",
                    "indicator": "sr_order",
                    "recorded_at": "2026-11-17T09:20:00+05:30",
                    "order": {
                        "order_id": "order-1",
                        "indicator": "SR",
                        "status": "DUMMY_PLACED",
                        "entry_time_ist": "2026-11-17T09:20:00+05:30",
                    },
                },
                {
                    "isolation_id": "2026-11-17__NSE_FO_123",
                    "indicator": "sr_order",
                    "recorded_at": "2026-11-17T09:35:00+05:30",
                    "event": "COMPLETED",
                    "order": {
                        "order_id": "order-1",
                        "indicator": "SR",
                        "status": "COMPLETED_TARGET",
                        "exit_reason": "TARGET",
                        "entry_time_ist": "2026-11-17T09:20:00+05:30",
                        "pnl_net": 125.5,
                    },
                },
            ],
            "by_isolation": {
                "isolation-hash": {
                    "isolation_id": "2026-11-17__NSE_FO_123",
                    "sr_order": {
                        "order_id": "order-1",
                        "indicator": "SR",
                        "status": "COMPLETED_TARGET",
                        "exit_reason": "TARGET",
                        "entry_time_ist": "2026-11-17T09:20:00+05:30",
                        "pnl_net": 125.5,
                    },
                }
            },
        },
    }

    orders = flatten_daily_orders(document)

    assert len(orders) == 1
    assert orders[0]["status"] == "COMPLETED_TARGET"
    assert orders[0]["strategy"] == "SR"
    assert orders[0]["trade_date_ist"] == "2026-11-17"
    assert orders[0]["pnl_net"] == 125.5


def test_same_order_id_in_different_isolations_is_not_collapsed():
    document = {
        "_id": "2026-11-17",
        "orders_data": {
            "by_isolation": {
                "isolation-a": {
                    "isolation_id": "2026-11-17__NSE_FO_111",
                    "sr_order": {"order_id": "same-id", "indicator": "SR", "status": "DUMMY_PLACED"},
                },
                "isolation-b": {
                    "isolation_id": "2026-11-17__NSE_FO_222",
                    "sr_order": {"order_id": "same-id", "indicator": "SR", "status": "DUMMY_PLACED"},
                },
            }
        },
    }

    orders = flatten_daily_orders(document)

    assert len(orders) == 2
    assert len({order["history_key"] for order in orders}) == 2


def test_legacy_root_snapshot_and_filters_and_summary():
    document = {
        "_id": "2026-11-17",
        "orders_data": {
            "sr_order": {
                "order_id": "legacy-sr",
                "indicator": "SR",
                "status": "COMPLETED_STOP_LOSS",
                "exit_reason": "STOP_LOSS",
                "pnl_net": -50,
                "trading_symbol": "NIFTY 25000 CE",
            },
            "supertrend_order": {
                "order_id": "legacy-st",
                "indicator": "SUPERTREND",
                "status": "DUMMY_PLACED",
                "trading_symbol": "NIFTY 25100 PE",
            },
        },
    }

    orders = flatten_daily_orders(document)
    assert len(orders) == 2

    filtered = filter_orders(orders, strategy="supertrend", search="25100")
    summary = summarize_orders(filtered)

    assert len(filtered) == 1
    assert summary == {
        "total_orders": 1,
        "open_orders": 1,
        "completed_orders": 0,
        "target_hits": 0,
        "stop_losses": 0,
        "realized_pnl": None,
    }

    sr_summary = summarize_orders(filter_orders(orders, status="COMPLETED_STOP_LOSS"))
    assert sr_summary["completed_orders"] == 1
    assert sr_summary["stop_losses"] == 1
    assert sr_summary["realized_pnl"] == -50
