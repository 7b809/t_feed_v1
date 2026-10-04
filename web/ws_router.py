"""WebSocket routes the templates connect to.

* /all-feeds        - flat market tick fanout used by index.html and chart.html
* /ws/ema-crossover - EMA crossover events for isolated_ema_dashboard.html
* /ws/opening-range - Opening Range touch events for isolated_ema_dashboard.html
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["UI-WS"])


# ---------------------------------------------------------------------------
# /all-feeds - fanout of market streamer messages
# ---------------------------------------------------------------------------
class _AllFeedsHub:
    """Tiny in-process pub/sub so /all-feeds can mirror the market streamer."""

    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def register(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def unregister(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, message: Dict[str, Any]) -> None:
        if not self._clients:
            return
        text = json.dumps(message, default=str)
        dead: List[WebSocket] = []
        for ws in list(self._clients):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.unregister(ws)


all_feeds_hub = _AllFeedsHub()


async def publish_market_tick(tick: Dict[str, Any]) -> None:
    """Public entrypoint - call this from the market streamer callback."""
    await all_feeds_hub.broadcast(tick)


@router.websocket("/all-feeds")
async def ws_all_feeds(websocket: WebSocket) -> None:
    await websocket.accept()
    await all_feeds_hub.register(websocket)
    try:
        await websocket.send_text(json.dumps({"type": "connected"}))

        # Best-effort: subscribe all known instrument keys upstream.
        try:
            from web import service

            keys = [c.get("instrument_key") for c in service.load_all_option_instruments()]
            keys = [k for k in keys if k]
            if keys:
                await websocket.send_text(
                    json.dumps({"type": "subscribed", "instrument_keys": keys})
                )
        except Exception:
            pass

        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=25)
                try:
                    payload = json.loads(raw)
                except Exception:
                    continue
                if payload.get("action") == "ping":
                    await websocket.send_text(json.dumps({"type": "ping"}))
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("/all-feeds websocket error: %s", exc)
    finally:
        await all_feeds_hub.unregister(websocket)


# ---------------------------------------------------------------------------
# /ws/ema-crossover
# ---------------------------------------------------------------------------
class _BroadcastHub:
    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def register(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def unregister(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, message: Dict[str, Any]) -> None:
        if not self._clients:
            return
        text = json.dumps(message, default=str)
        dead: List[WebSocket] = []
        for ws in list(self._clients):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.unregister(ws)


ema_crossover_hub = _BroadcastHub()
opening_range_hub = _BroadcastHub()


async def publish_ema_cross(payload: Dict[str, Any]) -> None:
    payload = {"type": "live_ema_cross", **payload}
    await ema_crossover_hub.broadcast(payload)


async def publish_opening_range_touch(payload: Dict[str, Any]) -> None:
    payload = {"type": "opening_range_touch", **payload}
    await opening_range_hub.broadcast(payload)


async def _ws_ping_loop(websocket: WebSocket) -> None:
    while True:
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=25)
        except asyncio.TimeoutError:
            await websocket.send_text(json.dumps({"type": "ping"}))
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        if msg.get("action") == "ping":
            await websocket.send_text(json.dumps({"type": "ping"}))


@router.websocket("/ws/ema-crossover")
async def ws_ema_crossover(websocket: WebSocket) -> None:
    await websocket.accept()
    await ema_crossover_hub.register(websocket)
    try:
        await websocket.send_text(json.dumps({"type": "connected"}))
        await _ws_ping_loop(websocket)
    except WebSocketDisconnect:
        pass
    finally:
        await ema_crossover_hub.unregister(websocket)


@router.websocket("/ws/opening-range")
async def ws_opening_range(websocket: WebSocket) -> None:
    await websocket.accept()
    await opening_range_hub.register(websocket)
    try:
        await websocket.send_text(json.dumps({"type": "connected"}))
        await _ws_ping_loop(websocket)
    except WebSocketDisconnect:
        pass
    finally:
        await opening_range_hub.unregister(websocket)