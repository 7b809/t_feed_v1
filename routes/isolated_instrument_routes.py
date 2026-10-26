# routes/isolated_instrument_routes.py
"""Admin routes for the level-isolation engine.

    GET  /admin/level-isolation/status                    — engine status + last isolated
    GET  /admin/level-isolation/isolated                  — just the isolated record
    GET  /admin/level-isolation/detail                    — UI-facing detail snapshot
                                                            (includes `sr_tracking`)
    GET  /admin/level-isolation/sr-tracking               — SR tracker state only
    GET  /admin/level-isolation/search-instruments        — search the registry
    GET  /admin/level-isolation/instrument-sr/{key}       — SR levels for a searched
                                                            instrument, tagged against
                                                            the official isolation time
    POST /admin/level-isolation/backfill                  — re-run the intraday replay
    POST /admin/level-isolation/reset                     — clear the day's state
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query

from services.isolated_sr_tracker_service import (
    isolated_sr_tracker_service,
)
from services.level_isolated_instrument_service import (
    level_isolated_instrument_service,
)

router = APIRouter(prefix="/admin/level-isolation", tags=["level-isolation"])

@router.get("/status")
def get_status() -> dict[str, Any]:
    payload = level_isolated_instrument_service.last_status()
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    return payload

@router.get("/isolated")
def get_isolated() -> dict[str, Any]:
    payload = level_isolated_instrument_service.last_status()
    return {
        "day": payload.get("day"),
        "isolated": payload.get("isolated"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

@router.get("/detail")
def get_detail() -> dict[str, Any]:
    """UI-facing detail snapshot — consumed by ``/isolated``.

    Returns a JSON-safe dict containing:
      * ``market``         — trading-day / session state + a human copy
      * ``isolated``       — the persisted isolation record (or None)
      * ``trigger_event``  — the event that produced the isolation
      * ``candidates``     — copy of ``trigger_event.candidates``
      * ``events``         — the day's touch log
      * ``touch_state``    — per (index, side) one-shot flags
      * ``or_levels``      — the reference OR levels currently on disk
      * ``sr_tracking``    — pre/post isolation SR level tracker state
      * ``live_sr_levels`` — the official isolated instrument's own SR file
    """
    return level_isolated_instrument_service.get_detail()

@router.get("/sr-tracking")
def get_sr_tracking() -> dict[str, Any]:
    """Return ONLY the isolated-instrument SR tracker snapshot.

    Contains:
      * ``instrument_key`` / ``trading_symbol`` — the tracked instrument
      * ``pre_isolation_levels``  — SR levels present at isolation moment
      * ``post_isolation_levels`` — SR levels confirmed after isolation
      * ``waiting_for_first_confirmation`` — True until the first
        post-isolation pivot lands

    Levels are returned sorted by price ascending. Every confirmed
    pivot is retained for the day — there is no broken / intact
    distinction.
    """
    return isolated_sr_tracker_service.get_detail()

# --------------------------------------------------------------------------- #
# Instrument search + comparison (NEW)                                        #
# --------------------------------------------------------------------------- #
@router.get("/search-instruments")
def search_instruments(
    query: str = Query(..., min_length=1, max_length=50),
    limit: int = Query(default=15, ge=1, le=50),
) -> dict[str, Any]:
    """Search the live instrument registry for matching contracts.

    Matches (case-insensitive substring) on:
      * ``trading_symbol`` (weight 10, +5 if the symbol starts with ``q``)
      * ``underlying``     (weight 3)
      * ``strike_price``   (weight 4, prefix-match on string form)
      * ``strike_type``    (weight 2, exact match on CE / PE / FUT)

    Returns up to ``limit`` scored results, sorted by relevance (highest
    score first) then alphabetically by trading_symbol. Empty ``query``
    returns an empty result set rather than erroring — the UI debounces
    and only calls this endpoint for non-empty queries.
    """
    q = str(query or "").strip()
    if not q:
        return {
            "query": query,
            "count": 0,
            "total_matches": 0,
            "results": [],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    q_lower = q.lower()
    q_upper = q.upper()

    from services.instrument_registry import instrument_registry

    scored: list[dict[str, Any]] = []
    for e in instrument_registry.all():
        sym = str(e.get("trading_symbol") or "")
        sym_lower = sym.lower()
        und = str(e.get("underlying") or "").lower()
        stk = str(e.get("strike_price") or "")
        typ = str(e.get("strike_type") or "").upper()

        score = 0
        if q_lower and q_lower in sym_lower:
            score += 10
            if sym_lower.startswith(q_lower):
                score += 5
        if q_lower and q_lower in und:
            score += 3
        if stk and (q_lower == stk or stk.startswith(q_lower)):
            score += 4
        if typ and q_upper == typ:
            score += 2

        if score > 0:
            scored.append({
                "_score": score,
                "instrument_key": e.get("instrument_key"),
                "trading_symbol": e.get("trading_symbol"),
                "underlying": e.get("underlying"),
                "strike": e.get("strike_price"),
                "type": e.get("strike_type"),
                "expiry": e.get("expiry"),
                "is_index": bool(e.get("is_index")),
            })

    scored.sort(key=lambda x: (
        -int(x.get("_score") or 0),
        str(x.get("trading_symbol") or ""),
    ))

    trimmed = scored[:limit]
    for r in trimmed:
        r.pop("_score", None)

    return {
        "query": query,
        "count": len(trimmed),
        "total_matches": len(scored),
        "results": trimmed,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

@router.get("/instrument-sr/{instrument_key:path}")
def get_instrument_sr(instrument_key: str) -> dict[str, Any]:
    """Return the SR levels of a **searched** instrument.

    Levels are tagged with ``_after_isolation`` relative to the
    **official** isolated record's ``touched_at`` so the comparison is
    apples-to-apples. The response also carries:

      * ``is_official_isolated`` — True if the searched key matches the
        officially isolated instrument (that happens when the user picks
        the same key from the search dropdown)
      * ``official_instrument_key`` / ``official_trading_symbol`` /
        ``official_touched_at`` — the reference record

    Never touches the persisted isolation state.
    """
    return level_isolated_instrument_service.get_instrument_sr_comparison(
        instrument_key
    )

@router.post("/backfill")
async def run_backfill() -> dict[str, Any]:
    from token_tasks.level_isolated_instrument_task import (
        run_level_isolation_backfill_manual,
    )
    return await asyncio.to_thread(run_level_isolation_backfill_manual)

@router.post("/reset")
async def reset() -> dict[str, Any]:
    return await asyncio.to_thread(level_isolated_instrument_service.reset)