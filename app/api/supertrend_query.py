# app/api/supertrend_query.py
"""Read-only Supertrend state and connection status endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from urllib.parse import unquote

router = APIRouter()


@router.get("/supertrend/{instrument_key:path}/latest")
async def get_supertrend_latest(instrument_key: str, request: Request) -> dict:
    key = unquote(instrument_key)
    store = request.app.state.supertrend_state_store
    doc = store.get_latest(key)
    if not doc:
        raise HTTPException(status_code=404, detail=f"no Supertrend state for {key}")
    return doc


@router.get("/supertrend/{instrument_key:path}/status")
async def get_supertrend_status(instrument_key: str, request: Request) -> dict:
    key = unquote(instrument_key)
    client = request.app.state.subscriber.get_supertrend_client(key)
    if client is None:
        return {
            "instrument_key": key, "indicator": "supertrend",
            "ws_state": "idle", "last_frame_at": None,
            "last_frame_event": None, "reconnect_attempts": 0,
            "last_error": None,
        }
    return client.status()


@router.get("/supertrend/status")
async def get_all_supertrend_status(request: Request) -> dict:
    config = request.app.state.config
    clients = request.app.state.subscriber.list_supertrend_clients()
    return {
        "enabled": config.supertrend_enabled,
        "active_connections": len(clients),
        "connections": [client.status() for client in clients],
    }
