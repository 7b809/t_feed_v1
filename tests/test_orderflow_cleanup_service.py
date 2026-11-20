from services import orderflow_cleanup_service as cleanup_module


class FakeRepository:
    def __init__(self):
        self.entries = []

    def find_order(self, order_id):
        for entry in self.entries:
            if entry.get("order_id") == order_id:
                return entry
            if order_id in (entry.get("order_ids") or []):
                return entry
        return None

    def append_entry(self, entry, *, require_mongo=True):
        self.entries.append(dict(entry))
        return {"local_saved": True, "mongo_saved": True}

    def update_entry(self, local_id, updates, *, require_mongo=True):
        for entry in self.entries:
            if entry.get("local_id") == local_id:
                entry.update(updates)
                return {"local_saved": True, "mongo_saved": True}
        raise KeyError(local_id)


def test_scheduled_gtt_is_cancelled_without_account_exit(monkeypatch):
    monkeypatch.setattr(cleanup_module, "ORDERFLOW_CLEANUP_ENABLED", True)
    monkeypatch.setattr(
        cleanup_module,
        "get_gtt_order_details",
        lambda gtt_id: {"data": [{
            "gtt_order_id": gtt_id,
            "instrument_token": "NSE_FO|123",
            "quantity": 25,
            "product": "I",
            "rules": [{"strategy": "ENTRY", "status": "SCHEDULED", "order_id": None}],
        }]},
    )
    calls = []
    monkeypatch.setattr(
        cleanup_module, "cancel_gtt_order",
        lambda gtt_id: calls.append(gtt_id) or {"success": True, "status": "CANCEL_SUBMITTED"},
    )
    repo = FakeRepository()
    service = cleanup_module.OrderflowCleanupService(repository=repo)
    result = service.cleanup(["GTT-1"], isolation_id="iso-1", source_order_id="DUMMY-1")
    assert result["cleanup_complete"] is True
    assert result["results"][0]["status"] == "CANCELLED_UNTRIGGERED"
    assert calls == ["GTT-1"]
    assert repo.entries[0]["status"] == "CANCELLED_UNTRIGGERED"


def test_triggered_gtt_without_entry_order_id_blocks_cleanup(monkeypatch):
    monkeypatch.setattr(cleanup_module, "ORDERFLOW_CLEANUP_ENABLED", True)
    monkeypatch.setattr(
        cleanup_module,
        "get_gtt_order_details",
        lambda gtt_id: {"data": [{
            "gtt_order_id": gtt_id,
            "instrument_token": "NSE_FO|123",
            "rules": [{"strategy": "ENTRY", "status": "TRIGGERED", "order_id": None}],
        }]},
    )
    repo = FakeRepository()
    service = cleanup_module.OrderflowCleanupService(repository=repo)
    try:
        service.cleanup(["GTT-2"], isolation_id="iso-1")
    except cleanup_module.OrderflowCleanupError as exc:
        assert "no generated ENTRY order ID" in str(exc)
    else:
        raise AssertionError("Unknown triggered exposure must block replacement")
