from app.api.dashboard_query import _flatten_orders, _instrument_details

def test_flatten_orders_merges_history_and_current_order():
    states = [{
        "isolation_id": "2026-11-17__NSE_FO_123",
        "order_history": [{"order_id": "one", "indicator": "sr", "status": "COMPLETED_STOP_LOSS"}],
        "orders": {
            "sr": {"order_id": "one", "indicator": "sr", "status": "COMPLETED_STOP_LOSS", "pnl_net": -1250},
            "supertrend": {"order_id": "two", "indicator": "supertrend", "status": "DUMMY_PLACED"},
        },
    }]
    result = _flatten_orders(states)
    assert len(result) == 2
    by_id = {item["order_id"]: item for item in result}
    assert by_id["one"]["pnl_net"] == -1250
    assert by_id["one"]["isolation_id"] == "2026-11-17__NSE_FO_123"
    assert by_id["two"]["strategy"] == "SUPERTREND"

def test_instrument_details_uses_provider_symbol_and_metadata():
    details = _instrument_details({
        "trading_symbol": "NIFTY 25000 CE 27 NOV 26", "strike": 25000,
        "option_type": "CE", "expiry": "2026-11-27", "lot_size": 65,
    }, "isolated")
    assert details["trading_symbol"] == "NIFTY 25000 CE 27 NOV 26"
    assert details["strike"] == 25000
    assert details["lot_size"] == 65
