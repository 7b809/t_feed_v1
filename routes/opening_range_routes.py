"""Opening Range (OR) level API.

    GET  /admin/opening-range
        -> list today's OR levels for all registered instruments
        -> ?only_today=false to include yesterday's stale files

    GET  /admin/opening-range/{instrument_key}
        -> today's OR levels for one instrument.
        Accepts either an exact instrument_key ("NSE_INDEX|Nifty 50")
        or a bare ticker ("NIFTY") which is mapped through
        config.STRATEGY_INDEXES.

    POST /admin/opening-range/recalculate
        -> force a full recalc pass

    POST /admin/opening-range/recalculate/{instrument_key}
        -> force a recalc for a single instrument
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from core import config
from services.instrument_registry import instrument_registry
from services.normalization import normalize_underlying
from services.opening_range_service import opening_range_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/opening-range", tags=["opening-range"])


def _resolve_index_key(instrument_key: str) -> str:
    """Resolve a bare ticker like "NIFTY" to its full instrument_key.

    If the input already contains "|" it's treated as an instrument
    key and returned as-is. Otherwise it's matched against the
    configured strategy indices by ticker or index_name (case- and
    space-insensitive).
    """
    key = str(instrument_key or "").strip()
    if not key or "|" in key:
        return key

    want = normalize_underlying(key)
    for spec in (config.STRATEGY_INDEXES or []):
        if not isinstance(spec, dict):
            continue
        ticker = normalize_underlying(spec.get("ticker"))
        name = normalize_underlying(spec.get("index_name"))
        if want and want in (ticker, name):
            return str(spec.get("instrument_key") or key)
    return key


@router.get("")
def list_opening_ranges(
    only_today: bool = Query(
        default=True,
        description=(
            "When true, skip instruments whose saved OR file is not "
            "for today."
        ),
    ),
) -> dict[str, Any]:
    """Return OR payloads for every registered instrument."""
    return opening_range_service.list_all(only_today=only_today)


@router.get("/{instrument_key:path}")
def get_opening_range(instrument_key: str) -> dict[str, Any]:
    """Return today's OR payload for one instrument."""
    resolved = _resolve_index_key(instrument_key)
    entry = instrument_registry.get(resolved)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "instrument_not_registered",
                "instrument_key": resolved,
                "hint": (
                    "Use the full key (e.g. NSE_INDEX|Nifty 50) or a "
                    "configured ticker (e.g. NIFTY)."
                ),
            },
        )
    return opening_range_service.get_opening_range(resolved)


@router.post("/recalculate")
async def recalculate_all() -> dict[str, Any]:
    """Force a full OR recalculation pass across all instruments."""
    return await asyncio.to_thread(opening_range_service.calculate_all)


@router.post("/recalculate/{instrument_key:path}")
async def recalculate_one(instrument_key: str) -> dict[str, Any]:
    """Force an OR recalculation for a single instrument."""
    resolved = _resolve_index_key(instrument_key)
    entry = instrument_registry.get(resolved)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "instrument_not_registered",
                "instrument_key": resolved,
            },
        )
    return await asyncio.to_thread(
        opening_range_service.calculate_one, resolved
    )