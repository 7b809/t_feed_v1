"""Daily MongoDB persistence for dummy SR/Supertrend orders.

One MongoDB document is maintained per IST date. The requested summary fields
remain at the document root; by_isolation and orders_history preserve every
isolation/order when multiple isolations occur on the same day.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class DummyOrderMongoStore:
    def __init__(self, collection: Any) -> None:
        self._collection = collection

    @staticmethod
    def _isolation_key(isolation_id: str) -> str:
        # A digest is safe for Mongo dotted update paths (no dots or '$').
        return hashlib.sha256(isolation_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _now_utc() -> datetime:
        return datetime.now(timezone.utc)

    def _base_update(self, now: datetime) -> tuple[dict, dict]:
        return (
            {"created_datetime": now, "orders_data.created_at": now.isoformat()},
            {"last_updated_time": now, "orders_data.last_updated_at": now.isoformat()},
        )

    def save_isolation(self, date_key: str, isolation_id: str,
                       isolated_instrument: dict) -> None:
        now = self._now_utc()
        key = self._isolation_key(isolation_id)
        on_insert, fields = self._base_update(now)
        fields.update({
            "isolated_instrument": isolated_instrument,
            f"orders_data.by_isolation.{key}.isolation_id": isolation_id,
            f"orders_data.by_isolation.{key}.isolated_instrument": isolated_instrument,
        })
        self._collection.update_one(
            {"_id": date_key},
            {"$setOnInsert": on_insert, "$set": fields},
            upsert=True,
        )

    def save_order(self, date_key: str, isolation_id: str, indicator: str,
                   order: dict, isolated_instrument: dict) -> None:
        now = self._now_utc()
        key = self._isolation_key(isolation_id)
        indicator_key = "sr_order" if indicator == "sr" else "supertrend_order"
        # Use a detached JSON-safe document; PyMongo accepts datetime but the
        # event payload may contain non-BSON objects, so normalize defensively.
        safe_order = self._json_safe(order)
        history_item = {
            "isolation_id": isolation_id,
            "indicator": indicator_key,
            "order": safe_order,
            "recorded_at": now.isoformat(),
        }
        on_insert, fields = self._base_update(now)
        fields.update({
            "isolated_instrument": isolated_instrument,
            f"orders_data.{indicator_key}": safe_order,
            f"orders_data.by_isolation.{key}.isolation_id": isolation_id,
            f"orders_data.by_isolation.{key}.isolated_instrument": isolated_instrument,
            f"orders_data.by_isolation.{key}.{indicator_key}": safe_order,
        })
        self._collection.update_one(
            {"_id": date_key},
            {
                "$setOnInsert": on_insert,
                "$set": fields,
                "$push": {"orders_data.orders_history": {"$each": [history_item], "$slice": -5000}},
            },
            upsert=True,
        )

    def update_order(self, date_key: str, isolation_id: str, indicator: str,
                     order: dict, isolated_instrument: dict,
                     completed: bool = False) -> None:
        now = self._now_utc()
        key = self._isolation_key(isolation_id)
        indicator_key = "sr_order" if indicator == "sr" else "supertrend_order"
        safe_order = self._json_safe(order)
        fields = {
            "last_updated_time": now,
            "orders_data.last_updated_at": now.isoformat(),
            "isolated_instrument": isolated_instrument,
            f"orders_data.{indicator_key}": safe_order,
            f"orders_data.by_isolation.{key}.{indicator_key}": safe_order,
        }
        update = {
            "$setOnInsert": {"created_datetime": now, "orders_data.created_at": now.isoformat()},
            "$set": fields,
        }
        if completed:
            update["$push"] = {"orders_data.orders_history": {"$each": [{
                "isolation_id": isolation_id, "indicator": indicator_key,
                "event": "COMPLETED", "order": safe_order, "recorded_at": now.isoformat(),
            }], "$slice": -5000}}
        self._collection.update_one({"_id": date_key}, update, upsert=True)

    def list_date_keys(self, limit: int = 365) -> list[str]:
        """Return archived IST trading dates, newest first."""
        safe_limit = max(1, min(int(limit), 3650))
        cursor = self._collection.find({}, {"_id": 1}).sort("_id", -1).limit(safe_limit)
        dates: list[str] = []
        for document in cursor:
            value = str(document.get("_id") or "")
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                continue
            dates.append(value)
        return dates

    def get_day(self, date_key: str) -> dict[str, Any] | None:
        """Load one daily archive document by its YYYY-MM-DD key."""
        result = self._collection.find_one({"_id": date_key})
        return result if isinstance(result, dict) else None

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(k): cls._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._json_safe(v) for v in value]
        if isinstance(value, datetime):
            return value.isoformat()
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)
