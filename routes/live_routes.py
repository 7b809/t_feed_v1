"""Live data REST and WebSocket routes.

    GET  /live/status                          -> feed + engine status
    GET  /live/instruments                     -> all registry instruments
    GET  /live/instruments/resolve             -> resolve by underlying / strike / type
    GET  /live/crosses/{instrument_key}        -> today's crosses for one instrument
    GET  /live/crosses                         -> today's crosses across all

    WS   /ws/live                              -> live feed snapshots (per filter)
    WS   /ws/ema-crosses                       -> live EMA crosses (per filter)

Query parameters for both WebSockets (mix and match):

    instrument_key=NSE_FO|50201
    underlying=NIFTY&strike=23500&type=PE
    all=true                                   -> receive everything

Query parameters for REST lookups:

    underlying, strike, type (for /live/instruments/resolve)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from services.instrument_registry import instrument_registry
from services.live_broadcast import live_broadcast
from services.live_cross_store import live_cross_store
from services.live_feed_service import live_feed_service

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

@router.get("/live/status")
def live_status() -> dict[str, Any]:
    return {
        "feed": live_feed_service.status(),
        "instruments": len(instrument_registry.all()),
    }


@router.get("/live/instruments")
def list_live_instruments(
    underlying: str | None = Query(default=None),
    type: str | None = Query(default=None, alias="type"),
    is_index: bool | None = Query(default=None),
) -> dict[str, Any]:
    items = instrument_registry.query(
        underlying=underlying,
        strike_type=type,
        is_index=is_index,
    )
    return {"count": len(items), "instruments": items}


@router.get("/live/instruments/resolve")
def resolve_instrument(
    underlying: str = Query(...),
    strike: str | None = Query(default=None),
    type: str | None = Query(default=None),
) -> dict[str, Any]:
    entry = instrument_registry.resolve(underlying, strike, type)
    if entry is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    return entry


@router.get("/live/crosses/{instrument_key:path}")
def crosses_for_instrument(instrument_key: str) -> dict[str, Any]:
    entry = instrument_registry.get(instrument_key)
    if entry is None:
        raise HTTPException(status_code=404, detail="Instrument not registered")
    return live_cross_store.read_today(entry)


@router.get("/live/crosses")
def crosses_all() -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    for entry in instrument_registry.all():
        payload = live_cross_store.read_today(entry)
        if payload.get("cross_count"):
            out.append(payload)
    return {"instrument_count": len(out), "payloads": out}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

def _parse_filters(ws: WebSocket):
    q = ws.query_params
    instrument_key = q.get("instrument_key")
    underlying = q.get("underlying")
    strike = q.get("strike")
    type_ = q.get("type")
    all_ = (q.get("all", "false").lower() in {"1", "true", "yes"})
    return instrument_key, underlying, strike, type_, all_


async def _pump(ws: WebSocket, sub) -> None:
    try:
        while True:
            message = await sub.queue.get()
            await ws.send_text(json.dumps(message, default=str))
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WebSocket pump crashed")
    finally:
        await live_broadcast.unsubscribe(sub.id)
        try:
            await ws.close()
        except Exception:
            pass


@router.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    await ws.accept()
    instrument_key, underlying, strike, type_, all_ = _parse_filters(ws)

    keys, triples, _ = live_broadcast.resolve_subscription_params(
        instrument_key, underlying, strike, type_
    )
    sub = await live_broadcast.subscribe(
        "feed",
        instrument_keys=keys,
        triples=triples,
        all_instruments=all_,
    )
    await ws.send_text(
        json.dumps(
            {
                "type": "subscribed",
                "topic": "feed",
                "instrument_keys": list(sub.instrument_keys),
                "triples": [list(t) for t in sub.triples],
                "all": sub.all_instruments,
            }
        )
    )
    await _pump(ws, sub)


@router.websocket("/ws/ema-crosses")
async def ws_ema_crosses(ws: WebSocket) -> None:
    await ws.accept()
    instrument_key, underlying, strike, type_, all_ = _parse_filters(ws)

    keys, triples, _ = live_broadcast.resolve_subscription_params(
        instrument_key, underlying, strike, type_
    )
    sub = await live_broadcast.subscribe(
        "crosses",
        instrument_keys=keys,
        triples=triples,
        all_instruments=all_,
    )
    await ws.send_text(
        json.dumps(
            {
                "type": "subscribed",
                "topic": "crosses",
                "instrument_keys": list(sub.instrument_keys),
                "triples": [list(t) for t in sub.triples],
                "all": sub.all_instruments,
            }
        )
    )
    await _pump(ws, sub)