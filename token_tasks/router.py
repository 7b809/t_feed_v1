"""
token_tasks/router.py
HTTP endpoints for the token.

SECURITY:
    No endpoint returns the raw access token, user id, user name, or the
    raw Upstox payload. Every response is limited to status, timestamps,
    source, broker, and validation state.
"""
from fastapi import APIRouter, HTTPException

from core.logger import get_logger
from token_tasks.schemas import (
    SaveTokenRequest,
    SaveTokenResponse,
    TokenCacheStatus,
    TokenDocSafe,
    ValidateTokenRequest,
    ValidationResult,
)
from token_tasks.service import token_service

logger = get_logger(__name__)

router = APIRouter(prefix="/token", tags=["token"])


# ── cache reads (safe) ───────────────────────────────────────────
@router.get("/status", response_model=TokenCacheStatus)
def token_status() -> TokenCacheStatus:
    """Return cache metadata (no raw token, no user info)."""
    logger.info("GET /token/status")
    return TokenCacheStatus(**token_service.get_cache_status())


@router.get("/doc", response_model=TokenDocSafe)
def get_token_doc() -> TokenDocSafe:
    """
    Return a SAFE projection of the cached token document.

    Explicitly strips: access_token, last_profile_user_id, last_profile_user_name.
    """
    logger.info("GET /token/doc")
    doc = token_service.get_token_doc()
    if not doc:
        raise HTTPException(status_code=404, detail="Token document not loaded")

    safe_doc = {
        "_id": str(doc.get("_id")),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "source": doc.get("source"),
        "last_validation_status": doc.get("last_validation_status"),
        "last_validation_status_text": doc.get("last_validation_status_text"),
        "last_validated_at": doc.get("last_validated_at"),
        "last_validation_error": doc.get("last_validation_error"),
        "last_profile_broker": doc.get("last_profile_broker"),
    }
    return TokenDocSafe(**safe_doc)


# ── mutations (safe responses) ───────────────────────────────────
@router.post("/save", response_model=SaveTokenResponse)
def save_token(payload: SaveTokenRequest) -> SaveTokenResponse:
    """
    Validate + save the given access token into MongoDB.

    - Runs Upstox profile validation first.
    - Upserts the token doc (updates `updated_at`, `source`,
      validation fields; sets `created_at` only on first insert).
    - Refreshes the in-memory cache with the newly saved token.

    Response contains NO token, NO user id/name — only status + timestamps.
    """
    logger.info("POST /token/save | source=%s", payload.source)

    result = token_service.save_token(
        access_token=payload.access_token,
        source=payload.source,
    )

    if not result.get("saved"):
        raise HTTPException(
            status_code=500,
            detail=result.get("error", "Failed to save token"),
        )

    # Pydantic will drop any extra keys (user_id, user_name, raw, ...)
    return SaveTokenResponse(**result)


@router.post("/validate", response_model=ValidationResult)
def validate_token(payload: ValidateTokenRequest) -> ValidationResult:
    """
    Validate a token WITHOUT saving it.

    - Body may include `access_token`; if omitted, the cached token is used.
    - Response contains only status / broker / source — no user info.
    """
    logger.info(
        "POST /token/validate | mode=%s",
        "explicit" if payload.access_token else "cache",
    )
    result = token_service.validate_only(payload.access_token)
    return ValidationResult(**result)


@router.post("/validate-cached", response_model=ValidationResult)
def validate_cached_token() -> ValidationResult:
    """
    Validate the token currently in the in-memory cache.
    Response contains only status / broker / source — no user info.
    """
    logger.info("POST /token/validate-cached")
    result = token_service.validate_cached()
    return ValidationResult(**result)


@router.post("/refresh", response_model=TokenCacheStatus)
def refresh_token() -> TokenCacheStatus:
    """Force an immediate reload of the token document from MongoDB."""
    logger.info("POST /token/refresh")
    doc = token_service.refresh_token()
    if not doc:
        raise HTTPException(
            status_code=502,
            detail="Failed to refresh token from MongoDB",
        )
    return TokenCacheStatus(**token_service.get_cache_status())