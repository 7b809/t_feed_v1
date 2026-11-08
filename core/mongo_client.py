# core/mongo_client.py
"""MongoDB client singleton."""

import pymongo
from pymongo.database import Database

from core.config import MONGO_URI, MONGO_DB_NAME
from core.logger import get_logger

logger = get_logger(__file__)

_client = None
_db = None


def get_mongo_db() -> Database:
    """Return a MongoDB database instance (singleton)."""
    global _client, _db
    if _db is None:
        try:
            _client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
            _client.server_info()  # force connection check
            _db = _client[MONGO_DB_NAME]
            logger.info("Connected to MongoDB database: %s", MONGO_DB_NAME)
        except Exception as exc:
            logger.exception("Failed to connect to MongoDB")
            raise
    return _db