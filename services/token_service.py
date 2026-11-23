import logging
from threading import Lock

from pymongo import MongoClient

from core import config

logger = logging.getLogger(__name__)


class TokenService:
    def __init__(self):
        self._client = None
        self._collection = None
        self._cache = {}
        self._lock = Lock()

    def _get_collection(self):
        if self._collection is None:
            self._client = MongoClient(config.MONGO_URI)
            self._collection = self._client[config.UPSTOX_TOKEN_MONGO_DB][config.TOKENS_COLLECTION]
        return self._collection

    def refresh_tokens(self) -> bool:
        """Refresh the in-process token snapshot from MongoDB.

        Returns True only when a new non-empty token replaces the cached
        token. Transient DB failures and empty documents retain the last
        known-good token so the live feed is not blanked accidentally.
        """
        try:
            doc = self._get_collection().find_one({"_id": config.UPSTOX_TOKEN_DOC_ID})
            if not isinstance(doc, dict):
                logger.warning("Configured Upstox token document was not found; retaining cached token.")
                return False
            new_token = str(doc.get("access_token") or "").strip()
            if not new_token:
                logger.warning("Upstox token document has no access_token; retaining cached token.")
                return False
            with self._lock:
                old_token = str(self._cache.get("access_token") or "").strip()
                self._cache = dict(doc)
            changed = bool(old_token and old_token != new_token)
            if not old_token:
                logger.info("Upstox token cache initialized from MongoDB.")
            elif changed:
                logger.info("Upstox access token changed in MongoDB; in-process cache updated.")
            else:
                logger.debug("Upstox token cache is already current.")
            return changed
        except Exception as exc:
            logger.error("Error refreshing tokens from MongoDB (%s); retaining cached token.", type(exc).__name__)
            return False

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
            logger.error("Failed to reload access token (%s).", type(exc).__name__)

        return None

    def get_token_document(self) -> dict:
        with self._lock:
            return self._cache.copy()


token_service = TokenService()
