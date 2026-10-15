"""Live data REST and WebSocket routes.

Every endpoint that reads a *single* instrument now accepts either an
explicit ``instrument_key`` **or** the three-part selector
``underlying`` + ``strike`` + ``type``. The latter is resolved through
``instrument_registry.resolve(...)``.

    GET  /live/status
    GET  /live/instruments
    GET  /live/instruments/resolve

    # Single-instrument reads (key OR underlying+strike+type)
    GET  /live/crosses                     ?instrument_key=... | underlying+strike+type
    GET  /live/crosses/{instrument_key}    (path form, kept for compat)
    GET  /live/ema                         ?instrument_key=... | underlying+strike+type
    GET  /live/ema/{instrument_key}
    GET  /live/ema-series                  ?instrument_key=... | underlying+strike+type
    GET  /live/candles                     ?instrument_key=... | underlying+strike+type
    GET  /live/snapshot                    feed + EMA in one call
    GET  /live/snapshot/{instrument_key}

    # All-instrument reads
    GET  /live/crosses                     (no params -> all non-empty)

    # WebSockets
    WS   /ws/live
    WS   /ws/chart
    WS   /all-feeds  (and /ws/all-feeds)
    WS   /ws/ema-crosses
    WS   /ws/ema                           (streaming EMA snapshot per instrument)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from core import config
from services.instrument_registry import instrument_registry
from services.live_broadcast import live_broadcast
from services.live_cross_store import live_cross_store
from services.live_ema_engine import live_ema_engine
from services.live_feed_service import live_feed_service

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Instrument resolution helper
# ---------------------------------------------------------------------------

def _resolve_entry(
    *,
    instrument_key: str | None = None,
    underlying: str | None = None,
    strike: str | int | float | None = None,
    strike_type: str | None = None,
    require: bool = True,
) -> dict[str, Any] | None:
    """Resolve a single registry entry from either selector form.

    Priority:
        1. Explicit ``instrument_key`` — looked up directly, then
           falls back to ad-hoc registration if unknown (so chart-style
           direct access keeps working).
        2. ``underlying`` + ``strike`` + ``strike_type`` triple lookup.

    When ``require`` is True, raises HTTP 404 if nothing matches.
    """
    if instrument_key:
        key = str(instrument_key).strip()
        if key:
            entry = instrument_registry.get(key)
            if entry is None:
                # Chart URLs and browser-driven flows pass raw keys
                # that aren't in the backup tree. Register ad-hoc so
                # the rest of the pipeline can operate on them.
                entry = instrument_registry.register_adhoc(key)
            return entry

    if underlying and strike is not None and strike_type:
        entry = instrument_registry.resolve(underlying, strike, strike_type)
        if entry is not None:
            return entry

    if require:
        raise HTTPException(
            status_code=404,
            detail=(
                "Instrument not found. Provide either `instrument_key` "
                "or all of `underlying`, `strike`, and `type`."
            ),
        )
    return None

def _selector_query(
    instrument_key: str | None,
    underlying: str | None,
    strike: str | None,
    strike_type: str | None,
) -> dict[str, Any]:
    """Build the echo object returned alongside single-instrument payloads."""
    return {
        "instrument_key": instrument_key,
        "underlying": underlying,
        "strike": strike,
        "type": strike_type,
    }

def _feed_snapshot(entry: dict[str, Any]) -> dict[str, Any]:
    """Return the current last-tick snapshot for one instrument, if any."""
    status = live_feed_service.status()
    last_tick = (status.get("last_tick_at") or {}).get(entry["instrument_key"])
    return {
        "instrument_key": entry["instrument_key"],
        "subscribed": live_feed_service.is_subscribed(entry["instrument_key"]),
        "last_tick_at": last_tick,
    }

# ---------------------------------------------------------------------------
# REST — status / registry
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
    underlying: str | None = Query(default=None),
    strike: str | None = Query(default=None),
    type: str | None = Query(default=None, alias="type"),
    instrument_key: str | None = Query(default=None),
) -> dict[str, Any]:
    entry = _resolve_entry(
        instrument_key=instrument_key,
        underlying=underlying,
        strike=strike,
        strike_type=type,
    )
    return entry  # type: ignore[return-value]

# ---------------------------------------------------------------------------
# REST — single-instrument reads
# ---------------------------------------------------------------------------

@router.get("/live/crosses/{instrument_key:path}")
def crosses_for_instrument(instrument_key: str) -> dict[str, Any]:
    """Today's crosses for one instrument (path form, kept for compat)."""
    entry = _resolve_entry(instrument_key=instrument_key)
    assert entry is not None
    return live_cross_store.read_today(entry)

@router.get("/live/crosses")
def crosses_query(
    instrument_key: str | None = Query(default=None),
    underlying: str | None = Query(default=None),
    strike: str | None = Query(default=None),
    type: str | None = Query(default=None, alias="type"),
) -> dict[str, Any]:
    """Single-instrument crosses (query form) OR all crosses when empty.

    * ``?instrument_key=...`` → crosses for that key
    * ``?underlying=NIFTY&strike=23400&type=CE`` → crosses for the resolved key
    * no params → crosses for every non-empty instrument
    """
    if instrument_key or (underlying and strike is not None and type):
        entry = _resolve_entry(
            instrument_key=instrument_key,
            underlying=underlying,
            strike=strike,
            strike_type=type,
        )
        assert entry is not None
        payload = live_cross_store.read_today(entry)
        payload["selector"] = _selector_query(
            instrument_key, underlying, strike, type
        )
        return payload

    out: list[dict[str, Any]] = []
    for entry in instrument_registry.all():
        payload = live_cross_store.read_today(entry)
        if payload.get("cross_count"):
            out.append(payload)
    return {"instrument_count": len(out), "payloads": out}

@router.get("/live/ema/{instrument_key:path}")
def ema_for_instrument(instrument_key: str) -> dict[str, Any]:
    """Current EMA snapshot for one instrument (path form)."""
    entry = _resolve_entry(instrument_key=instrument_key)
    assert entry is not None
    snapshot = live_ema_engine.get_ema_snapshot(entry["instrument_key"])
    snapshot["selector"] = {"instrument_key": instrument_key}
    return snapshot

@router.get("/live/ema")
def ema_query(
    instrument_key: str | None = Query(default=None),
    underlying: str | None = Query(default=None),
    strike: str | None = Query(default=None),
    type: str | None = Query(default=None, alias="type"),
) -> dict[str, Any]:
    """Current EMA snapshot for one instrument (key OR triple)."""
    entry = _resolve_entry(
        instrument_key=instrument_key,
        underlying=underlying,
        strike=strike,
        strike_type=type,
    )
    assert entry is not None
    snapshot = live_ema_engine.get_ema_snapshot(entry["instrument_key"])
    snapshot["selector"] = _selector_query(
        instrument_key, underlying, strike, type
    )
    return snapshot

@router.get("/live/ema-series")
def ema_series_query(
    instrument_key: str | None = Query(default=None),
    underlying: str | None = Query(default=None),
    strike: str | None = Query(default=None),
    type: str | None = Query(default=None, alias="type"),
    limit: int = Query(default=200, ge=0, le=2000),
) -> dict[str, Any]:
    """EMA series (per-minute close, fast EMA, slow EMA)."""
    entry = _resolve_entry(
        instrument_key=instrument_key,
        underlying=underlying,
        strike=strike,
        strike_type=type,
    )
    assert entry is not None
    payload = live_ema_engine.get_ema_series(entry["instrument_key"], limit=limit)
    payload["selector"] = _selector_query(
        instrument_key, underlying, strike, type
    )
    return payload

@router.get("/live/candles")
def candles_query(
    instrument_key: str | None = Query(default=None),
    underlying: str | None = Query(default=None),
    strike: str | None = Query(default=None),
    type: str | None = Query(default=None, alias="type"),
    limit: int = Query(default=200, ge=0, le=2000),
) -> dict[str, Any]:
    """Rolling 1-min candles the EMA engine currently holds for one key."""
    entry = _resolve_entry(
        instrument_key=instrument_key,
        underlying=underlying,
        strike=strike,
        strike_type=type,
    )
    assert entry is not None
    payload = live_ema_engine.get_candles(entry["instrument_key"], limit=limit)
    payload["selector"] = _selector_query(
        instrument_key, underlying, strike, type
    )
    return payload

@router.get("/live/snapshot/{instrument_key:path}")
def snapshot_for_instrument(instrument_key: str) -> dict[str, Any]:
    """Combined feed status + EMA snapshot for one instrument (path form)."""
    entry = _resolve_entry(instrument_key=instrument_key)
    assert entry is not None
    return {
        "instrument": entry,
        "feed": _feed_snapshot(entry),
        "ema": live_ema_engine.get_ema_snapshot(entry["instrument_key"]),
    }

@router.get("/live/snapshot")
def snapshot_query(
    instrument_key: str | None = Query(default=None),
    underlying: str | None = Query(default=None),
    strike: str | None = Query(default=None),
    type: str | None = Query(default=None, alias="type"),
) -> dict[str, Any]:
    """Combined feed status + EMA snapshot for one instrument (key OR triple)."""
    entry = _resolve_entry(
        instrument_key=instrument_key,
        underlying=underlying,
        strike=strike,
        strike_type=type,
    )
    assert entry is not None
    return {
        "selector": _selector_query(instrument_key, underlying, strike, type),
        "instrument": entry,
        "feed": _feed_snapshot(entry),
        "ema": live_ema_engine.get_ema_snapshot(entry["instrument_key"]),
    }

# ---------------------------------------------------------------------------
# WebSocket Helpers
# ---------------------------------------------------------------------------

def _parse_filters(ws: WebSocket):
    q = ws.query_params
    instrument_key = q.get("instrument_key") or q.get("key")
    underlying = q.get("underlying")
    strike = q.get("strike")
    type_ = q.get("type")
    all_ = (q.get("all", "false").lower() in {"1", "true", "yes"})
    return instrument_key, underlying, strike, type_, all_

def _ensure_subscription(instrument_key: str | None) -> None:
    if not instrument_key:
        return
    key = instrument_key.strip()
    if not instrument_registry.get(key):
        instrument_registry.register_adhoc(key)
    if not live_feed_service.is_subscribed(key):
        live_feed_service.subscribe_instrument(key)

async def _pump_with_client_listener(ws: WebSocket, sub) -> None:
    """Pump broadcast messages to client while concurrently listening for incoming subscriptions."""
    async def _send_loop():
        while True:
            message = await sub.queue.get()
            # Send message in standard payload structure compatible with both UI versions
            await ws.send_text(json.dumps(message, default=str))

    async def _recv_loop():
        while True:
            text = await ws.receive_text()
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    action = data.get("action") or data.get("type")
                    key = data.get("instrument_key") or data.get("key")
                    if action in {"subscribe", "sub"} and key:
                        _ensure_subscription(key)
                        sub.instrument_keys.add(key)
                        await ws.send_text(
                            json.dumps({"type": "subscribed", "instrument_key": key})
                        )
            except Exception:
                pass

    send_task = asyncio.create_task(_send_loop())
    recv_task = asyncio.create_task(_recv_loop())

    try:
        done, pending = await asyncio.wait(
            [send_task, recv_task], return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WebSocket pump error")
    finally:
        send_task.cancel()
        recv_task.cancel()
        await live_broadcast.unsubscribe(sub.id)
        try:
            await ws.close()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# WebSocket Routes
# ---------------------------------------------------------------------------

@router.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    await ws.accept()
    instrument_key, underlying, strike, type_, all_ = _parse_filters(ws)

    if instrument_key:
        _ensure_subscription(instrument_key)

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
    await _pump_with_client_listener(ws, sub)

@router.websocket("/ws/chart")
async def ws_chart(ws: WebSocket) -> None:
    await ws.accept()
    instrument_key, underlying, strike, type_, all_ = _parse_filters(ws)
    if instrument_key:
        _ensure_subscription(instrument_key)

    keys, triples, _ = live_broadcast.resolve_subscription_params(
        instrument_key, underlying, strike, type_
    )
    sub = await live_broadcast.subscribe(
        "feed",
        instrument_keys=keys,
        triples=triples,
        all_instruments=all_ if not keys else False,
    )
    await ws.send_text(
        json.dumps(
            {
                "type": "subscribed",
                "topic": "feed",
                "instrument_keys": list(sub.instrument_keys),
                "all": sub.all_instruments,
            }
        )
    )
    await _pump_with_client_listener(ws, sub)

@router.websocket("/all-feeds")
@router.websocket("/ws/all-feeds")
async def ws_all_feeds(ws: WebSocket) -> None:
    """Direct route for client charts pointing to /all-feeds.

    Subscribes dynamically to the requested instrument if provided, or enables
    full feed if requested.
    """
    await ws.accept()
    instrument_key, underlying, strike, type_, all_ = _parse_filters(ws)

    # If instrument_key is provided or all is set
    if instrument_key:
        _ensure_subscription(instrument_key)
        all_ = False
    else:
        # Default /all-feeds without params subscribes to all available
        all_ = True

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
                "status": "connected",
                "instrument_keys": list(sub.instrument_keys),
                "all": sub.all_instruments,
            }
        )
    )
    await _pump_with_client_listener(ws, sub)

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
    await _pump_with_client_listener(ws, sub)

@router.websocket("/ws/ema")
async def ws_ema(ws: WebSocket) -> None:
    """Stream the current EMA snapshot for a single instrument.

    Query params (either form accepted):
        * ``?instrument_key=NSE_FO|47162``
        * ``?underlying=NIFTY&strike=23400&type=CE``

    The snapshot is pushed every ``CHART_LIVE_POLL_SECONDS`` seconds
    (minimum 2s). Each message is the same shape returned by
    ``GET /live/ema``.
    """
    await ws.accept()
    instrument_key, underlying, strike, type_, _ = _parse_filters(ws)

    try:
        entry = _resolve_entry(
            instrument_key=instrument_key,
            underlying=underlying,
            strike=strike,
            strike_type=type,
        )
    except HTTPException as exc:
        await ws.send_text(json.dumps({"type": "error", "detail": exc.detail}))
        await ws.close()
        return

    assert entry is not None
    resolved_key = entry["instrument_key"]
    _ensure_subscription(resolved_key)

    await ws.send_text(
        json.dumps(
            {
                "type": "subscribed",
                "topic": "ema",
                "instrument_key": resolved_key,
                "instrument": entry,
            },
            default=str,
        )
    )

    interval = max(2, int(config.CHART_LIVE_POLL_SECONDS))

    async def _poll_loop() -> None:
        try:
            while True:
                snap = live_ema_engine.get_ema_snapshot(resolved_key)
                await ws.send_text(json.dumps(snap, default=str))
                await asyncio.sleep(interval)
        except WebSocketDisconnect:
            return
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("EMA WS poll loop failed for %s", resolved_key)

    async def _recv_loop() -> None:
        try:
            while True:
                # We don't need input, but we must keep reading so the
                # socket close event is detected promptly.
                await ws.receive_text()
        except WebSocketDisconnect:
            return
        except asyncio.CancelledError:
            return
        except Exception:
            return

    poll_task = asyncio.create_task(_poll_loop())
    recv_task = asyncio.create_task(_recv_loop())

    try:
        done, pending = await asyncio.wait(
            [poll_task, recv_task], return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
    finally:
        poll_task.cancel()
        recv_task.cancel()
        try:
            await ws.close()
        except Exception:
            pass