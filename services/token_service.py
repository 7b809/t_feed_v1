import logging
from threading import Lock

from pymongo import MongoClient

from core import config

logger = logging.getLogger(__name__)


class TokenService:
    def __init__(self):
        self._client = MongoClient(config.MONGO_URI)
        self._db = self._client[config.MONGO_DB]
        self._collection = self._db[config.TOKENS_COLLECTION]

        self._cache = {}
        self._lock = Lock()

    def refresh_tokens(self) -> None:
        try:
            logger.info("Fetching token document from MongoDB...")

            doc = self._collection.find_one({"_id": "upstox_access_token"})

            if doc:
                with self._lock:
                    self._cache = doc

                logger.info("Token cache refreshed successfully.")
            else:
                logger.warning("Document '_id: upstox_access_token' not found.")

        except Exception as exc:
            logger.error(
                "Error refreshing tokens from MongoDB: %s",
                exc,
            )

    def get_access_token(self) -> str | None:
        with self._lock:
            token = self._cache.get("access_token")

        if token:
            return token

        try:
            self.refresh_tokens()

            with self._lock:
                return self._cache.get("access_token")

        except Exception as exc:
            logger.error(
                "Failed to reload access token: %s",
                exc,
            )

        return None

    def get_token_document(self) -> dict:
        with self._lock:
            return self._cache.copy()


token_service = TokenService()
