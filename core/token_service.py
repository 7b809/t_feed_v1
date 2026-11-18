"""Load the Upstox access token from the shared UPSTOX_APP_V2 token document."""
from __future__ import annotations

from core.config import APP_TOKENS_COLLECTION, TOKEN_DOC_ID
from core.logger import get_logger
from core.mongo_client import get_mongo_db

logger = get_logger(__file__)


def fetch_access_token() -> str:
    """Read app_tokens._id=upstox_access_token and return access_token."""
    collection = get_mongo_db()[APP_TOKENS_COLLECTION]
    document = collection.find_one({"_id": TOKEN_DOC_ID})
    if not document:
        raise RuntimeError(
            f"MongoDB token document not found: {APP_TOKENS_COLLECTION}/{TOKEN_DOC_ID}"
        )
    token = document.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("MongoDB token document has no usable access_token")
    logger.debug("Upstox access token loaded from MongoDB (updated_at=%s)",
                 document.get("updated_at"))
    return token.strip()
