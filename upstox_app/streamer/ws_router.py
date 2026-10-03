"""
upstox_app/ws_router.py
Downstream WebSocket endpoint: /ws/market

Clients connect and receive the live feed of every instrument that is
subscribed upstream. Optional query param `keys` (comma-separated) starts
them with an explicit filter.

Client → server actions (JSON):
    {"action": "ping"}
    {"action": "status"}
    {"action": "subscribe",   "instrument_keys": [...], "mode": "full",
                              "subscribe_upstream": true}
    {"action": "unsubscribe", "instrument_keys": [...],
                              "unsubscribe_upstream": false}
    {"action": "set-filter",  "instrument_keys": [...] }   # filter only, no upstream
    {"action": "clear-filter"}                             # back to "receive all"

Server → client messages (JSON):
    {"type": "welcome",  "conn_id": "...", "filter_keys": [...] | null}
    {"type": "market",   "instrument_key": "...", "data": {...}}
    {"type": "event",    "event": "...", "payload": {...}}
    {"type": "ack",      "action": "...", ...}
    {"type": "pong"}
    {"type": "status",   ...}
    {"type": "error",    "message": "..."}
"""
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.logger import get_logger
from upstox_app.common.config import upstox_config
from upstox_app.market.market_streamer import market_streamer
from upstox_app.streamer.ws_manager import WSClient, fanout_event, ws_manager

logger = get_logger(__name__)

router = APIRouter(tags=["upstox-ws"])

# ── helpers ──────────────────────────────────────────────────────
def _clean_keys(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    return [k.strip() for k in raw if isinstance(k, str) and k.strip()]

def _parse_query_keys(raw: Optional[str]) -> Optional[Set[str]]:
    if not raw:
        return None
    keys = {k.strip() for k in raw.split(",") if k.strip()}
    return keys or None

# ── websocket endpoint ───────────────────────────────────────────
@router.websocket("/ws/market")
async def ws_market(websocket: WebSocket, keys: Optional[str] = None) -> None:
    """Live market feed. Optional `?keys=A,B` initial filter."""
    await websocket.accept()

    initial_filter = _parse_query_keys(keys)
    conn = await ws_manager.connect(websocket, initial_filter)

    try:
        await conn.send_json({
            "type": "welcome",
            "conn_id": conn.id,
            "filter_keys": sorted(initial_filter) if initial_filter else None,
            "clients": ws_manager.client_count(),
            "upstream": market_streamer.status(),
        })

        while True:
            try:
                payload = await websocket.receive_json()
            except Exception:  # noqa: BLE001
                # Non-JSON payload → tell the client and continue
                await conn.send_json({"type": "error", "message": "expected JSON payload"})
                continue

            if not isinstance(payload, dict):
                await conn.send_json({"type": "error", "message": "payload must be an object"})
                continue

            await _handle_action(conn, payload)

    except WebSocketDisconnect:
        logger.info("WS client disconnected | id=%s", conn.id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("WS connection error | id=%s | err=%s", conn.id, exc)
    finally:
        await ws_manager.disconnect(conn.id)

# ── action handler ───────────────────────────────────────────────
async def _handle_action(conn: WSClient, payload: Dict[str, Any]) -> None:
    action = str(payload.get("action") or "").strip().lower()

    # ── ping ────────────────────────────────────────────────────
    if action == "ping":
        await conn.send_json({"type": "pong"})
        return

    # ── status ──────────────────────────────────────────────────
    if action == "status":
        await conn.send_json({
            "type": "status",
            "conn_id": conn.id,
            "filter_keys": sorted(conn.filter_keys) if conn.filter_keys else None,
            "upstream": market_streamer.status(),
            "clients": ws_manager.client_count(),
        })
        return

    # ── subscribe ───────────────────────────────────────────────
    if action == "subscribe":
        keys = _clean_keys(payload.get("instrument_keys"))
        if not keys:
            await conn.send_json({"type": "error", "message": "instrument_keys required"})
            return

        mode = str(payload.get("mode") or upstox_config.DEFAULT_SUBSCRIPTION_MODE).strip().lower()
        subscribe_upstream = bool(payload.get("subscribe_upstream", True))

        # 1) add to this client's filter
        if conn.filter_keys is None:
            conn.filter_keys = set(keys)     # switch from "ALL" to explicit set
        else:
            conn.filter_keys.update(keys)

        ack: Dict[str, Any] = {
            "type": "ack",
            "action": "subscribe",
            "requested": keys,
            "filter_keys": sorted(conn.filter_keys),
        }

        # 2) optionally also push upstream
        if subscribe_upstream:
            try:
                applied, skipped = market_streamer.subscribe(keys, mode)
                ack["applied"] = applied
                ack["skipped"] = skipped
                if applied:
                    fanout_event("upstream_subscribe", {"keys": applied, "mode": mode, "by": "ws"})
            except ValueError as exc:
                await conn.send_json({"type": "error", "message": str(exc)})
                return

        await conn.send_json(ack)
        return

    # ── unsubscribe ─────────────────────────────────────────────
    if action == "unsubscribe":
        keys = _clean_keys(payload.get("instrument_keys"))
        if not keys:
            await conn.send_json({"type": "error", "message": "instrument_keys required"})
            return

        unsubscribe_upstream = bool(payload.get("unsubscribe_upstream", False))

        removed: List[str] = []
        if conn.filter_keys is not None:
            for k in keys:
                if k in conn.filter_keys:
                    conn.filter_keys.discard(k)
                    removed.append(k)
            if not conn.filter_keys:
                conn.filter_keys = None      # empty set → go back to "ALL"

        ack = {
            "type": "ack",
            "action": "unsubscribe",
            "requested": keys,
            "removed_from_filter": removed,
            "filter_keys": sorted(conn.filter_keys) if conn.filter_keys else None,
        }

        if unsubscribe_upstream:
            try:
                applied, skipped = market_streamer.unsubscribe(keys)
                ack["applied"] = applied
                ack["skipped"] = skipped
                if applied:
                    fanout_event("upstream_unsubscribe", {"keys": applied, "by": "ws"})
            except ValueError as exc:
                await conn.send_json({"type": "error", "message": str(exc)})
                return

        await conn.send_json(ack)
        return

    # ── set-filter (no upstream side effect) ────────────────────
    if action == "set-filter":
        keys = _clean_keys(payload.get("instrument_keys"))
        conn.filter_keys = set(keys) if keys else None
        await conn.send_json({
            "type": "ack",
            "action": "set-filter",
            "filter_keys": sorted(conn.filter_keys) if conn.filter_keys else None,
        })
        return

    # ── clear-filter ────────────────────────────────────────────
    if action == "clear-filter":
        conn.filter_keys = None
        await conn.send_json({"type": "ack", "action": "clear-filter", "filter_keys": None})
        return

    # ── unknown ─────────────────────────────────────────────────
    await conn.send_json({"type": "error", "message": f"unknown action: {action or '<empty>'}"})