"""Today-only Supertrend REST and interval-aware WebSocket endpoints."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

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
def supertrend_latest(
    request: Request, instrument_key: str,
    interval: str = Query(default="1m", description="Candle interval, e.g. 1m, 5m, 15m"),
) -> dict[str, Any]:
    try:
        snapshot = _service(request).snapshot(instrument_key, interval)
    except SupertrendFeedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if snapshot is None:
        raise HTTPException(status_code=404, detail="No Supertrend snapshot for instrument/interval")
    return snapshot


@router.post("/api/v1/supertrend/subscribe/{instrument_key:path}")
async def supertrend_subscribe(
    request: Request, instrument_key: str, interval: str = Query(default="1m")
) -> dict[str, Any]:
    try:
        return await _service(request).subscribe(instrument_key, interval)
    except SupertrendFeedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/v1/supertrend/unsubscribe/{instrument_key:path}")
async def supertrend_unsubscribe(
    request: Request, instrument_key: str,
    interval: str | None = Query(default=None, description="Omit to unsubscribe all intervals"),
) -> dict[str, Any]:
    service = _service(request)
    try:
        removed = await service.unsubscribe(instrument_key, interval)
    except SupertrendFeedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"instrument_key": service.canonical_key(instrument_key),
            "interval": interval, "removed": removed}


async def _serve_ws(
    websocket: WebSocket, instrument_key: str, service, interval: str = "1m"
) -> None:
    try:
        result = await service.subscribe(instrument_key, interval)
    except SupertrendFeedError as exc:
        await _reject_ws(websocket, 4404, str(exc))
        return
    key, interval = result["instrument_key"], result["interval"]
    channel = service._channel(key, interval)
    await websocket.accept()
    await service.broadcaster.register(channel, websocket)
    today = datetime.now(IST).date().isoformat()
    await websocket.send_json({
        "event": "connection_ack", "status": "connected",
        "message": "Supertrend feed connected successfully",
        "instrument_key": key, "indicator": "supertrend", "interval": interval,
        "data_scope": "today_only", "today_ist_date": today,
    })
    snapshot = service.snapshot(key, interval)
    if snapshot is not None:
        await websocket.send_json({"event": "snapshot", **snapshot})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Supertrend WS closed unexpectedly for %s interval=%s", key, interval)
    finally:
        await service.broadcaster.unregister(channel, websocket)


# Register interval routes before the legacy path catch-all.
@router.websocket("/ws/supertrend/interval/{interval}/index/{underlying}")
async def ws_supertrend_interval_index(
    websocket: WebSocket, interval: str, underlying: str
) -> None:
    await _serve_ws(websocket, underlying, websocket.app.state.supertrend_feed_service, interval)


@router.websocket("/ws/supertrend/interval/{interval}/{instrument_key:path}")
async def ws_supertrend_interval_instrument(
    websocket: WebSocket, interval: str, instrument_key: str
) -> None:
    await _serve_ws(websocket, instrument_key, websocket.app.state.supertrend_feed_service, interval)


# Legacy routes remain the 1-minute stream and retain their original channel.
@router.websocket("/ws/supertrend/index/{underlying}")
async def ws_supertrend_index(websocket: WebSocket, underlying: str) -> None:
    await _serve_ws(websocket, underlying, websocket.app.state.supertrend_feed_service, "1m")


@router.websocket("/ws/supertrend/{instrument_key:path}")
async def ws_supertrend_instrument(websocket: WebSocket, instrument_key: str) -> None:
    await _serve_ws(websocket, instrument_key, websocket.app.state.supertrend_feed_service, "1m")
