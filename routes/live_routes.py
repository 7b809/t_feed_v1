# routes/live_routes.py
"""Live data REST and WebSocket routes.

Every endpoint that reads a *single* instrument accepts either an
explicit ``instrument_key`` **or** the three-part selector
``underlying`` + ``strike`` + ``type``.

All selector values are normalized through ``services.normalization``
before lookup, so "nifty", "NiFty", "NIFTY 50", "nifty50" all map to
the same registry triple, and "ce", "Ce", "call", "C" all map to
"CE".

On a triple miss the route makes one best-effort Upstox search (when
``config.LIVE_RESOLVE_ON_MISS`` is True and a token is available),
registers the result ad-hoc, and only then returns 404 — so any real
strike resolves once.

WebSocket routes
----------------
    WS   /ws/live           — single instrument OR triple OR all=true
    WS   /ws/chart          — same as /ws/live (chart-page convenience)
    WS   /all-feeds         — all instruments (unless a key/triple given)
    WS   /ws/all-feeds      — alias of /all-feeds
    WS   /list-feeds        — NEW: subscribe to a LIST of instruments
    WS   /ws/list-feeds     — alias of /list-feeds

List-of-feeds
-------------
``/list-feeds`` requires an explicit list of instruments, passed as:

    ?instruments=NSE_FO|123,NSE_FO|456,NSE_FO|789
    ?keys=NSE_FO|123,NSE_FO|456          (alias)
    ?instrument_key=NSE_FO|123           (single, still accepted)

Multiple keys MUST be comma-separated. Whitespace is NOT a separator
because Upstox index keys contain spaces (e.g. ``NSE_INDEX|Nifty 50``).

The connection receives **only** ticks for the listed instruments.
Clients can update the list live without reconnecting by sending:

    {"action": "subscribe",   "instrument_keys": ["NSE_FO|123", ...]}
    {"action": "unsubscribe", "instrument_keys": ["NSE_FO|123", ...]}
    {"action": "set",         "instrument_keys": ["NSE_FO|123", ...]}   # replace
    {"action": "clear"}                                                  # empty list

``instruments`` (comma-separated) is also accepted on the message body
in place of ``instrument_keys`` for symmetry with the query string.

Every existing route (``/ws/live``, ``/ws/chart``, ``/all-feeds``) also
accepts the comma-separated ``instruments=`` / ``keys=`` form now, so
a caller can subscribe to several keys on any WS path. Behaviour is
otherwise unchanged.

Strategy-layer removal
----------------------
The EMA engine and live-cross store were deleted with the strategy
layer. Their routes (/live/ema*, /live/candles, /live/crosses,
/live/snapshot, /ws/ema-crosses, /ws/ema) have been removed. The
remaining endpoints only touch the feed snapshot layer:

    GET  /live/status
    GET  /live/instruments
    GET  /live/instruments/resolve

    # WebSockets
    WS   /ws/live
    WS   /ws/chart
    WS   /all-feeds  (and /ws/all-feeds)
    WS   /list-feeds (and /ws/list-feeds)          <-- NEW
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import requests
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from core import config
from services.instrument_registry import instrument_registry
from services.live_broadcast import live_broadcast
from services.live_feed_service import live_feed_service
from services.normalization import (
    normalize_strike,
    normalize_strike_type,
    normalize_underlying,
)
from services.token_service import token_service

logger = logging.getLogger(__name__)

router = APIRouter()

_UPSTOX_SEARCH_URL = "https://api.upstox.com/v2/instruments/search"
_UPSTOX_RESOLVE_TIMEOUT = 10

# Soft cap on a single /list-feeds subscription so a stray client cannot
# ask for every listed contract on the exchange.
_MAX_LIST_FEED_INSTRUMENTS = 500


# ---------------------------------------------------------------------------
# On-miss Upstox resolution
# ---------------------------------------------------------------------------
def _resolve_via_upstox(
    underlying: str,
    strike: str,
    strike_type: str,
) -> dict[str, Any] | None:
    """Best-effort Upstox search for a triple not in the registry.

    Returns the newly registered entry on success, or None when the
    token is missing, the search fails, or no matching contract is
    found. Never raises.
    """
    if not getattr(config, "LIVE_RESOLVE_ON_MISS", True):
        return None

    # Safe conversion — a non-numeric strike must yield a clean miss,
    # not a 500 further down the comparison.
    try:
        target_strike = float(strike)
    except (TypeError, ValueError):
        return None

    try:
        token = token_service.get_access_token()
    except Exception:
        token = None
    if not token:
        return None

    query = f"{underlying} {strike} {strike_type}"
    try:
        resp = requests.get(
            _UPSTOX_SEARCH_URL,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            params={
                "query": query,
                "exchanges": "NSE,BSE",
                "segments": "FO",
                "instrument_types": strike_type,
                "records": 5,
                "page_number": 1,
            },
            timeout=_UPSTOX_RESOLVE_TIMEOUT,
        )
    except requests.RequestException:
        logger.info(
            "Upstox on-miss resolution network error for %s", query,
        )
        return None

    if resp.status_code >= 400:
        logger.info(
            "Upstox on-miss resolution HTTP %s for %s",
            resp.status_code, query,
        )
        return None

    try:
        items = resp.json().get("data") or []
    except ValueError:
        return None

    match_key: str | None = None
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            sp = float(item.get("strike_price"))
        except (TypeError, ValueError):
            continue
        it = str(item.get("instrument_type") or "").upper()
        if int(sp) == int(target_strike) and it == strike_type:
            key = item.get("instrument_key")
            if key:
                match_key = str(key)
                break

    if not match_key:
        return None

    try:
        entry = instrument_registry.register_adhoc(
            match_key,
            trading_symbol=f"{underlying} {strike} {strike_type}",
            underlying=underlying,
            strike_price=int(target_strike),
            strike_type=strike_type,
        )
    except Exception:
        logger.exception(
            "register_adhoc failed after Upstox resolution: %s", match_key
        )
        return None

    logger.info(
        "On-miss Upstox resolution: %s %s %s -> %s",
        underlying, strike, strike_type, match_key,
    )
    return entry


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
        1. Explicit ``instrument_key`` — direct registry lookup,
           falling back to ad-hoc registration.
        2. ``underlying`` + ``strike`` + ``strike_type`` triple lookup
           on normalized values.
        3. Alias-aware second pass (e.g. "NIFTY" tries "NIFTY 50" and
           "NIFTY50" too).
        4. Best-effort Upstox on-miss resolution.

    When ``require`` is True, raises HTTP 404 with a detailed body
    if nothing matches.
    """
    key_raw = str(instrument_key).strip() if instrument_key else None
    u = normalize_underlying(underlying)
    s = normalize_strike(strike)
    t = normalize_strike_type(strike_type)

    # 1. explicit instrument_key
    if key_raw:
        entry = instrument_registry.get(key_raw)
        if entry is None:
            entry = instrument_registry.register_adhoc(key_raw)
        return entry

    # 2. triple lookup
    if u and s and t:
        entry = instrument_registry.resolve(u, s, t)
        if entry is not None:
            return entry

        # 2b. alias-aware second pass. If the user typed "NIFTY" but
        #     the index is registered under "NIFTY 50" (or vice versa),
        #     try the alternate forms before giving up.
        for alt in (f"{u} 50", f"{u}50", u.split(" ", 1)[0]):
            alt_norm = normalize_underlying(alt)
            if not alt_norm or alt_norm == u:
                continue
            entry = instrument_registry.resolve(alt_norm, s, t)
            if entry is not None:
                return entry

        # 3. best-effort on-miss Upstox resolution
        entry = _resolve_via_upstox(u, s, t)
        if entry is not None:
            return entry

    if require:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "instrument_not_found",
                "message": (
                    "Instrument not found. Provide either "
                    "`instrument_key` or all of `underlying`, "
                    "`strike`, and `type`."
                ),
                "selector": {
                    "instrument_key": key_raw,
                    "underlying": u or None,
                    "strike": s or None,
                    "type": t or None,
                },
                "hint": (
                    "Pass the exact `instrument_key` or select the "
                    "strike/type from /live/instruments."
                ),
            },
        )
    return None


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
        underlying=normalize_underlying(underlying) or underlying,
        strike_type=normalize_strike_type(type) or type,
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
# WebSocket helpers
# ---------------------------------------------------------------------------
def _parse_filters(
    ws: WebSocket,
) -> tuple[list[str], str | None, str | None, str | None, bool]:
    """Return ``(keys, underlying, strike, type, all_)`` from query params.

    ``keys`` is always a list — a single ``instrument_key=`` produces a
    one-element list, while ``instruments=a,b,c`` (or ``keys=a,b,c``)
    produces the full list.

    Multiple keys MUST be comma-separated. Whitespace is NOT a
    separator because Upstox index keys contain spaces, e.g.
    ``NSE_INDEX|Nifty 50``. Parsing is delegated to
    ``live_broadcast.parse_instrument_list`` so every WS route shares
    the same rule.
    """
    q = ws.query_params
    raw_keys = (
        q.get("instruments")
        or q.get("keys")
        or q.get("instrument_keys")
        or q.get("instrument_key")
        or q.get("key")
    )
    keys = live_broadcast.parse_instrument_list(raw_keys)
    underlying = q.get("underlying")
    strike = q.get("strike")
    type_ = q.get("type")
    all_ = (q.get("all", "false").lower() in {"1", "true", "yes"})
    return keys, underlying, strike, type_, all_


def _ensure_subscription(instrument_key: str | None) -> None:
    """Register + subscribe a single key with the live feed."""
    if not instrument_key:
        return
    key = str(instrument_key).strip()
    if not key:
        return
    if not instrument_registry.get(key):
        instrument_registry.register_adhoc(key)
    if not live_feed_service.is_subscribed(key):
        live_feed_service.subscribe_instrument(key)


def _ensure_subscriptions(keys: list[str] | None) -> None:
    """Register + subscribe every key in ``keys`` with the live feed."""
    if not keys:
        return
    for k in keys:
        _ensure_subscription(k)


async def _pump_with_client_listener(ws: WebSocket, sub) -> None:
    """Pump broadcast messages to client while concurrently listening
    for incoming subscription messages.

    Supported client -> server messages::

        {"action": "subscribe",   "instrument_keys": ["a", "b"]}
        {"action": "unsubscribe", "instrument_keys": ["a"]}
        {"action": "set",         "instrument_keys": ["a", "b"]}   # replace
        {"action": "clear"}                                        # empty list

    ``instruments`` (comma-separated) is accepted as an alias for
    ``instrument_keys`` so callers can mirror the query-string form.
    """
    async def _send_loop():
        while True:
            message = await sub.queue.get()
            await ws.send_text(json.dumps(message, default=str))

    async def _recv_loop():
        while True:
            text = await ws.receive_text()
            try:
                data = json.loads(text)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue

            action = data.get("action") or data.get("type")
            raw_keys = (
                data.get("instrument_keys")
                or data.get("instruments")
                or data.get("instrument_key")
                or data.get("key")
            )
            keys = live_broadcast.parse_instrument_list(raw_keys)

            try:
                if action in {"subscribe", "sub"} and keys:
                    _ensure_subscriptions(keys)
                    for k in keys:
                        sub.instrument_keys.add(k)
                    await ws.send_text(json.dumps({
                        "type": "subscribed",
                        "instrument_keys": keys,
                        "total_subscriptions": len(sub.instrument_keys),
                    }))
                elif action in {"unsubscribe", "unsub"} and keys:
                    for k in keys:
                        sub.instrument_keys.discard(k)
                    await ws.send_text(json.dumps({
                        "type": "unsubscribed",
                        "instrument_keys": keys,
                        "total_subscriptions": len(sub.instrument_keys),
                    }))
                elif action == "set" and keys:
                    _ensure_subscriptions(keys)
                    sub.instrument_keys = set(keys)
                    await ws.send_text(json.dumps({
                        "type": "subscriptions_replaced",
                        "instrument_keys": list(sub.instrument_keys),
                        "total_subscriptions": len(sub.instrument_keys),
                    }))
                elif action == "clear":
                    sub.instrument_keys = set()
                    await ws.send_text(json.dumps({
                        "type": "subscriptions_cleared",
                        "total_subscriptions": 0,
                    }))
            except Exception as exc:
                try:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }))
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
# WebSocket routes
# ---------------------------------------------------------------------------
@router.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    await ws.accept()
    keys_param, underlying, strike, type_, all_ = _parse_filters(ws)

    if keys_param:
        _ensure_subscriptions(keys_param)

    keys, triples, _ = live_broadcast.resolve_subscription_params(
        keys_param, underlying, strike, type_
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
    keys_param, underlying, strike, type_, all_ = _parse_filters(ws)
    if keys_param:
        _ensure_subscriptions(keys_param)

    keys, triples, _ = live_broadcast.resolve_subscription_params(
        keys_param, underlying, strike, type_
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
    """Direct route for client charts pointing to /all-feeds."""
    await ws.accept()
    keys_param, underlying, strike, type_, all_ = _parse_filters(ws)

    if keys_param:
        _ensure_subscriptions(keys_param)
        all_ = False
    else:
        all_ = True

    keys, triples, _ = live_broadcast.resolve_subscription_params(
        keys_param, underlying, strike, type_
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


@router.websocket("/list-feeds")
@router.websocket("/ws/list-feeds")
async def ws_list_feeds(ws: WebSocket) -> None:
    """Subscribe to an explicit LIST of instruments only.

    Query string::

        ?instruments=NSE_FO|123,NSE_FO|456,NSE_FO|789
        ?keys=NSE_FO|123,NSE_FO|456        (alias)
        ?instrument_key=NSE_FO|123         (single, still accepted)

    Unlike ``/all-feeds``, this route **rejects** a connection that does
    not specify at least one instrument. That keeps the contract clear:
    “I want these specific instruments, nothing else”.

    The subscription list can be updated live (without reconnecting) by
    sending any of the messages handled by ``_pump_with_client_listener``
    — ``subscribe`` / ``unsubscribe`` / ``set`` / ``clear``.
    """
    await ws.accept()

    keys_param, underlying, strike, type_, _ = _parse_filters(ws)

    if not keys_param and not (underlying and strike and type_):
        await ws.close(
            code=1008,
            reason=(
                "list-feeds requires at least one instrument — pass "
                "`instruments=a,b,c` or `instrument_key=...`"
            ),
        )
        return

    if len(keys_param) > _MAX_LIST_FEED_INSTRUMENTS:
        await ws.close(
            code=1008,
            reason=(
                f"too many instruments in list-feeds "
                f"(max {_MAX_LIST_FEED_INSTRUMENTS})"
            ),
        )
        return

    if keys_param:
        _ensure_subscriptions(keys_param)

    keys, triples, _ = live_broadcast.resolve_subscription_params(
        keys_param, underlying, strike, type_
    )

    if not keys and not triples:
        await ws.close(
            code=1008,
            reason="no resolvable instruments in the requested list",
        )
        return

    sub = await live_broadcast.subscribe(
        "feed",
        instrument_keys=keys,
        triples=triples,
        all_instruments=False,   # list-feeds never falls back to "all"
    )

    await ws.send_text(
        json.dumps(
            {
                "type": "subscribed",
                "topic": "feed",
                "mode": "list",
                "instrument_keys": list(sub.instrument_keys),
                "triples": [list(t) for t in sub.triples],
                "total_subscriptions": len(sub.instrument_keys),
                "all": False,
            }
        )
    )
    await _pump_with_client_listener(ws, sub)