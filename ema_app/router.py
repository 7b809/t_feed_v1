"""HTTP endpoints for the EMA app."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from core.logger import get_logger
from ema_app.service import ema_service
from ema_app.ws_manager import ema_ws_manager

logger = get_logger(__name__)

router = APIRouter(prefix="/ema-app", tags=["ema-app"])


@router.get("/status", summary="Service status")
async def status() -> Dict[str, Any]:
    s = ema_service.status()
    s["ws_clients"] = ema_ws_manager.client_count()
    return s


@router.get("/instruments", summary="Tracked instruments with state")
async def instruments() -> Dict[str, Any]:
    items = ema_service.list_instruments()
    return {"count": len(items), "instruments": items}


@router.get(
    "/opening-range/{instrument_key:path}",
    summary="Opening range levels for a contract",
)
async def opening_range(instrument_key: str) -> Dict[str, Any]:
    data = ema_service.get_opening_range(instrument_key)
    if not data:
        raise HTTPException(status_code=404, detail="Instrument not tracked")
    return data


@router.get("/state/{instrument_key:path}", summary="Current EMA state for a contract")
async def state(instrument_key: str) -> Dict[str, Any]:
    snap = ema_service.get_state(instrument_key)
    if not snap:
        raise HTTPException(status_code=404, detail="Instrument not tracked")
    return snap


@router.get("/crosses/today", summary="All crosses recorded today")
async def crosses_today() -> Dict[str, Any]:
    items = ema_service.crosses_today()
    return {"count": len(items), "crosses": items}


@router.get("/crosses/{instrument_key:path}", summary="Crosses for a single instrument")
async def crosses_for_instrument(instrument_key: str) -> Dict[str, Any]:
    items = ema_service.crosses_for_instrument(instrument_key)
    return {"count": len(items), "instrument_key": instrument_key, "crosses": items}


@router.post(
    "/backfill",
    summary="Force backfill EMA state + opening range + intraday crosses",
)
async def backfill(force: bool = False) -> Dict[str, Any]:
    done = ema_service.backfill_all(force=force)
    return {"ok": True, "instruments": ema_service.instrument_count(), "backfilled": done}


@router.post("/finalize-now", summary="Force-finalize the current minute now")
async def finalize_now() -> Dict[str, Any]:
    n = ema_service.finalize_current_minute()
    return {"ok": True, "finalized": n}