"""
token_tasks/router.py

HTTP endpoints under /token/*.

Endpoints
---------
GET  /token/status           Safe cache metadata (no token / identity).
GET  /token/doc              Safe projection of the token document.
POST /token/save             Validate, save, notify, schedule hard refresh.
POST /token/validate         Validate an explicit or cached token.
POST /token/validate-cached  Validate only the cached token.
POST /token/refresh          Force a reload from MongoDB.

Rules
-----
- No endpoint ever returns the raw access_token, user_id or user_name.
- Every failure is logged as one compact line:
      router::<method> failed | step=<stage> | reason=<type: short msg>
- /token/save triggers a background hard refresh after a successful save.

Self-contained
--------------
Response models are declared locally so this module does not depend on
specific class names inside token_tasks.schemas. Request models are
imported from schemas.py if present, otherwise a local fallback is used.
"""

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.logger import get_logger
from telegram_app.manager import telegram_manager
from token_tasks.config import token_config
from token_tasks.service import token_service

logger = get_logger(__name__)

router = APIRouter(prefix="/token", tags=["token"])


# ---------------------------------------------------------------------------
# Request models (prefer schemas.py, fall back to local declarations)
# ---------------------------------------------------------------------------

try:  # pragma: no cover - optional import
    from token_tasks.schemas import (  # type: ignore
        SaveTokenRequest,
        ValidateTokenRequest,
    )

    _HAS_SCHEMAS = True
except Exception:  # noqa: BLE001
    _HAS_SCHEMAS = False

    class SaveTokenRequest(BaseModel):  # type: ignore[no-redef]
        access_token: str = Field(..., min_length=1)
        source: Optional[str] = "api"

    class ValidateTokenRequest(BaseModel):  # type: ignore[no-redef]
        access_token: Optional[str] = None


# ---------------------------------------------------------------------------
# Response models (declared locally to avoid schema drift)
# ---------------------------------------------------------------------------


class TokenStatusResponse(BaseModel):
    loaded: bool
    doc_id: str
    loaded_at: Optional[str] = None
    source: Optional[str] = None
    updated_at: Optional[str] = None
    last_error: Optional[str] = None


class TokenActionResponse(BaseModel):
    ok: bool
    message: str
    token_source: Optional[str] = None


# ---------------------------------------------------------------------------
# validator imports (kept optional so an unfinished validator doesn't block)
# ---------------------------------------------------------------------------

try:  # pragma: no cover - optional import
    from token_tasks.validator import (  # type: ignore
        validate_access_token,
        validate_cached_token,
    )

    _HAS_VALIDATOR = True
except Exception:  # noqa: BLE001
    _HAS_VALIDATOR = False

    def validate_access_token(token: str) -> Dict[str, Any]:  # type: ignore[no-redef]
        try:
            from upstox_app.profile.get_profile_status import get_profile_status

            profile = get_profile_status(token)
            valid = (
                bool(profile.get("valid"))
                if isinstance(profile, dict)
                else bool(profile)
            )
            return {"valid": valid, "error": None if valid else "profile rejected"}
        except Exception as exc:  # noqa: BLE001
            return {"valid": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}

    def validate_cached_token() -> Dict[str, Any]:  # type: ignore[no-redef]
        token = token_service.get_access_token()
        if not token:
            return {"valid": False, "error": "no cached token"}
        return validate_access_token(token)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _short(exc: BaseException) -> str:
    """Compact 'Type: message' summary, single line, capped length."""
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if len(msg) > 160:
        msg = msg[:160] + "…"
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _safe_projection(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Strip every secret and identity field before returning a document."""
    if not doc:
        return None
    return {
        "source": doc.get("source"),
        "updated_at": doc.get("updated_at"),
        "has_token": bool(doc.get("access_token")),
    }


async def _trigger_hard_refresh(trigger: str) -> None:
    """
    Fire-and-forget hard refresh. Imported lazily so this module has no
    dependency on core.lifespan at import time.
    """
    try:
        from core.lifespan import _run_hard_refresh
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "router::_trigger_hard_refresh failed | step=import | reason=%s",
            _short(exc),
        )
        return
    try:
        await _run_hard_refresh(trigger)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "router::_trigger_hard_refresh failed | step=run | reason=%s",
            _short(exc),
        )


# ---------------------------------------------------------------------------
# status / doc
# ---------------------------------------------------------------------------


@router.get("/status", response_model=TokenStatusResponse)
def get_status() -> TokenStatusResponse:
    try:
        meta = token_service.status_meta()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "router::get_status failed | step=status_meta | reason=%s",
            _short(exc),
        )
        raise HTTPException(status_code=500, detail="status unavailable")

    return TokenStatusResponse(
        loaded=meta.get("loaded", False),
        doc_id=meta.get("doc_id", token_config.DOC_ID),
        loaded_at=meta.get("loaded_at"),
        source=meta.get("source"),
        updated_at=meta.get("updated_at"),
        last_error=meta.get("last_error"),
    )


@router.get("/doc")
def get_doc() -> Dict[str, Any]:
    try:
        doc = token_service.get_cached_doc()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "router::get_doc failed | step=get_cached_doc | reason=%s",
            _short(exc),
        )
        raise HTTPException(status_code=500, detail="doc unavailable")

    if not doc:
        return {"found": False, "doc": None}
    return {"found": True, "doc": _safe_projection(doc)}


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


@router.post("/save", response_model=TokenActionResponse)
async def save_token(payload: SaveTokenRequest) -> TokenActionResponse:
    """
    1. Validate the submitted token via the Upstox Profile API.
    2. Upsert it into MongoDB.
    3. Reload the local cache.
    4. Notify Telegram.
    5. Schedule a hard refresh in the background.
    """
    source = ((getattr(payload, "source", None) or "api") or "api").strip()

    try:
        result = token_service.save_and_reload(payload.access_token, source=source)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "router::save_token failed | step=save_and_reload | reason=%s",
            _short(exc),
        )
        raise HTTPException(status_code=500, detail="save failed")

    if not result.get("saved"):
        reason = result.get("error") or "unknown"
        logger.warning("router::save_token rejected | reason=%s", reason)
        raise HTTPException(status_code=400, detail=reason)

    # Notify telegram. Safe even when telegram is disabled.
    try:
        telegram_manager.notify_token_saved(source=source)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "router::save_token failed | step=telegram notify | reason=%s",
            _short(exc),
        )

    # Schedule the hard refresh on the running event loop.
    try:
        asyncio.create_task(_trigger_hard_refresh("api_after_save"))
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "router::save_token failed | step=schedule refresh | reason=%s",
            _short(exc),
        )

    return TokenActionResponse(
        ok=True,
        message="token saved; hard refresh scheduled",
        token_source=source,
    )


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@router.post("/validate", response_model=TokenActionResponse)
def validate_token(payload: ValidateTokenRequest) -> TokenActionResponse:
    """
    Validate an explicit token if provided, otherwise the cached one.
    """
    try:
        explicit = getattr(payload, "access_token", None)
        if explicit:
            outcome = validate_access_token(explicit)
            token_source = "explicit"
        else:
            outcome = validate_cached_token()
            token_source = "cache"
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "router::validate_token failed | step=validate | reason=%s",
            _short(exc),
        )
        raise HTTPException(status_code=500, detail="validation failed")

    valid = bool(outcome.get("valid"))
    return TokenActionResponse(
        ok=valid,
        message="token valid" if valid else (outcome.get("error") or "token invalid"),
        token_source=token_source,
    )


@router.post("/validate-cached", response_model=TokenActionResponse)
def validate_cached() -> TokenActionResponse:
    try:
        outcome = validate_cached_token()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "router::validate_cached failed | step=validate | reason=%s",
            _short(exc),
        )
        raise HTTPException(status_code=500, detail="validation failed")

    valid = bool(outcome.get("valid"))
    return TokenActionResponse(
        ok=valid,
        message=(
            "cached token valid"
            if valid
            else (outcome.get("error") or "cached token invalid")
        ),
        token_source="cache",
    )


# ---------------------------------------------------------------------------
# refresh (from MongoDB into local cache)
# ---------------------------------------------------------------------------


@router.post("/refresh", response_model=TokenActionResponse)
def refresh_from_mongo() -> TokenActionResponse:
    try:
        loaded = token_service.load_token()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "router::refresh_from_mongo failed | step=load_token | reason=%s",
            _short(exc),
        )
        raise HTTPException(status_code=500, detail="refresh failed")

    if not loaded:
        return TokenActionResponse(
            ok=False,
            message="token not found or reload failed",
            token_source="none",
        )
    return TokenActionResponse(
        ok=True,
        message="cache refreshed from MongoDB",
        token_source="mongo",
    )
