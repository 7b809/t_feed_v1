# app/services/mongodb_service.py
from __future__ import annotations

import logging

from pymongo import MongoClient
from pymongo.database import Database

from ..config import Settings

logger = logging.getLogger(__name__)


class MongoDbService:
    """Thin wrapper around pymongo.MongoClient."""

    def __init__(self, config: Settings) -> None:
        self._config = config
        self._client: MongoClient | None = None
        self._db: Database | None = None

    def connect(self) -> None:
        self._client = MongoClient(
            self._config.mongo_url,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
        )
        self._db = self._client[self._config.mongo_database]
        

    @property
    def db(self) -> Database:
        if self._db is None:
            raise RuntimeError("MongoDbService.connect() must be called first")
        return self._db

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None
                self._db = None


def _mask_url(url: str) -> str:
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, tail = rest.rpartition("@")
    return f"{scheme}://***@{tail}"