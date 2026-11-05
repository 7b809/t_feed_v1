# upstox_app/apis/position_routes.py
"""FastAPI router: position listing, locking, and selective exit.

Endpoints
---------
    GET  /api/positions                        List every open position
                                               (each annotated with
                                               ``locked`` + ``lock_meta``).

    GET  /api/positions/locks                  List the persistent lock
                                               registry contents.

    POST /api/positions/lock                   Lock one or many positions.

    POST /api/positions/unlock                 Unlock one or many.

    POST /api/positions/locks/clear            Clear the entire registry.

    POST /api/positions/exit                   Exit positions.

                                               By default exits only
                                               non-locked ones. To
                                               include locked positions
                                               send::

                                                 {
                                                   "dry_run": false,
                                                   "include_locked": true,
                                                   "confirm": "CONFIRM_EXIT_LOCKED"
                                                 }

                                               IMPORTANT: on this HTTP
                                               route, ``dry_run`` defaults
                                               to **true** — you must
                                               explicitly send
                                               ``"dry_run": false`` to
                                               place real exit orders.

Lock request body (``/lock``, ``/unlock``)
------------------------------------------
::

    {
      "instrument_token":  "NSE_FO|43919",    # OR
      "instrument_tokens": ["NSE_FO|43919", "NSE_FO|43920"],
      "reason":            "hedge",           # optional
      "note":              "hold expiry",     # optional
      "source":            "dashboard",       # optional; default "api"
      "locked_by":         "session-user",    # optional
      "snapshot": {                            # optional, single-token only
        "trading_symbol":  "NIFTY24NOV24000CE",
        "quantity":        65,
        "side":            "LONG",
        "product":         "D",
        "average_price":   100.0,
        "last_price":      110.0
      }
    }

``snapshot`` is only applied when a **single** ``instrument_token``
is being locked; it is ignored for bulk locks (each locked entry has
its own ``instrument_token`` and only that token's snapshot would
make sense).

Authentication
--------------
Per-route auth is **not** applied here — ``main.py`` mounts this
router under ``app.include_router(position_api_router,
dependencies=_AUTH)``, so ``require_any_auth`` (Bearer / X-API-Key /
Basic / session-cookie) runs before every handler.

HTTP status mapping (see ``_STATUS_MAP`` below)
-----------------------------------------------
    ok / dry_run / partial        → 200
    no_token                      → 424 Failed Dependency
    disabled                      → 409 Conflict
    invalid_input                 → 400 Bad Request
    error + confirmation_required → 400 Bad Request
    error + lock_registry_*       → 503 Service Unavailable
    error otherwise               → 502 Bad Gateway
    unknown                       → 500 Internal Server Error

``no_token`` deliberately uses 424 rather than 401: it means "the
Upstox broker access token is missing / expired" — an upstream
problem, not an app-session problem. Returning 401 here would trip
the browser's global fetch interceptor in ``templates/base.html``
(which redirects on *any* 401) and yank a logged-in user back to
``/login`` for no reason.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from upstox_app.services.exit_all_positions import (
    CONFIRM_EXIT_LOCKED,
    exit_all_positions,
)
from upstox_app.services.get_positions import get_positions
from upstox_app.services.position_lock_service import (
    clear_all,
    list_locked,
    lock,
    lock_many,
    unlock,
    unlock_many,
)

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/positions",
    tags=["positions"],
)


# --------------------------------------------------------------------------- #
# Request models                                                              #
# --------------------------------------------------------------------------- #
class LockRequest(BaseModel):
    """Body for ``POST /api/positions/lock`` and ``/unlock``."""

    instrument_token: str | None = Field(default=None)
    instrument_tokens: list[str] | None = Field(default=None)
    reason: str | None = Field(default=None)
    note: str | None = Field(default=None)
    source: str | None = Field(default="api")
    locked_by: str | None = Field(default=None)
    snapshot: dict[str, Any] | None = Field(default=None)


class ExitRequest(BaseModel):
    """Body for ``POST /api/positions/exit``."""

    dry_run: bool = Field(default=True)
    include_locked: bool = Field(default=False)
    confirm: str | None = Field(default=None)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
_STATUS_MAP: dict[str, int] = {
    "ok": 200,
    "dry_run": 200,
    "partial": 200,
    "no_token": 424,
    "disabled": 409,
    "invalid_input": 400,
}


def _response(result: dict[str, Any]) -> JSONResponse:
    """Serialise a service result into a ``JSONResponse`` with the
    correct HTTP status code."""
    status = str(result.get("status") or "").lower()

    if status == "error":
        err = str(result.get("error") or "")
        if err == "confirmation_required":
            return JSONResponse(content=result, status_code=400)
        if err.startswith("lock_registry_"):
            return JSONResponse(content=result, status_code=503)
        return JSONResponse(content=result, status_code=502)

    http_code = _STATUS_MAP.get(status, 500)
    return JSONResponse(content=result, status_code=http_code)


def _extract_tokens(payload: LockRequest) -> list[str]:
    raw: list[str] = []
    if payload.instrument_token:
        raw.append(str(payload.instrument_token).strip())
    if payload.instrument_tokens:
        for t in payload.instrument_tokens:
            if t is not None:
                raw.append(str(t).strip())

    seen: set[str] = set()
    out: list[str] = []
    for t in raw:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


# --------------------------------------------------------------------------- #
# GET /api/positions                                                          #
# --------------------------------------------------------------------------- #
@router.get("")
async def api_list_positions(
    locked_only: bool = Query(default=False),
    unlocked_only: bool = Query(default=False),
) -> JSONResponse:
    """Return every open position, each annotated with lock status."""
    result = get_positions()

    if result.get("status") == "ok" and (locked_only or unlocked_only):
        positions = result.get("positions") or []
        if locked_only:
            positions = [p for p in positions if p.get("locked")]
        if unlocked_only:
            positions = [p for p in positions if not p.get("locked")]
        result = dict(result)
        result["positions"] = positions
        result["total"] = len(positions)
        result["locked_count"] = sum(
            1 for p in positions if p.get("locked")
        )
        result["unlocked_count"] = len(positions) - result["locked_count"]

    return _response(result)


# --------------------------------------------------------------------------- #
# GET /api/positions/locks                                                    #
# --------------------------------------------------------------------------- #
@router.get("/locks")
async def api_list_locks() -> JSONResponse:
    """Return the contents of the persistent lock registry."""
    result = list_locked()
    if result.get("status") == "ok":
        return JSONResponse(content=result, status_code=200)
    return JSONResponse(content=result, status_code=503)


# --------------------------------------------------------------------------- #
# POST /api/positions/lock                                                    #
# --------------------------------------------------------------------------- #
@router.post("/lock")
async def api_lock_position(
    payload: LockRequest = Body(default_factory=LockRequest),
) -> JSONResponse:
    """Lock one or many positions."""
    tokens = _extract_tokens(payload)
    if not tokens:
        return JSONResponse(
            content={
                "status": "invalid_input",
                "error": "instrument_token or instrument_tokens required",
            },
            status_code=400,
        )

    source = payload.source or "api"

    if len(tokens) == 1:
        result = lock(
            tokens[0],
            reason=payload.reason,
            note=payload.note,
            source=source,
            locked_by=payload.locked_by,
            snapshot=payload.snapshot,
        )
    else:
        result = lock_many(
            tokens,
            reason=payload.reason,
            note=payload.note,
            source=source,
            locked_by=payload.locked_by,
        )

    if result.get("status") == "error" and str(
        result.get("error") or ""
    ).startswith("mongodb"):
        return JSONResponse(
            content=result,
            status_code=503,
        )
    return _response(result)


# --------------------------------------------------------------------------- #
# POST /api/positions/unlock                                                  #
# --------------------------------------------------------------------------- #
@router.post("/unlock")
async def api_unlock_position(
    payload: LockRequest = Body(default_factory=LockRequest),
) -> JSONResponse:
    """Unlock one or many positions."""
    tokens = _extract_tokens(payload)
    if not tokens:
        return JSONResponse(
            content={
                "status": "invalid_input",
                "error": "instrument_token or instrument_tokens required",
            },
            status_code=400,
        )

    if len(tokens) == 1:
        result = unlock(tokens[0])
    else:
        result = unlock_many(tokens)

    if result.get("status") == "error" and str(
        result.get("error") or ""
    ).startswith("mongodb"):
        return JSONResponse(
            content=result,
            status_code=503,
        )
    return _response(result)


# --------------------------------------------------------------------------- #
# POST /api/positions/locks/clear                                             #
# --------------------------------------------------------------------------- #
@router.post("/locks/clear")
async def api_clear_locks() -> JSONResponse:
    """Clear the entire lock registry."""
    result = clear_all()
    if result.get("status") == "ok":
        return JSONResponse(content=result, status_code=200)
    return JSONResponse(content=result, status_code=503)


# --------------------------------------------------------------------------- #
# POST /api/positions/exit                                                    #
# --------------------------------------------------------------------------- #
@router.post("/exit")
async def api_exit_positions(
    payload: ExitRequest = Body(default_factory=ExitRequest),
) -> JSONResponse:
    """Exit open positions.

    ``dry_run`` defaults to ``True`` on this route — pass
    ``"dry_run": false`` explicitly to place real exit orders.
    """
    result = exit_all_positions(
        dry_run=payload.dry_run,
        include_locked=payload.include_locked,
        confirm=payload.confirm,
    )
    return _response(result)


__all__ = ["router", "CONFIRM_EXIT_LOCKED"]