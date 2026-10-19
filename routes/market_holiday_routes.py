"""Admin routes for the market holiday service.

    GET  /admin/market-holiday              -> today's holiday payload
    GET  /admin/market-holiday/{date_str}   -> holiday payload for a date
    POST /admin/market-holiday/clear-cache  -> clear the in-process cache
    GET  /admin/market-holiday/cache-status -> cache diagnostics

All responses are JSON-safe. Failure responses still return HTTP 200
with ``status="error"`` in the body so callers do not need to special-
case transport errors. The exception is a malformed ``date_str`` — that
returns HTTP 400 so callers can distinguish “bad input” from
“provider unavailable”.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException

from services.market_holiday_service import market_holiday_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/market-holiday", tags=["market-holiday"])


@router.get("")
def get_today_holiday() -> dict[str, Any]:
    """Return today's holiday payload."""
    return market_holiday_service.get_today()


@router.get("/cache-status")
def cache_status() -> dict[str, Any]:
    """Return a light snapshot of the in-process cache."""
    payload = market_holiday_service.last_status()
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    return payload


@router.post("/clear-cache")
def clear_cache() -> dict[str, Any]:
    """Clear the in-process holiday cache."""
    market_holiday_service.clear_cache()
    return {"status": "ok", "cleared_at": datetime.now(timezone.utc).isoformat()}


@router.get("/{date_str}")
def get_holiday_for_date(date_str: str) -> dict[str, Any]:
    """Return the holiday payload for ``date_str``.

    ``date_str`` must be ``YYYY-MM-DD`` (``YYYY/MM/DD`` also works).
    A malformed date returns HTTP 400; a provider failure returns
    HTTP 200 with ``status="error"`` so it matches the today route.
    """
    payload = market_holiday_service.get_holiday(date_str)

    if payload.get("status") == "error" and str(payload.get("error") or "").startswith(
        "invalid_date"
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_date",
                "message": (
                    "date_str must be a valid date in YYYY-MM-DD form."
                ),
                "date_str": date_str,
            },
        )

    return payload