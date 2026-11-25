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

    def force_refresh_tokens(self) -> bool:
        """Perform an unconditional MongoDB read and replace the token cache.

        This method always issues find_one() against the configured token
        document. It never treats the in-process cache as the source of truth.
        A missing document, empty token, or database error raises so mandatory
        refresh workflows can stop before reconnecting with a stale token.

        Returns True only when an already-initialized cached token changed.
        The raw token is never logged or returned.
        """
        doc = self._get_collection().find_one(
            {"_id": config.UPSTOX_TOKEN_DOC_ID}
        )
        if not isinstance(doc, dict):
            raise RuntimeError("Configured Upstox token document was not found.")

        new_token = str(doc.get("access_token") or "").strip()
        if not new_token:
            raise RuntimeError("Upstox token document has no access_token.")

        with self._lock:
            old_token = str(self._cache.get("access_token") or "").strip()
            self._cache = dict(doc)

        changed = bool(old_token and old_token != new_token)
        if not old_token:
            logger.info("[token-refresh] in-process token cache initialized from forced MongoDB read.")
        elif changed:
            logger.info("[token-refresh] forced MongoDB read found a new access token; cache updated.")
        else:
            logger.info("[token-refresh] forced MongoDB read succeeded; access token unchanged.")
        return changed

    def refresh_tokens(self) -> bool:
        """Refresh the cache while retaining the last known-good token on errors.

        The periodic monitor uses this tolerant wrapper. Mandatory refresh
        workflows should call force_refresh_tokens() so a failed MongoDB read
        is distinguishable from an unchanged token.
        """
        try:
            return self.force_refresh_tokens()
        except Exception as exc:
            logger.error(
                "Error refreshing tokens from MongoDB (%s); retaining cached token.",
                type(exc).__name__,
            )
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
