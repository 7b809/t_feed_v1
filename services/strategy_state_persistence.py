"""MongoDB persistence for one strategy snapshot per trading date/underlying."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from threading import RLock
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


class StrategyStateRepository:
    """Upsert date/index state and preserve prior trading-day documents."""

    def __init__(self, collection=None):
        self._collection = collection
        self._client = None
        self._lock = RLock()
        self._indexes_ready = False
        self.last_error: str | None = None

    def _get_collection(self):
        if self._collection is not None:
            self._ensure_indexes()
            return self._collection
        with self._lock:
            if self._collection is not None:
                self._ensure_indexes()
                return self._collection
            uri = str(getattr(config, "MONGO_URI", "") or "").strip()
            database_name = str(getattr(config, "MONGO_DB", "") or "").strip()
            collection_name = str(
                getattr(config, "STRATEGY_STATE_COLLECTION", "strategy_state") or "strategy_state"
            ).strip()
            if not uri or not database_name or not collection_name:
                raise ValueError("Mongo URI, database, and strategy-state collection must be configured")
            self._client = MongoClient(uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000)
            self._collection = self._client[database_name][collection_name]
            self._ensure_indexes()
            return self._collection

    def _ensure_indexes(self):
        if self._indexes_ready or self._collection is None:
            return
        self._collection.create_index(
            [("trading_date", ASCENDING), ("underlying", ASCENDING)],
            unique=True,
            name="strategy_state_trading_date_underlying_uq",
        )
        self._collection.create_index(
            [("underlying", ASCENDING), ("metadata.updated_at", DESCENDING)],
            name="strategy_state_underlying_updated_idx",
        )
        self._indexes_ready = True

    @staticmethod
    def _identity(trading_date: Any, underlying: Any) -> dict:
        date_key = str(trading_date or "")[:10]
        index_key = str(underlying or "").strip().upper()
        try:
            date_key = date.fromisoformat(date_key).isoformat()
        except ValueError:
            date_key = ""
        if not date_key or not index_key:
            raise ValueError("trading_date (YYYY-MM-DD) and underlying are required")
        return {"trading_date": date_key, "underlying": index_key}

    def load(self, trading_date: str, underlying: str) -> dict | None:
        identity = self._identity(trading_date, underlying)
        try:
            result = self._get_collection().find_one(identity)
            self.last_error = None
            return deepcopy(result) if result else None
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Strategy state load failed. identity=%s error=%s", identity, self.last_error)
            return None

    def save(self, document: dict, event_type: str | None = None) -> bool:
        if not isinstance(document, dict):
            return False
        identity = self._identity(document.get("trading_date"), document.get("underlying"))
        snapshot = deepcopy(document)
        snapshot.update(identity)
        snapshot.pop("_id", None)
        if event_type:
            snapshot.setdefault("metadata", {})["last_event_type"] = str(event_type)
        try:
            self._get_collection().update_one(
                identity,
                {"$set": snapshot},
                upsert=True,
            )
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Strategy state upsert failed. identity=%s error=%s", identity, self.last_error)
            return False

    def status(self) -> dict:
        return {
            "collection": getattr(config, "STRATEGY_STATE_COLLECTION", "strategy_state"),
            "indexes_ready": self._indexes_ready,
            "last_error": self.last_error,
        }

    def close(self):
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
                self._collection = None
                self._indexes_ready = False


strategy_state_repository = StrategyStateRepository()

__all__ = ["StrategyStateRepository", "strategy_state_repository"]
