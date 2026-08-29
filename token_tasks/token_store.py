from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pymongo import MongoClient

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


class TokenStore:
    """
    MongoDB token store for Upstox access token.

    Expected MongoDB document:

        {
            "_id": "upstox_access_token",
            "access_token": "eyJ0eXAiOiJK...",
            "created_at": "2026-08-13T07:16:44.000000+05:30",
            "updated_at": "2026-08-13T07:16:44.000000+05:30"
        }

    Behavior:
        - Reads current Upstox token from MongoDB.
        - Saves new token from Telegram /save_token flow.
        - Updates validation status after background token checks.
        - Never returns or logs the raw token.
    """

    def __init__(self):
        self.mongo_uri = config.MONGO_URI
        self.mongo_db = config.MONGO_DB
        self.collection_name = config.TOKENS_COLLECTION

        self.token_doc_id = getattr(
            config,
            "UPSTOX_TOKEN_DOC_ID",
            "upstox_access_token",
        )

        self.market_timezone = self._load_market_timezone()

        if not self.mongo_uri:
            raise RuntimeError("MONGO_URI / MONGO_URL is not configured.")

        if not self.mongo_db:
            raise RuntimeError("MONGO_DB is not configured.")

        if not self.collection_name:
            raise RuntimeError("TOKENS_COLLECTION is not configured.")

        self.client = MongoClient(self.mongo_uri)
        self.db = self.client[self.mongo_db]
        self.collection = self.db[self.collection_name]

    def _load_market_timezone(self):
        """
        Loads market timezone from config.
        Falls back to Asia/Kolkata if configured timezone is invalid.
        """

        timezone_name = getattr(config, "MARKET_TIMEZONE", "Asia/Kolkata")

        try:
            return ZoneInfo(timezone_name)

        except ZoneInfoNotFoundError:
            logger.error(
                f"Invalid MARKET_TIMEZONE configured: {timezone_name}. "
                "Falling back to Asia/Kolkata."
            )
            return ZoneInfo("Asia/Kolkata")

    def now_iso(self) -> str:
        """
        Returns current market datetime in ISO format.
        """

        return datetime.now(self.market_timezone).isoformat()

    def get_token_document(self) -> dict | None:
        """
        Returns current token document from MongoDB.

        Expected query:
            {"_id": "upstox_access_token"}
        """

        try:
            return self.collection.find_one(
                {
                    "_id": self.token_doc_id,
                }
            )

        except Exception as ex:
            logger.error(
                f"Failed reading token document from MongoDB: "
                f"{type(ex).__name__}: {ex}"
            )
            return None

    def get_access_token(self) -> str | None:
        """
        Returns raw access token from MongoDB.

        Important:
            This method returns raw token only for internal API calls.
            Do not log this value.
            Do not send this value to Telegram.
        """

        doc = self.get_token_document()

        if not doc:
            return None

        token = doc.get("access_token")

        if not token:
            return None

        token_text = str(token).strip()

        if not token_text:
            return None

        return token_text

    def save_access_token(
        self,
        access_token: str,
        source: str = "telegram",
    ) -> dict:
        """
        Saves new Upstox access token to MongoDB.

        Requirement:
            When user sends a new token through Telegram /save_token:
                - Save raw token in MongoDB.
                - Set created_at to current datetime.
                - Set updated_at to current datetime.
                - Replace existing token document.
                - Never return raw token to Telegram/logs.

        Returns:
            Sanitized saved document with access_token masked.
        """

        access_token = str(access_token or "").strip()

        if not access_token:
            raise ValueError("access_token is empty.")

        now_text = self.now_iso()

        payload = {
            "_id": self.token_doc_id,
            "access_token": access_token,
            "created_at": now_text,
            "updated_at": now_text,
            "source": source,
            "last_validation_status": None,
            "last_validation_status_text": None,
            "last_validated_at": None,
            "last_validation_error": None,
            "last_profile_user_id": None,
            "last_profile_user_name": None,
            "last_profile_broker": None,
        }

        try:
            self.collection.replace_one(
                {
                    "_id": self.token_doc_id,
                },
                payload,
                upsert=True,
            )

            logger.info(
                f"Upstox access token saved to MongoDB. "
                f"doc_id={self.token_doc_id}, source={source}"
            )

            sanitized_payload = dict(payload)
            sanitized_payload["access_token"] = self.mask_token(access_token)

            return sanitized_payload

        except Exception as ex:
            logger.error(
                f"Failed saving Upstox access token to MongoDB: "
                f"{type(ex).__name__}: {ex}"
            )
            raise

    def update_validation_status(
        self,
        is_valid: bool,
        status: str,
        error: str | None = None,
        profile: dict | None = None,
    ) -> bool:
        """
        Updates token validation metadata.

        Used by:
            - Background token monitor.
            - Manual Telegram /profile validation.
            - Telegram /save_token validation flow.
        """

        now_text = self.now_iso()

        update_payload = {
            "last_validation_status": "success" if is_valid else "failed",
            "last_validation_status_text": status,
            "last_validated_at": now_text,
            "last_validation_error": error,
        }

        if isinstance(profile, dict):
            profile_data = profile.get("data") or {}

            if isinstance(profile_data, dict):
                update_payload["last_profile_user_id"] = profile_data.get("user_id")
                update_payload["last_profile_user_name"] = profile_data.get("user_name")
                update_payload["last_profile_broker"] = profile_data.get("broker")

        try:
            result = self.collection.update_one(
                {
                    "_id": self.token_doc_id,
                },
                {
                    "$set": update_payload,
                },
                upsert=True,
            )

            logger.info(
                f"Token validation status updated. "
                f"doc_id={self.token_doc_id}, "
                f"is_valid={is_valid}, "
                f"status={status}, "
                f"matched={result.matched_count}, "
                f"modified={result.modified_count}"
            )

            return True

        except Exception as ex:
            logger.error(
                f"Failed updating token validation status: "
                f"{type(ex).__name__}: {ex}"
            )
            return False

    def mask_token(self, token: str | None) -> str | None:
        """
        Masks token for safe display.

        Example:
            eyJ0eXAiOiJK...abc123
        """

        token_text = str(token or "").strip()

        if not token_text:
            return None

        if len(token_text) <= 20:
            return "***masked***"

        return f"{token_text[:12]}...{token_text[-6:]}"

    def get_masked_token_info(self) -> dict:
        """
        Returns safe token document summary for Telegram /token-status.

        Raw access_token is never returned.
        """

        doc = self.get_token_document() or {}

        raw_token = str(doc.get("access_token") or "").strip()

        return {
            "_id": doc.get("_id"),
            "token_available": bool(raw_token),
            "access_token_masked": self.mask_token(raw_token),
            "created_at": doc.get("created_at"),
            "updated_at": doc.get("updated_at"),
            "source": doc.get("source"),
            "last_validation_status": doc.get("last_validation_status"),
            "last_validation_status_text": doc.get("last_validation_status_text"),
            "last_validated_at": doc.get("last_validated_at"),
            "last_validation_error": doc.get("last_validation_error"),
            "last_profile_user_id": doc.get("last_profile_user_id"),
            "last_profile_user_name": doc.get("last_profile_user_name"),
            "last_profile_broker": doc.get("last_profile_broker"),
        }

    def touch_updated_at(self) -> bool:
        """
        Updates only updated_at field.

        This is optional utility if another workflow wants to mark token document touched.
        """

        now_text = self.now_iso()

        try:
            self.collection.update_one(
                {
                    "_id": self.token_doc_id,
                },
                {
                    "$set": {
                        "updated_at": now_text,
                    }
                },
                upsert=True,
            )

            return True

        except Exception as ex:
            logger.error(f"Failed touching token updated_at: {type(ex).__name__}: {ex}")
            return False


token_store = TokenStore()
