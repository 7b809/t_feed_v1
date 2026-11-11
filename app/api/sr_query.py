# app/api/sr_query.py
from __future__ import annotations

import asyncio
import logging
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request

logger = logging.getLogger(__name__)

router = APIRouter()

_VALID_CONFIGS = {"15/3", "15/15", "15/30", "all"}


@router.get("/sr/{instrument_key}/latest")
async def get_sr_latest(
    instrument_key: str,
    request: Request,
    config: str = Query("15/3"),
) -> dict:
    if config not in _VALID_CONFIGS:
        raise HTTPException(
            status_code=400,
            detail=f"config must be one of {sorted(_VALID_CONFIGS)}",
        )

    key = unquote(instrument_key)
    store = request.app.state.sr_state_store

    if config == "all":
        docs = await asyncio.to_thread(store.get_all_configs, key)
        if not docs:
            raise HTTPException(
                status_code=404,
                detail=f"no sr_state for {key}",
            )
        return {"instrument_key": key, "configs": docs}

    doc = await asyncio.to_thread(store.get_latest, key, config)
    if not doc:
        raise HTTPException(
            status_code=404,
            detail=f"no sr_state for {key} config={config}",
        )
    return doc


@router.get("/sr/{instrument_key}/status")
async def get_sr_status(instrument_key: str, request: Request) -> dict:
    key = unquote(instrument_key)
    subscriber = request.app.state.subscriber
    client = subscriber.get_client(key)

    if client is None:
        return {
            "instrument_key": key,
            "isolation_id": None,
            "ws_state": "idle",
            "last_frame_at": None,
            "last_frame_event": None,
            "configs": [],
            "reconnect_attempts": 0,
            "last_error": None,
        }

    status = client.status()
    return {
        "instrument_key": status["instrument_key"],
        "isolation_id": status["isolation_id"],
        "ws_state": status["ws_state"],
        "last_frame_at": status["last_frame_at"],
        "last_frame_event": status["last_frame_event"],
        "configs": status["configs"],
        "reconnect_attempts": status["reconnect_attempts"],
        "last_error": status["last_error"],
    }


@router.get("/status")
async def overall_status(request: Request) -> dict:
    subscriber = request.app.state.subscriber
    clients = subscriber.list_clients()
    connections = []
    for c in clients:
        st = c.status()
        connections.append(
            {
                "instrument_key": st["instrument_key"],
                "leg": st["leg_role"],
                "ws_state": st["ws_state"],
                "last_frame_at": st["last_frame_at"],
            }
        )
    return {
        "running": True,
        "isolation_count": len(subscriber.list_isolation_ids()),
        "active_legs": len(clients),
        "connections": connections,
    }