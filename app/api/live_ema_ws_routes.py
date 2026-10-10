"""Our own WebSocket endpoints for live EMA updates, plus a discovery API.

Clients connect to one of these endpoints and receive, for the instrument
they chose:

* ``snapshot``         — sent right after connect.
* ``tick``             — every upstream LTP tick; includes the current
                         forming candle.
* ``candle_completed`` — a bucket closed. Carries the final OHLC and the
                         recomputed fast/slow EMA values.
* ``ema_cross``        — only when a *completed* candle produces a new
                         bullish/bearish cross. Never fires on a partial
                         candle.
* ``error``            — human-readable error (e.g. unknown instrument).

Endpoints
---------
WS /ws/live-ema/by-instrument/{instrument_key}
    ``instrument_key`` is URL-encoded (contains ``|``), e.g.
    ``/ws/live-ema/by-instrument/NSE_INDEX%7CNifty%2050``.

WS /ws/live-ema/by-parts
    Query params: ``underlying`` (required), ``strike`` and ``type``
    (optional). ``underlying`` accepts names like ``nifty``, ``NIFTY 50``,
    ``sensex``, ``banknifty``.

GET /api/live-ema/ws-endpoints
    Lists every available WebSocket endpoint with a description, a URL
    template, and concrete examples — so a UI can discover how to connect.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.config import settings
from app.core.logger import get_logger
from app.services.instrument_paths import (
    build_dir_from_instrument_key,
    normalize_strike,
    normalize_underlying_name,
    resolve_underlying,
)
from app.services.live_ema_ws_hub import live_ema_ws_hub
from app.services.subscription_store import subscription_store

logger = get_logger(__name__)

router = APIRouter(tags=["live-ema-ws"])

# ---------------------------------------------------------------------------
# REST: list available WebSocket endpoints
# ---------------------------------------------------------------------------
@router.get("/api/live-ema/ws-endpoints")
async def list_ws_endpoints() -> dict[str, Any]:
    """Return the available WebSocket endpoints and how to connect."""
    return {
        "use_live_ltp_feed": settings.use_live_ltp_feed,
        "live_ltp_enabled": settings.use_live_ltp_feed,
        "interval_seconds": settings.live_ema_interval_seconds,
        "endpoints": [
            {
                "name": "by-instrument",
                "description": (
                    "Subscribe using an exact instrument_key. The key is "
                    "URL-encoded because it contains a '|'."
                ),
                "url_template": "/ws/live-ema/by-instrument/{instrument_key}",
                "examples": [
                    "/ws/live-ema/by-instrument/NSE_INDEX%7CNifty%2050",
                    "/ws/live-ema/by-instrument/NSE_FO%7C44694",
                    "/ws/live-ema/by-instrument/BSE_INDEX%7CSENSEX",
                ],
            },
            {
                "name": "by-parts",
                "description": (
                    "Subscribe using underlying + optional strike + optional "
                    "type. 'underlying' accepts names like nifty, NIFTY 50, "
                    "sensex, banknifty. Leave strike/type off for index rows."
                ),
                "url_template": (
                    "/ws/live-ema/by-parts?underlying={underlying}"
                    "&strike={strike}&type={type}"
                ),
                "examples": [
                    "/ws/live-ema/by-parts?underlying=nifty",
                    "/ws/live-ema/by-parts?underlying=nifty&strike=25000&type=CE",
                    "/ws/live-ema/by-parts?underlying=sensex&strike=70100&type=PE",
                ],
            },
        ],
        "message_types": {
            "snapshot": (
                "Sent immediately after connect with the latest known state "
                "for the instrument."
            ),
            "tick": "Every upstream LTP tick; includes the forming candle.",
            "candle_completed": (
                "A bucket closed. Includes the completed OHLC and the "
                "recomputed EMA fast/slow values."
            ),
            "ema_cross": (
                "A new cross detected on a *completed* candle. Never emitted "
                "for a partial candle."
            ),
            "error": "Human-readable error (e.g. unknown instrument).",
        },
    }

# ---------------------------------------------------------------------------
# WebSocket: by instrument_key
# ---------------------------------------------------------------------------
@router.websocket("/ws/live-ema/by-instrument/{instrument_key}")
async def ws_by_instrument(websocket: WebSocket, instrument_key: str) -> None:
    info = _lookup_by_instrument_key(instrument_key)
    if info is None:
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "error",
                "error": f"instrument_key not found: {instrument_key}",
            }
        )
        await websocket.close(code=1008)
        return

    await _serve_ws(websocket, info)

# ---------------------------------------------------------------------------
# WebSocket: by underlying + strike + type
# ---------------------------------------------------------------------------
@router.websocket("/ws/live-ema/by-parts")
async def ws_by_parts(
    websocket: WebSocket,
    underlying: str = Query(...),
    strike: str | None = Query(default=None),
    type: str | None = Query(default=None),
) -> None:
    info = _lookup_by_parts(underlying, strike, type)
    if info is None:
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "error",
                "error": (
                    f"No instrument matched underlying={underlying} "
                    f"strike={strike} type={type}"
                ),
            }
        )
        await websocket.close(code=1008)
        return

    await _serve_ws(websocket, info)

# ---------------------------------------------------------------------------
# Shared connection handling
# ---------------------------------------------------------------------------
async def _serve_ws(websocket: WebSocket, info: dict[str, Any]) -> None:
    instrument_key = str(info["instrument_key"])
    await websocket.accept()
    await live_ema_ws_hub.subscribe(instrument_key, websocket)

    await websocket.send_json(
        {
            "type": "snapshot",
            "server_time": datetime.now(timezone.utc).isoformat(),
            "instrument_key": instrument_key,
            "instrument": info,
            "server": {
                "use_live_ltp_feed": settings.use_live_ltp_feed,
                "interval_seconds": settings.live_ema_interval_seconds,
            },
        }
    )

    try:
        # Idle loop; all outbound traffic flows through the hub.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception(
            "Live EMA WS: connection error; instrument=%s", instrument_key
        )
    finally:
        await live_ema_ws_hub.unsubscribe(instrument_key, websocket)

# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------
def _lookup_by_instrument_key(instrument_key: str) -> dict[str, Any] | None:
    if not instrument_key or not instrument_key.strip():
        return None

    instruments = subscription_store.snapshot().get("active", [])
    for item in instruments:
        if isinstance(item, dict) and item.get("instrument_key") == instrument_key:
            return item

    # Index row not currently in the subscription snapshot.
    if build_dir_from_instrument_key(instrument_key) is not None:
        return {"instrument_key": instrument_key}

    return None

def _lookup_by_parts(
    underlying: str, strike: str | None, type_: str | None
) -> dict[str, Any] | None:
    target_underlying = normalize_underlying_name(underlying)
    target_strike = normalize_strike(strike) if strike else None
    target_type = type_.strip().upper() if type_ else None

    instruments = subscription_store.snapshot().get("active", [])
    for item in instruments:
        if not isinstance(item, dict):
            continue
        if resolve_underlying(item) != target_underlying:
            continue

        item_strike_raw = item.get("strike_price")
        item_type = str(item.get("instrument_type") or "").strip().upper() or None

        if target_strike is not None:
            if item_strike_raw is None:
                continue
            if normalize_strike(item_strike_raw) != target_strike:
                continue
        else:
            # No strike given -> require a "no strike" row.
            if item_strike_raw not in (None, "", 0, "0", "0.0", 0.0):
                continue

        if target_type is not None and item_type != target_type:
            continue

        return item

    return None