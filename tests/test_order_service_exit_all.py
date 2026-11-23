from services import order_service as order_service_module
from services.order_service import OrderService


class FakeRepository:
    def __init__(self):
        self.entries = {}

    def append_entry(self, entry, *, require_mongo=True):
        self.entries[entry["local_id"]] = dict(entry)
        return {"local_saved": True, "mongo_saved": True}

    def find_entry(self, local_id):
        return self.entries.get(local_id)

    def update_entry(self, local_id, updates, *, require_mongo=True):
        if local_id not in self.entries:
            raise KeyError(local_id)
        self.entries[local_id].update(updates)
        return {"local_saved": True, "mongo_saved": True}


def test_exit_all_uses_official_rest_service_and_keeps_partial_errors(monkeypatch):
    calls = []
    broker_result = {
        "http_status": 207,
        "broker_status": "partial_success",
        "order_ids": ["EXIT-1"],
        "errors": [{"error_code": "SAMPLE", "message": "One position could not be exited"}],
        "summary": {"total": 2, "success": 1, "error": 1},
        "broker_response": {
            "status": "partial_success",
            "data": {"order_ids": ["EXIT-1"]},
            "errors": [{"error_code": "SAMPLE", "message": "One position could not be exited"}],
            "summary": {"total": 2, "success": 1, "error": 1},
        },
    }

    monkeypatch.setattr(
        order_service_module,
        "submit_exit_positions",
        lambda **kwargs: calls.append(kwargs) or broker_result,
    )
    monkeypatch.setattr(OrderService, "_require_trading", lambda self: None)
    monkeypatch.setattr(order_service_module, "notify_event", lambda *args, **kwargs: None)

    repository = FakeRepository()
    service = OrderService(repository=repository)
    result = service.exit_all_positions(tag="strategy-a", segment="NSE_FO")

    assert calls == [{"tag": "strategy-a", "segment": "NSE_FO"}]
    assert result["status"] == "exit_all_partial_success"
    assert result["order_ids"] == ["EXIT-1"]
    assert result["errors"][0]["error_code"] == "SAMPLE"
    assert result["summary"] == {"total": 2, "success": 1, "error": 1}

    parent = next(item for item in repository.entries.values() if item["record_type"] == "EXIT_ALL")
    assert parent["status"] == "EXIT_ALL_PARTIAL_SUCCESS"
    assert parent["broker_errors"][0]["error_code"] == "SAMPLE"
    assert any(item["record_type"] == "EXIT_ALL_ORDER" for item in repository.entries.values())
