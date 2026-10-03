
"""
token_tasks/service.py

Responsibilities:
    - In-memory cache of the Upstox token document.
    - Load token document from MongoDB.
    - Refresh token cache every 30 minutes.
    - Validate tokens against Upstox.
    - Save / update token document with validation metadata.
    - Provide cache status and validation helpers.

Security:
    - Never log the access token.
    - Never log a token preview.
    - Never expose the access token through cache-status responses.
"""

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from core.config import mongo_manager
from core.logger import get_logger
from token_tasks.config import token_config
from token_tasks.validator import UPSTOX_BROKER, validate_upstox_token


logger = get_logger(__name__)


# IST (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    """Return current time in IST."""
    return datetime.now(IST)


def now_ist_iso() -> str:
    """Return current time in IST as an ISO-8601 string."""
    return now_ist().isoformat()


class TokenService:
    """
    In-memory cache of the token document and MongoDB write helpers.

    The actual access token is kept only internally and is never
    intentionally written to logs.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

        self._cached_doc: Optional[Dict[str, Any]] = None

        self._last_loaded_at: Optional[datetime] = None

        self._last_error: Optional[str] = None

        self._refresh_count: int = 0

    # =========================================================
    # MongoDB
    # =========================================================

    @property
    def collection(self):
        """Return the configured token collection."""

        return mongo_manager.get_db()[
            token_config.COLLECTION_NAME
        ]

    # =========================================================
    # Load / Refresh
    # =========================================================

    def load_token(self) -> Optional[Dict[str, Any]]:
        """
        Fetch the token document from MongoDB and replace
        the in-memory cache.

        Returns:
            Token document if successful, otherwise None.
        """

        doc_id = token_config.DOC_ID
        collection_name = token_config.COLLECTION_NAME

        logger.info(
            "Loading token document | collection=%s | doc_id=%s",
            collection_name,
            doc_id,
        )

        try:
            doc = self.collection.find_one(
                {"_id": doc_id}
            )

        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)

            logger.exception(
                "MongoDB read failed for token document"
            )

            return None

        if not doc:
            self._last_error = (
                f"Token document '{doc_id}' "
                f"not found in '{collection_name}'"
            )

            logger.warning(
                "Token document not found | doc_id=%s",
                doc_id,
            )

            return None

        # -----------------------------------------------------
        # Update in-memory cache
        # -----------------------------------------------------

        with self._lock:
            self._cached_doc = doc
            self._last_loaded_at = now_ist()
            self._last_error = None
            self._refresh_count += 1

        # -----------------------------------------------------
        # IMPORTANT:
        # Never log access_token or any token preview.
        # -----------------------------------------------------

        logger.info(
            "Token cache updated successfully | "
            "doc_updated_at=%s | source=%s",
            doc.get("updated_at"),
            doc.get("source"),
        )

        return doc

    def refresh_token(self) -> Optional[Dict[str, Any]]:
        """
        Refresh the in-memory token cache.

        Called by the scheduler every configured interval.
        """

        logger.info(
            "Refreshing token cache from MongoDB"
        )

        return self.load_token()

    # =========================================================
    # Save / Update
    # =========================================================

    def save_token(
        self,
        access_token: str,
        source: str = "api",
    ) -> Dict[str, Any]:
        """
        Validate the supplied token and upsert it into MongoDB.

        The following fields are updated:

            access_token
            updated_at
            source
            last_validation_status
            last_validation_status_text
            last_validated_at
            last_validation_error
            last_profile_user_id
            last_profile_user_name
            last_profile_broker

        created_at is only set on first insert.

        The in-memory cache is refreshed after a successful
        database write.

        IMPORTANT:
            The access token is never logged.
        """

        access_token = (
            access_token or ""
        ).strip()

        source = (
            source or "api"
        ).strip().lower()

        doc_id = token_config.DOC_ID
        collection_name = token_config.COLLECTION_NAME

        # -----------------------------------------------------
        # Request logging
        # -----------------------------------------------------

        logger.info(
            "save_token requested | source=%s | token_present=%s",
            source,
            bool(access_token),
        )

        # -----------------------------------------------------
        # Validate token against Upstox
        # -----------------------------------------------------

        validation = validate_upstox_token(
            access_token
        )

        now = now_ist_iso()

        # -----------------------------------------------------
        # Build MongoDB update payload
        # -----------------------------------------------------

        update_fields: Dict[str, Any] = {
            "access_token": access_token,
            "updated_at": now,
            "source": source,
            "last_validation_status": validation[
                "status"
            ],
            "last_validation_status_text": validation[
                "status_text"
            ],
            "last_validated_at": now,
            "last_validation_error": (
                None
                if validation["valid"]
                else validation["message"]
            ),
            "last_profile_user_id": validation[
                "user_id"
            ],
            "last_profile_user_name": validation[
                "user_name"
            ],
            "last_profile_broker": validation.get(
                "broker",
                UPSTOX_BROKER,
            ),
        }

        # -----------------------------------------------------
        # MongoDB upsert
        # -----------------------------------------------------

        try:
            result = self.collection.update_one(
                {"_id": doc_id},
                {
                    "$set": update_fields,
                    "$setOnInsert": {
                        "created_at": now
                    },
                },
                upsert=True,
            )

        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to upsert token document"
            )

            return {
                "saved": False,
                "valid": validation["valid"],
                "validation": validation,
                "error": (
                    "Failed to save token document"
                ),
            }

        logger.info(
            "Token document upserted | "
            "collection=%s | "
            "doc_id=%s | "
            "matched=%s | "
            "created=%s | "
            "validation=%s",
            collection_name,
            doc_id,
            result.matched_count,
            bool(result.upserted_id),
            validation["status"],
        )

        # -----------------------------------------------------
        # Refresh in-memory cache
        # -----------------------------------------------------

        cached_doc = self.load_token()

        if cached_doc is None:
            logger.warning(
                "Token document saved but cache refresh failed"
            )

        else:
            logger.info(
                "Token cache refreshed after token save"
            )

        # -----------------------------------------------------
        # Return result
        # -----------------------------------------------------

        return {
            "saved": True,
            "valid": validation["valid"],
            "validation": validation,
            "created": bool(
                result.upserted_id
            ),
            "updated": result.matched_count > 0,
            "updated_at": now,
            "source": source,
        }

    # =========================================================
    # Validation
    # =========================================================

    def validate_only(
        self,
        access_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Validate a token without persisting it.

        If access_token is supplied:
            Validate the supplied token.

        If access_token is not supplied:
            Validate the currently cached token.
        """

        mode = (
            "explicit"
            if access_token
            else "cache"
        )

        logger.info(
            "validate_only requested | mode=%s",
            mode,
        )

        return validate_upstox_token(
            access_token
        )

    def validate_cached(self) -> Dict[str, Any]:
        """
        Validate the token currently in memory.

        Does not read MongoDB.

        Returns an error immediately if no token
        is currently cached.
        """

        cached_token = self.get_access_token()

        if not cached_token:
            logger.warning(
                "validate_cached called but token cache is empty"
            )

            return {
                "valid": False,
                "status": "error",
                "status_text": "cache_empty",
                "user_id": None,
                "user_name": None,
                "broker": UPSTOX_BROKER,
                "message": "No token in cache",
                "token_source": "cache",
            }

        logger.info(
            "Validating cached token"
        )

        return validate_upstox_token(
            cached_token
        )

    # =========================================================
    # Cache Accessors
    # =========================================================

    def get_token_doc(
        self,
    ) -> Optional[Dict[str, Any]]:
        """
        Return the cached token document.

        Internal callers may use this when they actually
        need the complete document.
        """

        with self._lock:
            return self._cached_doc

    def get_access_token(
        self,
    ) -> Optional[str]:
        """
        Return the cached access token.

        IMPORTANT:
            Caller must never log this value.
        """

        doc = self.get_token_doc()

        if not doc:
            return None

        return doc.get("access_token")

    def get_cache_status(
        self,
    ) -> Dict[str, Any]:
        """
        Return safe cache status information.

        The actual access token is NEVER included.
        """

        with self._lock:
            doc = self._cached_doc

            return {
                "cached": doc is not None,

                "collection": (
                    token_config.COLLECTION_NAME
                ),

                "doc_id": token_config.DOC_ID,

                "refresh_interval_seconds": (
                    token_config.REFRESH_INTERVAL_SECONDS
                ),

                "refresh_count": (
                    self._refresh_count
                ),

                "last_loaded_at": (
                    self._last_loaded_at.isoformat()
                    if self._last_loaded_at
                    else None
                ),

                "last_error": (
                    self._last_error
                ),

                "has_access_token": bool(
                    doc
                    and doc.get("access_token")
                ),

                "source": (
                    doc.get("source")
                    if doc
                    else None
                ),

                "doc_updated_at": (
                    doc.get("updated_at")
                    if doc
                    else None
                ),

                "last_validation_status": (
                    doc.get(
                        "last_validation_status"
                    )
                    if doc
                    else None
                ),
            }

    # =========================================================
    # Cache Management
    # =========================================================

    def clear_cache(self) -> None:
        """
        Clear the in-memory token cache.

        Does not delete or modify the MongoDB document.
        """

        with self._lock:
            self._cached_doc = None
            self._last_loaded_at = None

        logger.info(
            "Token cache cleared"
        )


# =============================================================
# Module-level singleton
# =============================================================

token_service = TokenService()
