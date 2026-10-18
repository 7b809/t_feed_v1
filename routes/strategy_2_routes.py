"""Strategy 2 API — Stage 3 + Stage 4.

    GET  /admin/strategy2                  -> stage_3 summary
    GET  /admin/strategy2/tracker/status   -> live tracker status
    POST /admin/strategy2/tracker/start    -> start tracker thread
    POST /admin/strategy2/tracker/stop     -> stop tracker thread
    POST /admin/strategy2/stage3/run       -> run Stage 3 now
    GET  /admin/strategy2/stage4           -> stage_4 summary
    POST /admin/strategy2/stage4/run       -> run Stage 4 now
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from core import config
from services.strategy_2_isolation_service import strategy_2_isolation_service
from services.strategy_2_live_tracker_service import strategy_2_live_tracker_service
from services.strategy_2_order_service import strategy_2_order_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/strategy2", tags=["strategy2"])

def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

@router.get("")
def stage3_summary() -> dict[str, Any]:
    path = Path(config.STRATEGY_2_BASE_DIR) / config.STRATEGY_2_STAGE_3_NAME / "stage_3_summary.json"
    data = _read(path)
    if not data:
        return {"status": "not_generated", "path": str(path)}
    return data

@router.get("/tracker/status")
def tracker_status() -> dict[str, Any]:
    return strategy_2_live_tracker_service.status()

@router.post("/tracker/start")
def tracker_start() -> dict[str, Any]:
    ok = strategy_2_live_tracker_service.start()
    return {"started": ok, "status": strategy_2_live_tracker_service.status()}

@router.post("/tracker/stop")
def tracker_stop() -> dict[str, Any]:
    strategy_2_live_tracker_service.stop()
    return {"stopped": True, "status": strategy_2_live_tracker_service.status()}

@router.post("/stage3/run")
async def run_stage3() -> dict[str, Any]:
    return await asyncio.to_thread(
        strategy_2_isolation_service.run, "manual", mode="manual"
    )

@router.get("/stage4")
def stage4_summary() -> dict[str, Any]:
    path = Path(config.STRATEGY_2_BASE_DIR) / config.STRATEGY_2_STAGE_4_NAME / "stage_4_summary.json"
    data = _read(path)
    if not data:
        return {"status": "not_generated", "path": str(path)}
    return data

@router.post("/stage4/run")
async def run_stage4() -> dict[str, Any]:
    return await asyncio.to_thread(
        strategy_2_order_service.run, "manual", mode="manual"
    )