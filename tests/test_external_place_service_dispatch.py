from fastapi.exceptions import RequestValidationError

from api.external_routes import orders as orders_routes


def test_place_service_route_dispatches_budget_request_and_applies_defaults(monkeypatch):
    captured = {}

    def fake_budget(**kwargs):
        captured.update(kwargs)
        return {
            "status": "PREVIEW",
            "selected_instrument_key": "NSE_FO|100",
            "selected_instrument": {
                "instrument_key": "NSE_FO|100",
                "instrument_type": "CE",
                "ltp": 100,
            },
        }

    monkeypatch.setattr(orders_routes, "place_option_orders_by_budget", fake_budget)

    result = orders_routes.external_place_order_service({
        "api_token": "",
        "underlying_key": "NSE_INDEX|Nifty 50",
        "budget_min": 100,
        "budget_max": 150,
        "option_type": "CE",
        "execute": False,
    })

    assert result["status"] == "PREVIEW"
    assert result["selected_instrument_key"] == "NSE_FO|100"
    assert captured["underlying_key"] == "NSE_INDEX|Nifty 50"
    assert captured["max_orders"] == 1
    assert captured["lot"] == 1
    assert captured["transaction_type"] == "BUY"
    assert captured["product"] == "D"
    assert captured["order_type"] == "MARKET"
    assert captured["execute"] is False


def test_place_service_route_keeps_existing_single_instrument_requests(monkeypatch):
    captured = {}

    def fake_place(**kwargs):
        captured.update(kwargs)
        return {"status": "SUCCESS", "response": {"order_id": "ORDER-1"}}

    monkeypatch.setattr(orders_routes, "place_order_service", fake_place)
    result = orders_routes.external_place_order_service({
        "instrument_token": "NSE_FO|123",
        "lot": 1,
    })

    assert result["status"] == "SUCCESS"
    assert captured["instrument_token"] == "NSE_FO|123"


def test_place_service_route_returns_validation_422_for_incomplete_budget_payload():
    try:
        orders_routes.external_place_order_service({
            "budget_min": 100,
            "option_type": "CE",
        })
    except RequestValidationError as exc:
        assert any(tuple(item["loc"]) == ("body", "budget_max") for item in exc.errors())
    else:
        raise AssertionError("Expected request validation error")
