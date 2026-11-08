# core/token_service.py
"""Fetch and validate the Upstox access token from MongoDB."""

import upstox_client
from upstox_client.rest import ApiException

from core.config import UPSTOX_API_VERSION, APP_TOKENS_COLLECTION
from core.mongo_client import get_mongo_db
from core.logger import get_logger

logger = get_logger(__file__)

TOKEN_DOC_ID = "upstox_access_token"


def fetch_access_token() -> str:
    """Fetch the access token document from MongoDB."""
    db = get_mongo_db()
    doc = db[APP_TOKENS_COLLECTION].find_one({"_id": TOKEN_DOC_ID})
    if not doc:
        raise RuntimeError(f"Token document '{TOKEN_DOC_ID}' not found.")
    token = doc.get("access_token")
    if not token:
        raise RuntimeError("access_token field is missing.")
    logger.info("Access token fetched (updated_at=%s)", doc.get("updated_at"))
    return token


def validate_access_token(token: str) -> bool:
    """Validate the token by calling Upstox ``get_profile``.

    Only the pass/fail outcome is logged. The profile payload returned by
    Upstox (user id, name, broker, email, …) is deliberately **not**
    logged to avoid leaking account details into log files.
    """
    configuration = upstox_client.Configuration()
    configuration.access_token = token
    api_client = upstox_client.ApiClient(configuration)
    user_api = upstox_client.UserApi(api_client)
    try:
        # Call is made; response is intentionally discarded (not logged).
        user_api.get_profile(UPSTOX_API_VERSION)
        logger.info("Token valid.")
        return True
    except ApiException as exc:
        logger.error("Token validation failed: %s", exc)
        return False