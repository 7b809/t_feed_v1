from services import upstox_order_api


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 400
        self.text = ""

    def json(self):
        return self._payload


def test_get_order_book_fetches_live_current_day_orders(monkeypatch):
    expected = [{"order_id": "ORDER-1", "status": "complete", "pending_quantity": 0}]
    calls = []

    monkeypatch.setattr(upstox_order_api, "UPSTOX_SANDBOX", False)
    monkeypatch.setattr(upstox_order_api, "fetch_access_token", lambda: "access-token")

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse({"status": "success", "data": expected})

    monkeypatch.setattr(upstox_order_api.requests, "get", fake_get)
    result = upstox_order_api.get_order_book()

    assert result["source"] == "upstox"
    assert result["scope"] == "current_trading_day"
    assert result["count"] == 1
    assert result["data"] == expected
    assert calls[0][0] == upstox_order_api.ORDER_BOOK_URL
    assert calls[0][1]["headers"]["Authorization"] == "Bearer access-token"


def test_order_book_accepts_direct_array_response(monkeypatch):
    expected = [{"order_id": "ORDER-2"}]
    monkeypatch.setattr(upstox_order_api, "UPSTOX_SANDBOX", False)
    monkeypatch.setattr(upstox_order_api, "fetch_access_token", lambda: "access-token")
    monkeypatch.setattr(
        upstox_order_api.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(expected),
    )

    result = upstox_order_api.get_order_book()
    assert result["data"] == expected
    assert result["count"] == 1


def test_exit_all_sends_optional_filters_as_query_params_without_body(monkeypatch):
    calls = []
    payload = {
        "status": "success",
        "data": {"order_ids": ["EXIT-1", "EXIT-2"]},
        "errors": None,
        "summary": {"total": 2, "success": 2, "error": 0},
    }

    monkeypatch.setattr(upstox_order_api, "UPSTOX_SANDBOX", False)
    monkeypatch.setattr(upstox_order_api, "fetch_access_token", lambda: "access-token")

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(payload)

    monkeypatch.setattr(upstox_order_api.requests, "post", fake_post)
    result = upstox_order_api.submit_exit_positions(tag="my-tag", segment="NSE_FO")

    assert calls[0][0] == upstox_order_api.EXIT_POSITIONS_URL
    assert calls[0][1]["params"] == {"tag": "my-tag", "segment": "NSE_FO"}
    assert "json" not in calls[0][1]
    assert result["order_ids"] == ["EXIT-1", "EXIT-2"]
    assert result["summary"]["success"] == 2


def test_exit_all_preserves_http_207_partial_success(monkeypatch):
    payload = {
        "status": "partial_success",
        "data": {"order_ids": ["EXIT-1"]},
        "errors": [{"error_code": "SAMPLE", "message": "One position failed"}],
        "summary": {"total": 2, "success": 1, "error": 1},
    }
    monkeypatch.setattr(upstox_order_api, "UPSTOX_SANDBOX", False)
    monkeypatch.setattr(upstox_order_api, "fetch_access_token", lambda: "access-token")
    monkeypatch.setattr(
        upstox_order_api.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(payload, status_code=207),
    )

    result = upstox_order_api.submit_exit_positions()
    assert result["http_status"] == 207
    assert result["broker_status"] == "partial_success"
    assert result["order_ids"] == ["EXIT-1"]
    assert result["summary"]["error"] == 1
