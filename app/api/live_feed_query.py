# app/api/live_feed_query.py
"""Read-only local live-market feed status and persisted latest payloads."""
from __future__ import annotations

from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/live-feed/status")
async def live_feed_status(request: Request) -> dict:
    client = request.app.state.subscriber.get_live_feed_client()
    return {
        "connection": client.status(),
        "storage": request.app.state.live_feed_state_store.status(),
    }


@router.get("/live-feed/{instrument_key:path}/latest")
async def live_feed_latest(instrument_key: str, request: Request) -> dict:
    key = unquote(instrument_key)
    record = request.app.state.live_feed_state_store.latest(key)
    if not record:
        raise HTTPException(status_code=404, detail=f"no live-feed record for {key}")
    return record
