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

        # Thread-safe in-memory cache
        self._cache = {}
        self._lock = Lock()

    def refresh_tokens(self) -> None:
        """Fetches the token document from MongoDB and updates the cache."""
        try:
            logger.info("Fetching token document from MongoDB...")
            # Query for the specific single document
            doc = self._collection.find_one({"_id": "upstox_access_token"})

            if doc:
                with self._lock:
                    self._cache = doc
                logger.info("Token cache refreshed successfully.")
            else:
                logger.warning(
                    "Document '_id: upstox_access_token' not found in MongoDB."
                )
        except Exception as e:
            logger.error(f"Error refreshing tokens from MongoDB: {e}")

    def get_access_token(self) -> str | None:
        """Returns the current access token from in-memory cache."""
        with self._lock:
            return self._cache.get("access_token")

    def get_token_document(self) -> dict:
        """Returns the full cached token document."""
        with self._lock:
            return self._cache.copy()


# Singleton instance to be shared across the application
token_service = TokenService()
