# app/api/ema.py
#
# REST:
#   GET  /api/v1/ema/status
#   GET  /api/v1/ema/instruments
#   GET  /api/v1/ema/channels        <-- NEW: live WS channel counts
#   POST /api/v1/ema/subscribe/{instrument_key}
#   GET  /api/v1/ema/{instrument_key}/latest
#
# WebSockets:
#   /ws/index/{underlying}                       e.g. /ws/index/nifty
#   /ws/ema/options/{underlying}/{contract}      e.g. /ws/ema/options/nifty/22500PE
#   /ws/ema/{instrument_key}                     e.g. /ws/ema/NSE_FO|51330
#
# Behaviour notes
# ---------------
# * Auto-subscribe on connect: if a client asks for an option that is not
#   currently subscribed but IS present in all_instruments.json, the server
#   finds it and subscribes it on the spot.
# * Rejection codes: FastAPI/Starlette turns `close()`-before-`accept()` into
#   an HTTP 403 handshake rejection. To send a real WebSocket close code
#   (4400 / 4404) the socket MUST be accepted first, then closed.

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect, status

from app.services.ema_feed_service import EmaFeedError, parse_options_channel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ema"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _service(request: Request):
    svc = getattr(request.app.state, "ema_feed_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="EMA service unavailable")
    return svc

def _index_key(underlying: str) -> str:
    mapping = {
        "nifty": "NSE_INDEX|Nifty 50",
        "sensex": "BSE_INDEX|SENSEX",
    }
    key = mapping.get(underlying.lower())
    if not key:
        raise HTTPException(status_code=404, detail=f"Unknown index '{underlying}'")
    return key

def _strike_matches(strike_value: Any, requested: str) -> bool:
    if strike_value is None:
        return False
    try:
        return float(strike_value) == float(requested)
    except (TypeError, ValueError):
        return str(strike_value) in {requested, f"{requested}.0"}

def _find_subscribed_option(
    svc, underlying: str, strike: str, strike_type: str
) -> str | None:
    for key in svc.subscribed_keys():
        snap = svc.snapshot(key)
        if not snap:
            continue
        meta = snap["meta"]
        if (
            (meta.get("underlying") or "").lower() == underlying.lower()
            and str(meta.get("strike_type", "")).upper() == strike_type
            and _strike_matches(meta.get("strike_price"), strike)
        ):
            return key
    return None

async def _try_auto_subscribe(
    svc, underlying: str, strike: str, strike_type: str
) -> str | None:
    try:
        catalog = svc.instruments.all()
    except Exception:  # noqa: BLE001
        logger.exception("Could not read instrument catalog for auto-subscribe")
        return None

    for key, meta in catalog.items():
        if (
            (meta.get("underlying") or "").lower() == underlying.lower()
            and str(meta.get("strike_type", "")).upper() == strike_type
            and _strike_matches(meta.get("strike_price"), strike)
        ):
            try:
                await svc.subscribe(key)
                logger.info(
                    "Auto-subscribed %s for options channel %s/%s%s",
                    key, underlying, strike, strike_type,
                )
                return key
            except Exception as exc:  # noqa: BLE001
                logger.warning("Auto-subscribe failed for %s: %s", key, exc)
                return None

    logger.info(
        "No matching option in catalog for %s/%s%s — rejecting subscription",
        underlying, strike, strike_type,
    )
    return None

async def _reject_ws(websocket: WebSocket, code: int, reason: str = "") -> None:
    try:
        await websocket.accept()
    except Exception:  # noqa: BLE001
        pass
    try:
        await websocket.close(code=code, reason=reason[:120])
    except Exception:  # noqa: BLE001
        pass

# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------
@router.get("/api/v1/ema/status")
def ema_status(request: Request) -> dict[str, Any]:
    svc = _service(request)
    keys = svc.subscribed_keys()
    return {"subscribed": keys, "count": len(keys)}

@router.get("/api/v1/ema/instruments")
def ema_instruments(request: Request) -> dict[str, Any]:
    svc = _service(request)
    return {
        "instruments": [
            {
                "instrument_key": key,
                "snapshot": svc.snapshot(key),
            }
            for key in svc.subscribed_keys()
        ]
    }

@router.get("/api/v1/ema/channels")
def ema_channels(request: Request) -> dict[str, Any]:
    """Live WebSocket channel counts — used by the dashboard header tiles."""
    svc = _service(request)
    counts = svc.broadcaster.channel_counts()
    return {"channels": counts, "total": sum(counts.values())}

@router.post("/api/v1/ema/subscribe/{instrument_key:path}")
async def ema_subscribe(request: Request, instrument_key: str) -> dict[str, Any]:
    svc = _service(request)
    try:
        return await svc.subscribe(instrument_key)
    except EmaFeedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

@router.get("/api/v1/ema/{instrument_key:path}/latest")
def ema_latest(request: Request, instrument_key: str) -> dict[str, Any]:
    svc = _service(request)
    snapshot = svc.snapshot(instrument_key)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Instrument is not subscribed")
    return snapshot

# ---------------------------------------------------------------------------
# WebSockets
# ---------------------------------------------------------------------------
async def _serve_ws(ws: WebSocket, channel: str, instrument_key: str, svc) -> None:
    await ws.accept()
    broadcaster = svc.broadcaster
    await broadcaster.register(channel, ws)

    snapshot = svc.snapshot(instrument_key)
    if snapshot:
        await ws.send_json({"event": "snapshot", **snapshot})
        if snapshot.get("last_cross"):
            cross = snapshot["last_cross"]
            close_val = cross.get("close")
            if close_val is None:
                close_val = cross.get("fast_ema")
            await ws.send_json({
                "event": "last_cross",
                "instrument_key": instrument_key,
                "trading_symbol": snapshot["meta"].get("trading_symbol"),
                "close": close_val,
                "ema9": cross.get("fast_ema"),
                "ema21": cross.get("slow_ema"),
                "last_cross_time": cross.get("timestamp"),
            })

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("WS channel %s closed unexpectedly", channel)
    finally:
        await broadcaster.unregister(channel, ws)

@router.websocket("/ws/index/{underlying}")
async def ws_index(websocket: WebSocket, underlying: str) -> None:
    svc = websocket.app.state.ema_feed_service
    try:
        key = _index_key(underlying)
    except HTTPException as exc:
        await _reject_ws(websocket, 4404, str(exc.detail))
        return
    await _serve_ws(websocket, f"index:{underlying.lower()}", key, svc)

@router.websocket("/ws/ema/options/{underlying}/{contract}")
async def ws_options(websocket: WebSocket, underlying: str, contract: str) -> None:
    svc = websocket.app.state.ema_feed_service

    try:
        strike, strike_type = parse_options_channel(underlying, contract)
    except EmaFeedError as exc:
        await _reject_ws(websocket, 4400, str(exc))
        return

    target = _find_subscribed_option(svc, underlying, strike, strike_type)
    if target is None:
        target = await _try_auto_subscribe(svc, underlying, strike, strike_type)

    if target is None:
        await _reject_ws(
            websocket,
            4404,
            f"{underlying} {strike} {strike_type} not available "
            f"(missing from all_instruments.json or subscription failed)",
        )
        return

    channel = f"options:{underlying.lower()}:{strike}{strike_type}"
    await _serve_ws(websocket, channel, target, svc)

@router.websocket("/ws/ema/{instrument_key:path}")
async def ws_instrument(websocket: WebSocket, instrument_key: str) -> None:
    svc = websocket.app.state.ema_feed_service
    if svc.snapshot(instrument_key) is None:
        try:
            await svc.subscribe(instrument_key)
        except EmaFeedError as exc:
            await _reject_ws(websocket, 4404, str(exc))
            return
    await _serve_ws(websocket, f"instrument:{instrument_key}", instrument_key, svc)