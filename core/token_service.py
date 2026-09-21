import logging
from threading import Lock
from time import monotonic

from pymongo import MongoClient

from core import config

logger = logging.getLogger(__name__)


class TokenService:
    def __init__(self) -> None:
        self._client = MongoClient(config.MONGO_URI)
        self._db = self._client[config.MONGO_DB]
        self._collection = self._db[config.TOKENS_COLLECTION]
        self._cache: dict = {}
        self._last_refresh = 0.0
        self._lock = Lock()

    def refresh_tokens(self) -> None:
        logger.info("Fetching token document from MongoDB")
        doc = self._collection.find_one({"_id": config.TOKEN_DOCUMENT_ID})
        if not doc or not str(doc.get("access_token") or "").strip():
            raise RuntimeError(f"Token document {config.TOKEN_DOCUMENT_ID!r} is missing or invalid")
        with self._lock:
            self._cache = dict(doc)
            self._last_refresh = monotonic()
        logger.info("Token cache refreshed successfully")

    def get_access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            token = str(self._cache.get("access_token") or "").strip()
            stale = monotonic() - self._last_refresh >= config.TOKEN_REFRESH_SECONDS
        if force_refresh or not token or stale:
            self.refresh_tokens()
            with self._lock:
                token = str(self._cache.get("access_token") or "").strip()
        if not token:
            raise RuntimeError("Upstox access token is unavailable")
        return token

    def get_token_document(self) -> dict:
        with self._lock:
            return self._cache.copy()

    def close(self) -> None:
        self._client.close()


token_service = TokenService()
