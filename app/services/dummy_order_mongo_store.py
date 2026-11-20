"""Daily MongoDB persistence for Supertrend dummy orders.

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
        indicator_key = "supertrend_order"
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
        indicator_key = "supertrend_order"
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

    def load_isolation(self, date_key: str, isolation_id: str) -> dict[str, Any] | None:
        """Load the latest durable Supertrend state for one isolation.

        The daily archive remains backward compatible: the per-isolation
        snapshot is preferred, while orders_history supplies older orders and
        completion events. Duplicate snapshots are collapsed by stable order ID.
        """
        document = self._collection.find_one({"_id": date_key})
        if not isinstance(document, dict):
            return None
        orders_data = document.get("orders_data") or {}
        if not isinstance(orders_data, dict):
            return None

        key = self._isolation_key(isolation_id)
        by_isolation = orders_data.get("by_isolation") or {}
        entry = by_isolation.get(key) if isinstance(by_isolation, dict) else None
        if not isinstance(entry, dict) or entry.get("isolation_id") != isolation_id:
            # Older archives may have only the root-level snapshot. Do not
            # attribute a root snapshot to another isolation when per-isolation
            # records exist.
            has_scoped_records = isinstance(by_isolation, dict) and bool(by_isolation)
            if has_scoped_records:
                return None
            entry = {
                "isolation_id": isolation_id,
                "isolated_instrument": document.get("isolated_instrument"),
                "supertrend_order": orders_data.get("supertrend_order"),
            }

        candidates: dict[str, tuple[tuple[int, str, str], dict[str, Any]]] = {}

        def add(order_value: Any, recorded_at: Any = None, *, snapshot: bool = False) -> None:
            if not isinstance(order_value, dict):
                return
            indicator = str(order_value.get("indicator") or order_value.get("strategy") or "SUPERTREND").upper()
            if "SUPERTREND" not in indicator:
                return
            identity = str(
                order_value.get("order_id")
                or order_value.get("started_at_ist")
                or order_value.get("entry_time_ist")
                or ""
            )
            if not identity:
                return
            order = dict(order_value)
            status = str(order.get("status") or "").upper()
            marker = str(
                order.get("completed_at_ist") or order.get("updated_at")
                or order.get("high_time_ist") or order.get("low_time_ist")
                or recorded_at or order.get("started_at_ist") or ""
            )
            # A completion event must beat an older open-entry event. A
            # per-isolation snapshot wins ties because it is the latest slot.
            rank = (1 if status.startswith("COMPLETED") else 0,
                    marker, "1" if snapshot else "0")
            previous = candidates.get(identity)
            if previous is None or rank >= previous[0]:
                candidates[identity] = (rank, order)

        for event in orders_data.get("orders_history") or []:
            if not isinstance(event, dict) or str(event.get("isolation_id") or "") != isolation_id:
                continue
            if str(event.get("indicator") or "").upper() not in {"SUPERTREND_ORDER", "SUPERTREND"}:
                continue
            add(event.get("order"), event.get("recorded_at"))

        add(entry.get("supertrend_order"), snapshot=True)
        ordered = [value[1] for value in candidates.values()]
        ordered.sort(key=lambda item: (
            str(item.get("started_at_ist") or item.get("entry_time_ist") or ""),
            str(item.get("order_id") or ""),
        ))
        snapshot = entry.get("supertrend_order")
        current = None
        if isinstance(snapshot, dict):
            snapshot_id = str(snapshot.get("order_id") or snapshot.get("started_at_ist")
                               or snapshot.get("entry_time_ist") or "")
            current = candidates.get(snapshot_id, (None, dict(snapshot)))[1]
        elif ordered:
            current = ordered[-1]
        current_id = str(
            (current or {}).get("order_id")
            or (current or {}).get("started_at_ist")
            or (current or {}).get("entry_time_ist")
            or ""
        )
        history = [
            order for order in ordered
            if str(order.get("order_id") or order.get("started_at_ist")
                   or order.get("entry_time_ist") or "") != current_id
        ]
        return {
            "isolation_id": isolation_id,
            "isolated_instrument": entry.get("isolated_instrument") or {},
            "current_order": current,
            "order_history": history,
            "order_count": len(ordered),
        }

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
