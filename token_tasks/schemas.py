"""
token_tasks/schemas.py
Pydantic models for the token router.

IMPORTANT:
    Response models deliberately EXCLUDE sensitive fields
    (access_token, user_id, user_name, raw API payload).
    They only expose status / timestamps / source / broker.
"""
from typing import Optional

from pydantic import BaseModel, Field


# ── Requests (contain sensitive input, never echoed back) ────────
class SaveTokenRequest(BaseModel):
    access_token: str = Field(..., description="Upstox access token")
    source: str = Field("api", description="Where the token came from (api / telegram / ...)")


class ValidateTokenRequest(BaseModel):
    access_token: Optional[str] = Field(
        None,
        description="Upstox access token. If omitted, the cached token is used.",
    )


# ── Responses (safe — no token, no user id/name, no raw payload) ─
class ValidationResult(BaseModel):
    """Safe validation outcome. No token / user identity fields."""
    valid: bool
    status: str                       # "success" | "error"
    status_text: str                  # "profile_success" | "profile_failed" | ...
    broker: Optional[str] = None      # e.g. "UPSTOX"
    message: str
    token_source: Optional[str] = None  # "explicit" | "cache" | "none"


class SaveTokenResponse(BaseModel):
    """Safe save outcome. No token / user identity fields."""
    saved: bool
    valid: bool
    created: Optional[bool] = None
    updated: Optional[bool] = None
    updated_at: Optional[str] = None
    source: Optional[str] = None
    validation: ValidationResult


class TokenCacheStatus(BaseModel):
    """Safe cache-status snapshot. Never contains the token value."""
    cached: bool
    collection: str
    doc_id: str
    refresh_interval_seconds: int
    refresh_count: int
    last_loaded_at: Optional[str] = None
    last_error: Optional[str] = None
    has_access_token: bool
    source: Optional[str] = None
    doc_updated_at: Optional[str] = None
    last_validation_status: Optional[str] = None


class TokenDocSafe(BaseModel):
    """
    Safe projection of the stored token document.
    NOTE: no `access_token`, no `last_profile_user_id`,
          no `last_profile_user_name`.
    """
    _id: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    source: Optional[str] = None
    last_validation_status: Optional[str] = None
    last_validation_status_text: Optional[str] = None
    last_validated_at: Optional[str] = None
    last_validation_error: Optional[str] = None
    last_profile_broker: Optional[str] = None