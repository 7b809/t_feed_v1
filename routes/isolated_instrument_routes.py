# routes/isolated_instrument_routes.py
"""Admin routes for the level-isolation engine.

    GET  /admin/level-isolation/status         — engine status + last isolated
    GET  /admin/level-isolation/isolated       — just the isolated record
    GET  /admin/level-isolation/detail         — UI-facing detail snapshot
                                                 (includes `sr_tracking`)
    GET  /admin/level-isolation/sr-tracking    — SR tracker state only
    POST /admin/level-isolation/backfill       — re-run the intraday replay
    POST /admin/level-isolation/reset          — clear the day's state
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

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

@router.post("/backfill")
async def run_backfill() -> dict[str, Any]:
    from token_tasks.level_isolated_instrument_task import (
        run_level_isolation_backfill_manual,
    )
    return await asyncio.to_thread(run_level_isolation_backfill_manual)

@router.post("/reset")
async def reset() -> dict[str, Any]:
    return await asyncio.to_thread(level_isolated_instrument_service.reset)