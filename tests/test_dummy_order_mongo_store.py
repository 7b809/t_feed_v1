from __future__ import annotations

from datetime import datetime

from app.services.dummy_order_mongo_store import DummyOrderMongoStore


class FakeCollection:
    def __init__(self):
        self.doc = None

    def update_one(self, query, update, upsert=False):
        if self.doc is None:
            assert upsert
            self.doc = {"_id": query["_id"]}
        for key, value in update.get("$setOnInsert", {}).items():
            self.doc.setdefault(key, value)
        for key, value in update.get("$set", {}).items():
            self._set_path(key, value)
        for key, value in update.get("$push", {}).items():
            target = self._get_path(key)
            if target is None:
                target = []
                self._set_path(key, target)
            if isinstance(value, dict) and "$each" in value:
                target.extend(value["$each"])
                if "$slice" in value:
                    target[:] = target[value["$slice"]:]
            else:
                target.append(value)

    def _set_path(self, path, value):
        parts = path.split(".")
        node = self.doc
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def _get_path(self, path):
        node = self.doc
        for part in path.split("."):
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        return node


def test_one_document_per_date_preserves_multiple_isolations():
    collection = FakeCollection()
    store = DummyOrderMongoStore(collection)
    first = {"instrument_key": "CE1", "trading_symbol": "NIFTY CE1"}
    second = {"instrument_key": "CE2", "trading_symbol": "NIFTY CE2"}
    store.save_isolation("2026-11-13", "2026-11-13__CE1", first)
    store.save_order("2026-11-13", "2026-11-13__CE1", "sr",
                     {"entry_price": 123.0}, first)
    store.save_isolation("2026-11-13", "2026-11-13__CE2", second)
    store.save_order("2026-11-13", "2026-11-13__CE2", "supertrend",
                     {"entry_price": 234.0}, second)

    doc = collection.doc
    assert doc["_id"] == "2026-11-13"
    assert isinstance(doc["created_datetime"], datetime)
    assert doc["isolated_instrument"] == second
    assert doc["orders_data"]["sr_order"]["entry_price"] == 123.0
    assert doc["orders_data"]["supertrend_order"]["entry_price"] == 234.0
    assert len(doc["orders_data"]["orders_history"]) == 2
    assert len(doc["orders_data"]["by_isolation"]) == 2
