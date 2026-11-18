"""Daily local JSON + MongoDB persistence for all order lifecycle events."""
from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.config import ORDERS_COLLECTION, ORDERS_DATA_DIR, TIMEZONE
from core.logger import get_logger
from core.mongo_client import get_mongo_db

logger = get_logger(__file__)
_LOCAL_LOCK = threading.RLock()
_TZ = ZoneInfo(TIMEZONE)


def now_iso() -> str:
    return datetime.now(_TZ).isoformat()


def _json_default(value: Any):
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return str(value)


class OrderRepository:
    """Store one daily document in Mongo and a same-day JSON mirror."""

    def __init__(self) -> None:
        self.data_dir = Path(ORDERS_DATA_DIR)

    @property
    def collection(self):
        return get_mongo_db()[ORDERS_COLLECTION]

    def _date_key(self) -> str:
        return datetime.now(_TZ).date().isoformat()

    def _local_path(self, date_key: str) -> Path:
        return self.data_dir / f"{date_key}.json"

    def empty_document(self, date_key: str | None = None) -> dict:
        key = date_key or self._date_key()
        stamp = now_iso()
        return {
            "_id": key,
            "date": key,
            "orders": [],
            "created_time": stamp,
            "last_modified_time": stamp,
        }

    def load_daily_document(self, date_key: str | None = None) -> dict:
        key = date_key or self._date_key()
        path = self._local_path(key)
        with _LOCAL_LOCK:
            if path.is_file():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    logger.exception("Local order JSON is unreadable: %s", path)

            try:
                document = self.collection.find_one({"_id": key})
            except Exception:
                logger.exception("Could not load daily orders from MongoDB")
                if path.is_file():
                    raise
                return self.empty_document(key)

            if document:
                document = dict(document)
                document.setdefault("orders", [])
                document.setdefault("date", key)
                document.setdefault("created_time", now_iso())
                document.setdefault("last_modified_time", now_iso())
                return document
            return self.empty_document(key)

    def save_daily_document(self, document: dict, *, require_mongo: bool = True) -> dict:
        """Write local JSON first, then replace the date-keyed Mongo document."""
        with _LOCAL_LOCK:
            key = str(document.get("date") or self._date_key())
            document["_id"] = key
            document["date"] = key
            document.setdefault("created_time", now_iso())
            document["last_modified_time"] = now_iso()
            self.data_dir.mkdir(parents=True, exist_ok=True)
            target = self._local_path(key)
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(document, indent=2, ensure_ascii=False, default=_json_default),
                encoding="utf-8",
            )
            temporary.replace(target)

            try:
                self.collection.replace_one({"_id": key}, document, upsert=True)
                return {"local_saved": True, "mongo_saved": True, "path": str(target)}
            except Exception:
                logger.exception("MongoDB daily order write failed; local mirror is saved")
                if require_mongo:
                    raise
                return {"local_saved": True, "mongo_saved": False, "path": str(target)}

    def append_entry(self, entry: dict, *, require_mongo: bool = True) -> dict:
        document = self.load_daily_document()
        document.setdefault("orders", []).append(deepcopy(entry))
        return self.save_daily_document(document, require_mongo=require_mongo)

    def update_entry(self, local_id: str, updates: dict, *, require_mongo: bool = True) -> dict:
        document = self.load_daily_document()
        for entry in document.get("orders", []):
            if entry.get("local_id") == local_id:
                entry.update(deepcopy(updates))
                return self.save_daily_document(document, require_mongo=require_mongo)
        raise KeyError(f"Local order record not found: {local_id}")

    def find_order(self, order_id: str) -> dict | None:
        document = self.load_daily_document()
        for entry in document.get("orders", []):
            identifiers = entry.get("order_ids") or []
            if entry.get("order_id") == order_id or order_id in identifiers:
                return entry
        return None

    def find_entry(self, local_id: str) -> dict | None:
        document = self.load_daily_document()
        for entry in document.get("orders", []):
            if entry.get("local_id") == local_id:
                return entry
        return None

    def list_today(self) -> dict:
        return self.load_daily_document()
