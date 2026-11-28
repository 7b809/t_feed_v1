"""Admin-only historical Supertrend replay API."""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from services.supertrend_analysis_service import analyse_instrument

router = APIRouter(prefix="/admin/supertrend", tags=["admin-supertrend-analysis"])


@router.get("/analysis")
async def historical_supertrend_analysis(
    instrument_key: str = Query(
        ..., min_length=3, max_length=180,
        description="Full Upstox key; active: NSE_INDEX|Nifty 50; expired: NSE_FO|12345|27-11-2026.",
    ),
    target_date: date = Query(..., description="Target trading date in YYYY-MM-DD format."),
    atr_period: int = Query(default=10, ge=2, le=100),
    multiplier: float = Query(default=3.0, gt=0, le=20),
) -> dict[str, Any]:
    """Replay 1-minute Supertrend over target_date-10d through target_date+1d."""
    try:
        return analyse_instrument(instrument_key, target_date, atr_period, multiplier)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Supertrend analysis failed: {type(exc).__name__}"
        ) from exc
