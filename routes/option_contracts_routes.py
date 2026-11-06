# routes/option_contracts_routes.py
"""FastAPI routes for Upstox option-contracts.

Endpoints
---------
GET  /admin/option-contracts/fetch          Filtered list of option contracts
GET  /admin/option-contracts/nearest        Nearest weekly expiry (convenience)
GET  /admin/option-contracts/find           Single contract by strike + type
GET  /admin/option-contracts/expiries       Available expiry dates
POST /admin/option-contracts/clear-cache    Drop the TTL cache

Query parameters for ``/fetch`` (all optional — defaults come from config):

    underlying_key    Upstox instrument key (default: NSE_INDEX|Nifty 50)
    strike_min        inclusive lower strike bound (default: 21000)
    strike_max        inclusive upper strike bound (default: 25000)
    expiry_date       "YYYY-MM-DD" or a relative keyword:
                      current_week | next_week | far_week |
                      current_month | next_month | far_month |
                      "all" (no expiry filter)
    instrument_type   "CE" | "PE" (omit for both)
    force_refresh     bypass the TTL cache

``/find`` accepts ``strike`` (required, positive) and ``instrument_type``
(required, CE|PE). Returns 404 when no contract matches, with a
diagnostic body that includes ``nearest_expiry`` and
``available_expiries``.

Registration — add to ``main.py`` / ``app.py`` (once, alongside the other
FastAPI routers):

    from routes.option_contracts_routes import router as option_contracts_router
    app.include_router(option_contracts_router)

See also:
    * services/option_contracts_service.py   — implementation
    * routes/upstox_proxy.py                 — same FastAPI pattern
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from services.option_contracts_service import (
    OptionContractsServiceError,
    option_contracts_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/option-contracts",
    tags=["option-contracts"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _call_service(fn, *args, **kwargs) -> dict[str, Any]:
    """Invoke a service method, translating service errors to HTTP errors.

    ``OptionContractsServiceError`` becomes ``HTTPException(502)`` —
    the service could not complete due to an upstream problem (token,
    HTTP, non-success payload). Anything else becomes a 500.
    """
    try:
        return fn(*args, **kwargs)
    except OptionContractsServiceError as exc:
        logger.warning("[option-contracts] service error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[option-contracts] service crashed")
        raise HTTPException(status_code=500, detail=str(exc))


def _validate_instrument_type(value: str | None) -> str | None:
    """Normalize + validate ``instrument_type``. Returns ``None`` when omitted."""
    if value is None:
        return None
    it = str(value).strip().upper()
    if not it:
        return None
    if it not in ("CE", "PE"):
        raise HTTPException(
            status_code=400,
            detail=f"instrument_type must be 'CE' or 'PE' (got {value!r})",
        )
    return it


def _validate_strike_range(
    strike_min: float | None, strike_max: float | None,
) -> None:
    """Reject obviously-bad strike bounds up front with a 400."""
    if (strike_min is not None and strike_max is not None
            and strike_min > strike_max):
        raise HTTPException(
            status_code=400,
            detail=(
                f"strike_min ({strike_min}) must be <= strike_max "
                f"({strike_max})"
            ),
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("/fetch")
def fetch_option_contracts(
    underlying_key: str | None = Query(
        default=None,
        description="Upstox instrument key (default: NSE_INDEX|Nifty 50)",
    ),
    strike_min: float | None = Query(
        default=None,
        ge=0,
        description="Inclusive lower strike bound (default: 21000)",
    ),
    strike_max: float | None = Query(
        default=None,
        ge=0,
        description="Inclusive upper strike bound (default: 25000)",
    ),
    expiry_date: str | None = Query(
        default=None,
        description=(
            "YYYY-MM-DD or a relative keyword: current_week, next_week, "
            "far_week, current_month, next_month, far_month, or 'all'"
        ),
    ),
    instrument_type: str | None = Query(
        default=None,
        description="CE, PE, or omit for both",
    ),
    force_refresh: bool = Query(
        default=False,
        description="Bypass the TTL cache",
    ),
) -> dict[str, Any]:
    """Fetch option contracts filtered by strike range + type + expiry."""
    itype = _validate_instrument_type(instrument_type)
    _validate_strike_range(strike_min, strike_max)

    return _call_service(
        option_contracts_service.fetch,
        underlying_key=underlying_key,
        strike_min=strike_min,
        strike_max=strike_max,
        expiry_date=expiry_date,
        instrument_type=itype,
        force_refresh=force_refresh,
    )


@router.get("/nearest")
def fetch_nearest(
    underlying_key: str | None = Query(
        default=None,
        description="Upstox instrument key (default: NSE_INDEX|Nifty 50)",
    ),
    strike_min: float | None = Query(default=None, ge=0),
    strike_max: float | None = Query(default=None, ge=0),
    force_refresh: bool = Query(default=False),
) -> dict[str, Any]:
    """Nearest weekly expiry, both CE+PE, default strike range.

    Convenience wrapper around ``/fetch`` with
    ``expiry_date="current_week"`` and ``instrument_type=None``.
    """
    _validate_strike_range(strike_min, strike_max)

    return _call_service(
        option_contracts_service.fetch,
        underlying_key=underlying_key,
        strike_min=strike_min,
        strike_max=strike_max,
        expiry_date="current_week",
        instrument_type=None,
        force_refresh=force_refresh,
    )


@router.get("/find")
def find_option_contract(
    strike: float = Query(..., description="Strike price (positive)"),
    instrument_type: str = Query(..., description="CE or PE"),
    underlying_key: str | None = Query(
        default=None,
        description="Upstox instrument key (default: NSE_INDEX|Nifty 50)",
    ),
    expiry_date: str | None = Query(
        default=None,
        description="YYYY-MM-DD or a relative keyword (default: current_week)",
    ),
    force_refresh: bool = Query(default=False),
) -> Any:
    """Look up a single contract by strike + type.

    Returns 200 with ``{"status": "ok", "contract": {...}}`` on success.
    Returns 404 with ``{"status": "not_found", ...}`` and a diagnostic
    body (``nearest_expiry``, ``available_expiries``) when no contract
    matches.
    """
    if strike <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"strike must be positive (got {strike})",
        )

    itype = _validate_instrument_type(instrument_type)
    if itype is None:
        # instrument_type is a required query param but the string could
        # still be blank — guard against that explicitly.
        raise HTTPException(
            status_code=400,
            detail="instrument_type is required (CE or PE)",
        )

    result = _call_service(
        option_contracts_service.find_contract,
        strike=strike,
        instrument_type=itype,
        underlying_key=underlying_key,
        expiry_date=expiry_date,
        force_refresh=force_refresh,
    )

    if result.get("status") != "ok":
        return JSONResponse(content=result, status_code=404)
    return result


@router.get("/expiries")
def list_expiries(
    underlying_key: str | None = Query(
        default=None,
        description="Upstox instrument key (default: NSE_INDEX|Nifty 50)",
    ),
    force_refresh: bool = Query(default=False),
) -> dict[str, Any]:
    """List every available expiry date for an underlying."""
    return _call_service(
        option_contracts_service.list_expiries,
        underlying_key=underlying_key,
        force_refresh=force_refresh,
    )


@router.post("/clear-cache")
def clear_cache() -> dict[str, Any]:
    """Drop every cached option-contracts entry."""
    return _call_service(option_contracts_service.clear_cache)