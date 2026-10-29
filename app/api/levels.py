# app/api/levels.py
#
# REST API for Job 3 (pivot levels).
#
# Endpoints
# ---------
#   GET  /api/v1/levels                            all indexes, cached
#   GET  /api/v1/levels/available                  static catalog
#   GET  /api/v1/levels/db                         MongoDB persisted doc (verification)
#   GET  /api/v1/levels/{canonical_or_key}         one index, cached
#   POST /api/v1/levels/refresh                    force recompute all
#   POST /api/v1/levels/refresh/{canonical_or_key} force recompute one
#
# Auth
# ----
# Read endpoints (GET) are public.
# Write endpoints (POST) require X-API-Key when API_KEY is set.

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.config import INDEX_METADATA, settings
from app.services.levels_service import LevelsError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/levels", tags=["levels"])


def _service(request: Request):
    svc = getattr(request.app.state, "levels_service", None)
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Levels service unavailable",
        )
    return svc


def _project_info(request: Request):
    svc = getattr(request.app.state, "project_info_service", None)
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Project info service unavailable",
        )
    return svc


def _authorize(x_api_key: str | None) -> None:
    if settings.api_key:
        if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
            )


# --------------------------------------------------------------------------- #
@router.get("")
def get_all_levels(request: Request) -> dict[str, Any]:
    svc = _service(request)
    return svc.get_all()


@router.get("/available")
def get_available_indexes() -> dict[str, Any]:
    """Static list of canonical names + folder tags clients can query."""
    return {
        "indexes": [
            {
                "canonical": meta.get("canonical"),
                "folder": meta.get("folder"),
                "instrument_key": key,
                "priority": meta.get("priority"),
                "trading_symbol": meta.get("trading_symbol"),
            }
            for key, meta in sorted(
                INDEX_METADATA.items(), key=lambda kv: int(kv[1].get("priority", 999))
            )
        ]
    }


@router.get("/db")
def get_levels_db(request: Request) -> dict[str, Any]:
    """Return the MongoDB-persisted levels doc (fixed _id)."""
    svc = _project_info(request)
    try:
        doc = svc.get_levels()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No levels document in DB yet (no successful scheduled/startup/manual run)",
        )
    return doc


@router.get("/{canonical_or_key:path}")
def get_one_levels(request: Request, canonical_or_key: str) -> dict[str, Any]:
    svc = _service(request)
    try:
        return svc.get_one(canonical_or_key)
    except LevelsError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


@router.post("/refresh")
async def refresh_all_levels(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _authorize(x_api_key)
    svc = _service(request)
    try:
        return await svc.compute_all(trigger="manual")
    except LevelsError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


@router.post("/refresh/{canonical_or_key:path}")
async def refresh_one_levels(
    request: Request,
    canonical_or_key: str,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _authorize(x_api_key)
    svc = _service(request)
    try:
        return await svc.compute_one(canonical_or_key, trigger="manual")
    except LevelsError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc