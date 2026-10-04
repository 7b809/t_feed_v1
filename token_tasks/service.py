"""
token_tasks/service.py

Single-owner in-memory cache for the Upstox access token.

Responsibilities
----------------
- Load the token document from MongoDB into a plain in-memory object.
- Expose a safe accessor (`get_access_token`) for internal consumers.
- Provide an upsert path (`upsert_token`) used by the API and telegram.
- Provide `save_and_reload`, which validates a new token, upserts it, and
  refreshes the cache in one step.
- Never leak the raw token through any "status"/"meta" helper.

Concurrency
-----------
All state is guarded by a single `threading.RLock`.

Logging
-------
Every failure is logged as a single-line summary
`service::<method> failed | step=<what> | reason=<type: short msg>`.

Mongo interface discovery
-------------------------
Different MongoManager implementations expose the database differently.
`_collection()` tries, in order:
  1. mongo_manager.get_database()
  2. mongo_manager.get_db()
  3. mongo_manager.database
  4. mongo_manager.db
  5. mongo_manager.client[DB_NAME]      (raw pymongo client)
  6. mongo_manager._client[DB_NAME]     (private fallback)

Profile validation
------------------
Profile validation is delegated to `token_tasks._profile.validate_profile`,
which probes the `upstox_app.profile` package for any of several known
function names and falls back to the raw Upstox SDK if none is found.
This module no longer imports `get_profile_status` directly, because that
name has moved between versions of the project.
"""
import threading
from datetime import datetime
from typing import Any, Dict, Optional

from core.config import core_config, mongo_manager
from core.logger import get_logger
from token_tasks.config import token_config

logger = get_logger(__name__)


def _short(exc: BaseException) -> str:
    """Compact 'Type: message' string, capped to keep log lines readable."""
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if len(msg) > 160:
        msg = msg[:160] + "…"
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _db_name() -> str:
    """Resolve the database name from every plausible config location."""
    for attr in ("MONGO_DB_NAME", "DB_NAME", "MONGO_DATABASE"):
        value = getattr(core_config, attr, None)
        if value:
            return str(value)
    return "trading"


def _resolve_database():
    """
    Return a pymongo Database handle from whatever interface the current
    MongoManager exposes. Raises RuntimeError if none of the probes work.
    """
    mm = mongo_manager

    # 1-2) method-based access
    for name in ("get_database", "get_db", "get_database_name"):
        fn = getattr(mm, name, None)
        if callable(fn):
            try:
                db = fn() if name != "get_database_name" else None
                if db is not None and not isinstance(db, str):
                    return db
            except Exception:  # noqa: BLE001
                pass

    # 3-4) attribute-based access
    for name in ("database", "db"):
        db = getattr(mm, name, None)
        if db is not None and not isinstance(db, str):
            # Some managers expose `db` as the DB *name* string.
            if hasattr(db, "get_collection") or hasattr(db, "__getitem__"):
                return db

    # 5-6) raw pymongo client fallback
    for client_attr in ("client", "_client"):
        client = getattr(mm, client_attr, None)
        if client is None:
            continue
        try:
            return client[_db_name()]
        except Exception:  # noqa: BLE001
            pass

    raise RuntimeError(
        "MongoManager exposes neither get_database()/db/database nor a "
        "pymongo client attribute"
    )


class TokenService:
    """
    Mongo-backed in-memory token cache.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cached_doc: Optional[Dict[str, Any]] = None
        self._loaded_at: Optional[str] = None
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Collection / database helpers
    # ------------------------------------------------------------------

    def _collection(self):
        """Return the pymongo collection holding the token document."""
        db = _resolve_database()
        return db[token_config.COLLECTION_NAME]

    @property
    def doc_id(self) -> str:
        return token_config.DOC_ID

    # ------------------------------------------------------------------
    # Cache lifecycle
    # ------------------------------------------------------------------

    def load_token(self) -> bool:
        """Load (or reload) the token from MongoDB into the cache."""
        try:
            collection = self._collection()
        except Exception as exc:  # noqa: BLE001
            self._last_error = _short(exc)
            logger.error(
                "service::load_token failed | step=resolve collection | reason=%s",
                self._last_error,
            )
            return False

        try:
            doc = collection.find_one({"_id": self.doc_id})
        except Exception as exc:  # noqa: BLE001
            self._last_error = _short(exc)
            logger.error(
                "service::load_token failed | step=mongo find_one | reason=%s",
                self._last_error,
            )
            return False

        with self._lock:
            if doc is None:
                self._cached_doc = None
                self._loaded_at = datetime.now().astimezone().isoformat()
                self._last_error = "document not found"
                logger.warning(
                    "service::load_token | doc missing | _id=%s", self.doc_id
                )
                return False

            self._cached_doc = dict(doc)
            self._loaded_at = datetime.now().astimezone().isoformat()
            self._last_error = None

        logger.info(
            "service::load_token | loaded | source=%s | updated_at=%s",
            self._cached_doc.get("source", "unknown"),
            self._cached_doc.get("updated_at", "unknown"),
        )
        return True

    def refresh_token(self) -> bool:
        """Alias used by the scheduler loop."""
        return self.load_token()

    def clear_cache(self) -> None:
        """Drop the in-memory cache without touching MongoDB."""
        with self._lock:
            self._cached_doc = None
            self._loaded_at = None
        logger.info("service::clear_cache | cache cleared")

    # ------------------------------------------------------------------
    # Safe accessors
    # ------------------------------------------------------------------

    def get_access_token(self) -> Optional[str]:
        with self._lock:
            if not self._cached_doc:
                return None
            token = self._cached_doc.get("access_token")
        return str(token) if token else None

    def is_cache_loaded(self) -> bool:
        with self._lock:
            return bool(self._cached_doc and self._cached_doc.get("access_token"))

    def get_cached_doc(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._cached_doc) if self._cached_doc else None

    def status_meta(self) -> Dict[str, Any]:
        """Safe metadata for /token/status. No token / identity fields."""
        with self._lock:
            doc = self._cached_doc or {}
            return {
                "loaded": bool(doc.get("access_token")),
                "doc_id": self.doc_id,
                "loaded_at": self._loaded_at,
                "source": doc.get("source"),
                "updated_at": doc.get("updated_at"),
                "last_error": self._last_error,
            }

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------

    def upsert_token(self, access_token: str, source: str = "manual") -> bool:
        """Upsert a token document into MongoDB (no validation, no cache touch)."""
        if not access_token:
            logger.warning("service::upsert_token | empty token rejected")
            return False

        now_iso = datetime.now().astimezone().isoformat()
        doc = {
            "_id": self.doc_id,
            "access_token": access_token,
            "source": source,
            "updated_at": now_iso,
        }

        try:
            collection = self._collection()
            collection.replace_one({"_id": self.doc_id}, doc, upsert=True)
        except Exception as exc:  # noqa: BLE001
            self._last_error = _short(exc)
            logger.error(
                "service::upsert_token failed | step=mongo replace_one | reason=%s",
                self._last_error,
            )
            return False

        logger.info(
            "service::upsert_token | saved | source=%s | updated_at=%s",
            source, now_iso,
        )
        return True

    # ------------------------------------------------------------------
    # Validate + upsert + reload in one step
    # ------------------------------------------------------------------

    def save_and_reload(
        self, access_token: str, source: str = "manual"
    ) -> Dict[str, Any]:
        """
        Validate an explicit token, upsert it into MongoDB, and reload the
        local cache. Returns:
            {"saved": bool, "validated": bool, "error": str | None}
        Never raises.

        Validation goes through `token_tasks._profile.validate_profile`,
        which probes several known function names on the
        `upstox_app.profile` package and falls back to the raw Upstox SDK.
        """
        result: Dict[str, Any] = {
            "saved": False,
            "validated": False,
            "error": None,
        }

        if not access_token:
            result["error"] = "empty token"
            return result

        # 1) Validate via the shared profile resolver.
        try:
            from token_tasks._profile import validate_profile
            profile = validate_profile(access_token)
            valid = bool(profile.get("valid")) if isinstance(profile, dict) else False
            profile_error = profile.get("error") if isinstance(profile, dict) else None
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"validation error: {_short(exc)}"
            logger.error(
                "service::save_and_reload failed | step=validate token | reason=%s",
                _short(exc),
            )
            return result

        if not valid:
            result["error"] = profile_error or "profile rejected token"
            logger.warning(
                "service::save_and_reload | token rejected by profile API | reason=%s",
                result["error"],
            )
            return result
        result["validated"] = True

        # 2) Upsert into MongoDB
        if not self.upsert_token(access_token, source=source):
            result["error"] = "mongo upsert failed"
            return result

        # 3) Reload cache
        if not self.load_token():
            result["error"] = "cache reload failed"
            return result

        result["saved"] = True
        logger.info("service::save_and_reload | success | source=%s", source)
        return result


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

token_service = TokenService()