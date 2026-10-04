"""
WebSocket endpoint: /ws/ema-app

Client protocol:

    → {"action": "ping"}
    ← {"type": "pong"}

    → {"action": "subscribe", "filter": {"underlying": "NIFTY"}}
    ← {"type": "subscribed", "filter": {...}}

    → {"action": "subscribe", "filter": {"underlying": "NIFTY", "strike": 23500, "option_type": "CE"}}
    ← {"type": "subscribed", "filter": {...}}

    → {"action": "subscribe", "filter": {"instrument_key": "NSE_FO|12345"}}
    ← {"type": "subscribed", "filter": {...}}

    → {"action": "subscribe_all"}
    ← {"type": "subscribed_all"}

    → {"action": "unsubscribe", "filter": {...}}
    ← {"type": "unsubscribed", "filter": {...}}

    → {"action": "unsubscribe_all"}
    ← {"type": "unsubscribed_all"}

    → {"action": "status"}
    ← {"type": "status", "filters": [...], "subscribe_all": bool, "clients": N}

Every matched cross arrives as:

    {"type": "live_ema_cross", "instrument_key": "...", "underlying": "NIFTY",
     "strike": 23500, "option_type": "CE", "cross_time": 1728541234,
     "cross_time_iso": "2026-10-04 09:20:34", "cross_type": "bullish_cross",
     "close": 123.45, "ema_fast": 122.9, "ema_slow": 122.7,
     "ema_calculation_mode": "candle_close"}
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.logger import get_logger
from ema_app.ws_manager import ema_ws_manager

logger = get_logger(__name__)
router = APIRouter(tags=["ema-app-ws"])


async def _ping_loop(ws: WebSocket, client) -> None:
    """Idle loop — keep the socket open and handle inbound actions."""
    while True:
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=25)
        except asyncio.TimeoutError:
            await ws.send_text(json.dumps({"type": "ping"}))
            continue

        try:
            msg: Dict[str, Any] = json.loads(raw)
        except Exception:
            continue

        action = str(msg.get("action") or "").lower()

        if action == "ping":
            await ws.send_text(json.dumps({"type": "pong"}))
            continue

        if action == "subscribe_all":
            client.subscribe_all = True
            await ws.send_text(json.dumps({"type": "subscribed_all"}))
            continue

        if action == "unsubscribe_all":
            client.clear_filters()
            await ws.send_text(json.dumps({"type": "unsubscribed_all"}))
            continue

        if action == "subscribe":
            filters = []
            if isinstance(msg.get("filter"), dict):
                filters.append(msg["filter"])
            if isinstance(msg.get("filters"), list):
                filters.extend([f for f in msg["filters"] if isinstance(f, dict)])

            for f in filters:
                client.add_filter(f)
                await ws.send_text(json.dumps({"type": "subscribed", "filter": f}))
            continue

        if action == "unsubscribe":
            if isinstance(msg.get("filter"), dict):
                client.remove_filter(msg["filter"])
                await ws.send_text(json.dumps({"type": "unsubscribed", "filter": msg["filter"]}))
            continue

        if action == "status":
            await ws.send_text(
                json.dumps(
                    {
                        "type": "status",
                        "filters": client.filters,
                        "subscribe_all": client.subscribe_all,
                        "clients": ema_ws_manager.client_count(),
                    }
                )
            )
            continue


@router.websocket("/ws/ema-app")
async def ws_ema_app(websocket: WebSocket) -> None:
    await websocket.accept()
    client = await ema_ws_manager.register(websocket)

    try:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "connected",
                    "clients": ema_ws_manager.client_count(),
                    "info": "Send {\"action\":\"subscribe\",\"filter\":{...}} or {\"action\":\"subscribe_all\"}",
                }
            )
        )
        await _ping_loop(websocket, client)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("/ws/ema-app error: %s", exc)
    finally:
        await ema_ws_manager.unregister(client)