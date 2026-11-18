"""Thread-safe MongoDB client singleton."""
from __future__ import annotations

import threading
import pymongo
from pymongo.database import Database

from core.config import MONGO_DB_NAME, MONGO_URI
from core.logger import get_logger

logger = get_logger(__file__)
_client: pymongo.MongoClient | None = None
_db: Database | None = None
_lock = threading.Lock()


def get_mongo_db() -> Database:
    """Connect once, verify connectivity, and reuse the database handle."""
    global _client, _db
    if _db is not None:
        return _db

    with _lock:
        if _db is None:
            client = None
            try:
                client = pymongo.MongoClient(
                    MONGO_URI,
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=5000,
                )
                client.admin.command("ping")
                _client = client
                _db = client[MONGO_DB_NAME]
                logger.info("Connected to MongoDB database %s", MONGO_DB_NAME)
            except Exception:
                if client is not None:
                    client.close()
                _client = None
                _db = None
                logger.exception("MongoDB connection failed")
                raise
    return _db


def close_mongo_connection() -> None:
    global _client, _db
    with _lock:
        if _client is not None:
            _client.close()
        _client = None
        _db = None
