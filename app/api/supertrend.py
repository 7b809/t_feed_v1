"""Today-only Supertrend REST and WebSocket endpoints."""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from app.services.supertrend_feed_service import SupertrendFeedError

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
router = APIRouter(tags=["supertrend"])

def _service(request: Request):
    service = getattr(request.app.state, "supertrend_feed_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Supertrend service unavailable")
    return service

async def _reject_ws(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.accept()
        await websocket.close(code=code, reason=reason[:120])
    except Exception:
        pass

@router.get("/api/v1/supertrend/status")
def supertrend_status(request: Request) -> dict[str, Any]:
    return _service(request).status()

@router.get("/api/v1/supertrend/{instrument_key:path}/latest")
def supertrend_latest(request: Request, instrument_key: str) -> dict[str, Any]:
    snapshot = _service(request).snapshot(instrument_key)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="No Supertrend snapshot for instrument")
    return snapshot

@router.post("/api/v1/supertrend/subscribe/{instrument_key:path}")
async def supertrend_subscribe(request: Request, instrument_key: str) -> dict[str, Any]:
    try:
        return await _service(request).subscribe(instrument_key)
    except SupertrendFeedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@router.post("/api/v1/supertrend/unsubscribe/{instrument_key:path}")
async def supertrend_unsubscribe(request: Request, instrument_key: str) -> dict[str, Any]:
    service = _service(request)
    return {"instrument_key": service.canonical_key(instrument_key),
            "removed": await service.unsubscribe(instrument_key)}

async def _serve_ws(websocket: WebSocket, instrument_key: str, service) -> None:
    try:
        result = await service.subscribe(instrument_key)
    except SupertrendFeedError as exc:
        await _reject_ws(websocket, 4404, str(exc))
        return
    key = result["instrument_key"]
    channel = f"supertrend:{key}"
    await websocket.accept()
    await service.broadcaster.register(channel, websocket)
    today = datetime.now(IST).date().isoformat()
    await websocket.send_json({"event": "connection_ack", "status": "connected",
        "message": "Supertrend feed connected successfully", "instrument_key": key,
        "indicator": "supertrend", "data_scope": "today_only", "today_ist_date": today})
    snapshot = service.snapshot(key)
    if snapshot is not None:
        await websocket.send_json({"event": "snapshot", **snapshot})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Supertrend WS closed unexpectedly for %s", key)
    finally:
        await service.broadcaster.unregister(channel, websocket)

@router.websocket("/ws/supertrend/index/{underlying}")
async def ws_supertrend_index(websocket: WebSocket, underlying: str) -> None:
    await _serve_ws(websocket, underlying, websocket.app.state.supertrend_feed_service)

@router.websocket("/ws/supertrend/{instrument_key:path}")
async def ws_supertrend_instrument(websocket: WebSocket, instrument_key: str) -> None:
    await _serve_ws(websocket, instrument_key, websocket.app.state.supertrend_feed_service)
