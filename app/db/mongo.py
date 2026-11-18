"""MongoDB persistence: one document per IST date, with an orders array."""
from __future__ import annotations
import logging
from datetime import datetime
from threading import RLock
from zoneinfo import ZoneInfo
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import PyMongoError
from app.core.config import settings

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
_client: MongoClient | None = None
_collection: Collection | None = None
_lock = RLock()

def now_ist() -> datetime:
    return datetime.now(IST)

def connect_mongo() -> Collection | None:
    global _client, _collection
    with _lock:
        if _collection is not None:
            return _collection
        if not settings.MONGO_URL:
            logger.warning("MONGO_URL is not configured; MongoDB persistence is disabled")
            return None
        _client = MongoClient(
            settings.MONGO_URL,
            serverSelectionTimeoutMS=settings.MONGO_SERVER_SELECTION_TIMEOUT_MS,
            tz_aware=True,
        )
        _client.admin.command("ping")
        _collection = _client[settings.MONGO_DATABASE][settings.MONGO_ORDERS_COLLECTION]
        _collection.create_index("date", unique=True)
        logger.info("Connected to MongoDB %s.%s", settings.MONGO_DATABASE, settings.MONGO_ORDERS_COLLECTION)
        return _collection

def close_mongo() -> None:
    global _client, _collection
    with _lock:
        if _client is not None:
            _client.close()
        _client = None
        _collection = None

def append_order_event(event: dict) -> None:
    if not settings.MONGO_URL:
        raise RuntimeError("MONGO_URL is not configured; order was journaled locally but MongoDB persistence is unavailable")
    collection = connect_mongo()
    if collection is None:
        raise RuntimeError("MongoDB collection could not be initialized")
    local_now = now_ist()
    date_key = local_now.strftime("%Y-%m-%d")
    payload = dict(event)
    payload.setdefault("created_time", local_now.isoformat())
    payload["last_modified_time"] = local_now.isoformat()
    try:
        collection.update_one(
            {"_id": date_key},
            {
                "$setOnInsert": {"date": date_key, "created_time": local_now},
                "$set": {"last_modified_time": local_now},
                "$push": {"orders": payload},
            },
            upsert=True,
        )
    except PyMongoError:
        logger.exception("Failed to persist order event to MongoDB")
        raise
