"""Opening Range (OR) level API.

    GET  /admin/opening-range
        -> list today's OR levels for all registered instruments
        -> ?only_today=false to include yesterday's stale files

    GET  /admin/opening-range/{instrument_key}
        -> today's OR levels for one instrument

    POST /admin/opening-range/recalculate
        -> force a full recalc pass (equivalent to the scheduled job)

    POST /admin/opening-range/recalculate/{instrument_key}
        -> force a recalc for a single instrument
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from services.instrument_registry import instrument_registry
from services.opening_range_service import opening_range_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/opening-range", tags=["opening-range"])


@router.get("")
def list_opening_ranges(
    only_today: bool = Query(
        default=True,
        description="When true, skip instruments whose saved OR file is not for today.",
    ),
) -> dict[str, Any]:
    """Return OR payloads for every registered instrument."""
    return opening_range_service.list_all(only_today=only_today)


@router.get("/{instrument_key:path}")
def get_opening_range(instrument_key: str) -> dict[str, Any]:
    """Return today's OR payload for one instrument."""
    entry = instrument_registry.get(instrument_key)
    if entry is None:
        raise HTTPException(status_code=404, detail="Instrument not registered")
    return opening_range_service.get_opening_range(instrument_key)


@router.post("/recalculate")
async def recalculate_all() -> dict[str, Any]:
    """Force a full OR recalculation pass across all instruments."""
    return await asyncio.to_thread(opening_range_service.calculate_all)


@router.post("/recalculate/{instrument_key:path}")
async def recalculate_one(instrument_key: str) -> dict[str, Any]:
    """Force an OR recalculation for a single instrument."""
    entry = instrument_registry.get(instrument_key)
    if entry is None:
        raise HTTPException(status_code=404, detail="Instrument not registered")
    return await asyncio.to_thread(
        opening_range_service.calculate_one, instrument_key
    )