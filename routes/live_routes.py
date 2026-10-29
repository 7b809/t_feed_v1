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

Self-documenting help endpoint
------------------------------
``GET /live/ws-help`` returns the same guide in three formats:

    ?format=text  (default)  plain UTF-8 text
    ?format=html             HTML (renders in a browser)
    ?format=json             structured JSON (for programmatic use)

Alias: ``GET /ws-help``.

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
    GET  /live/ws-help                    <-- NEW: WebSocket guide

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
from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse

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
# WebSocket help — self-documenting
# ---------------------------------------------------------------------------
_WS_ROUTES: list[dict[str, Any]] = [
    {
        "path": "/ws/live",
        "aliases": [],
        "purpose": "General per-client feed. Any filter form accepted.",
        "requires_filter": False,
        "supports_all": True,
        "examples": [
            "ws://<host>/ws/live?instrument_key=NSE_FO%7C54452",
            "ws://<host>/ws/live?instruments=NSE_FO%7C54452,NSE_FO%7C54453",
            "ws://<host>/ws/live?underlying=NIFTY&strike=25000&type=CE",
            "ws://<host>/ws/live?all=true",
        ],
    },
    {
        "path": "/ws/chart",
        "aliases": [],
        "purpose": "Chart-page alias of /ws/live. `all` forced to false when a key is given.",
        "requires_filter": False,
        "supports_all": True,
        "examples": [
            "ws://<host>/ws/chart?instrument_key=NSE_FO%7C54452",
        ],
    },
    {
        "path": "/all-feeds",
        "aliases": ["/ws/all-feeds"],
        "purpose": (
            "Route used by templates/chart.html. No key → firehose "
            "(all registered instruments). Key → behaves like /ws/live."
        ),
        "requires_filter": False,
        "supports_all": True,
        "examples": [
            "ws://<host>/all-feeds?instrument_key=NSE_FO%7C54452",
            "ws://<host>/all-feeds?instruments=NSE_FO%7C1,NSE_FO%7C2",
            "ws://<host>/all-feeds   # firehose",
        ],
    },
    {
        "path": "/list-feeds",
        "aliases": ["/ws/list-feeds"],
        "purpose": (
            "Strict list-only feed. Rejects empty lists with close code "
            "1008 and enforces a 500-instrument cap. Never falls back to "
            "`all=true`."
        ),
        "requires_filter": True,
        "supports_all": False,
        "examples": [
            "ws://<host>/list-feeds?instruments=NSE_FO%7C1,NSE_FO%7C2,NSE_FO%7C3",
            "ws://<host>/list-feeds?instrument_key=NSE_FO%7C54452",
        ],
    },
]


def _ws_help_text(base_ws: str, base_http: str) -> str:
    """Plain-text guide rendered by ``GET /live/ws-help``."""
    lines: list[str] = []
    add = lines.append

    add("=" * 64)
    add("LIVE FEED — WEBSOCKET HELP")
    add("=" * 64)
    add("")
    add(f"Base WS URL:    {base_ws}")
    add(f"Base HTTP URL:  {base_http}")
    add("")
    add("All sockets publish the 'feed' topic. Text frames, JSON payloads.")
    add("Ticks are only emitted during NSE/BSE trading hours")
    add("(09:15–15:30 IST, Mon–Fri, non-holiday).")
    add("")

    add("-" * 64)
    add("ENDPOINTS")
    add("-" * 64)
    add("")
    for route in _WS_ROUTES:
        add(f"  {route['path']}")
        if route.get("aliases"):
            add(f"    aliases: {', '.join(route['aliases'])}")
        add(f"    purpose: {route['purpose']}")
        add(f"    requires_filter: {route['requires_filter']}")
        add(f"    supports_all=true: {route['supports_all']}")
        add("    examples:")
        for ex in route["examples"]:
            add(f"      {ex}")
        add("")

    add("-" * 64)
    add("URL-ENCODING")
    add("-" * 64)
    add("")
    add("  Instrument keys contain a pipe (|) and sometimes a space")
    add("  (NSE_INDEX|Nifty 50). Encode them in the URL:")
    add("")
    add("      NSE_FO|54452          ->  NSE_FO%7C54452")
    add("      NSE_INDEX|Nifty 50    ->  NSE_INDEX%7CNifty%2050")
    add("")
    add("  Multiple instruments MUST be comma-separated. Whitespace is")
    add("  NOT a separator — Upstox index keys contain spaces.")
    add("")

    add("-" * 64)
    add("CLIENT -> SERVER CONTROL FRAMES (no reconnect needed)")
    add("-" * 64)
    add("")
    add('  {"action":"subscribe",   "instrument_keys":["NSE_FO|1","NSE_FO|2"]}')
    add('  {"action":"unsubscribe", "instrument_keys":["NSE_FO|1"]}')
    add('  {"action":"set",         "instrument_keys":["NSE_FO|9"]}   # replace')
    add('  {"action":"clear"}                                          # empty set')
    add("")
    add("  Aliases: 'instruments' for 'instrument_keys';")
    add("           'sub' / 'unsub' for 'subscribe' / 'unsubscribe'.")
    add("")

    add("-" * 64)
    add("SERVER -> CLIENT FRAMES")
    add("-" * 64)
    add("")
    add("  Handshake (sent once on connect):")
    add('    {"type":"subscribed","topic":"feed","status":"connected",')
    add('     "instrument_keys":["NSE_FO|54452"],"all":false}')
    add("")
    add("  Tick (many, as they arrive):")
    add('    {"instrument_key":"NSE_FO|54452",')
    add('     "timestamp":"2026-10-29T09:15:32.123456+00:00",')
    add('     "ltp":177.35,"prev_close":168.20,"volume":45210,')
    add('     "candle_1m":{"open":176.9,"high":177.5,')
    add('                  "low":176.7,"close":177.35},')
    add('     "underlying":"NIFTY","strike_price":25000,"strike_type":"CE"}')
    add("")
    add("  Control confirmations:")
    add('    {"type":"subscribed","instrument_keys":[...],"total_subscriptions":N}')
    add('    {"type":"unsubscribed","instrument_keys":[...],"total_subscriptions":N}')
    add('    {"type":"subscriptions_replaced","instrument_keys":[...],"total_subscriptions":N}')
    add('    {"type":"subscriptions_cleared","total_subscriptions":0}')
    add('    {"type":"error","error":"<ExceptionType>: <message>"}')
    add("")

    add("-" * 64)
    add("CLOSE CODES")
    add("-" * 64)
    add("")
    add("  1000  Normal closure (client navigated away, market closed)")
    add("  1008  Policy violation — /list-feeds with no instruments,")
    add("        or > 500 instruments requested")
    add("")

    add("-" * 64)
    add("PYTHON EXAMPLE")
    add("-" * 64)
    add("")
    add("  import asyncio, json, websockets")
    add("")
    add("  async def main():")
    add(f'      url = "{base_ws}/all-feeds?instrument_key=NSE_FO%7C54452"')
    add("      async with websockets.connect(url) as ws:")
    add("          print(json.loads(await ws.recv()))   # handshake")
    add("          while True:")
    add("              frame = json.loads(await ws.recv())")
    add("              if frame.get('type') == 'error':")
    add("                  print('ERROR:', frame); continue")
    add("              print(frame.get('instrument_key'), frame.get('ltp'))")
    add("")
    add("  asyncio.run(main())")
    add("")

    add("-" * 64)
    add("BROWSER (JAVASCRIPT) EXAMPLE")
    add("-" * 64)
    add("")
    add("  const key = 'NSE_FO|54452';")
    add(f"  const ws  = new WebSocket(")
    add(f"    '{base_ws}/all-feeds?instrument_key=' + encodeURIComponent(key)")
    add("  );")
    add("  ws.onmessage = (ev) => {")
    add("    const f = JSON.parse(ev.data);")
    add("    if (f.type) return;   // handshake / control frame")
    add("    if (f.instrument_key !== key) return;")
    add("    console.log(f.ltp, f.candle_1m);")
    add("  };")
    add("")

    add("-" * 64)
    add("COMPANION REST ENDPOINTS")
    add("-" * 64)
    add("")
    add(f"  GET  {base_http}/live/status")
    add(f"  GET  {base_http}/live/instruments")
    add(f"  GET  {base_http}/live/instruments/resolve?underlying=NIFTY&strike=25000&type=CE")
    add(f"  GET  {base_http}/api/upstox/subscription-status?instrument_key=NSE_FO%7C54452")
    add(f"  POST {base_http}/api/upstox/subscribe")
    add("")
    add("=" * 64)
    return "\n".join(lines)


def _ws_help_html(base_ws: str, base_http: str) -> str:
    """HTML rendering of the same guide (pretty in a browser)."""
    text = _ws_help_text(base_ws, base_http)
    # HTML-escape the text block and wrap it in a minimal styled <pre>.
    escaped = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return (
        "<!doctype html>"
        "<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>Live Feed — WebSocket Help</title>"
        "<style>"
        "body{background:#0f1115;color:#d1d4dc;font-family:"
        "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;"
        "padding:24px;line-height:1.5;}"
        "pre{background:#161a23;border:1px solid #2a2e39;"
        "border-radius:10px;padding:20px;overflow-x:auto;"
        "font-size:13px;}"
        "h1{font-family:system-ui,sans-serif;font-size:18px;"
        "color:#fff;margin:0 0 16px;}"
        "a{color:#7ea6ff;}"
        "</style></head><body>"
        "<h1>Live Feed — WebSocket Help</h1>"
        "<p style='font-family:system-ui,sans-serif;font-size:13px'>"
        f"Same content as <code>?</code> "
        f"<a href=\"{base_http}/live/ws-help?format=text\">?format=text</a> "
        f"and <a href=\"{base_http}/live/ws-help?format=json\">?format=json</a>."
        "</p>"
        f"<pre>{escaped}</pre>"
        "</body></html>"
    )


def _ws_help_json(base_ws: str, base_http: str) -> dict[str, Any]:
    """Structured JSON guide (for programmatic consumption)."""
    return {
        "title": "Live Feed — WebSocket Help",
        "base_ws_url": base_ws,
        "base_http_url": base_http,
        "trading_hours": "09:15–15:30 IST, Mon–Fri, non-holiday",
        "topic": "feed",
        "endpoints": _WS_ROUTES,
        "url_encoding": {
            "pipe": "|",
            "pipe_encoded": "%7C",
            "space": " ",
            "space_encoded": "%20",
            "separator": ",",
            "separator_note": (
                "Only commas separate multiple instruments. Whitespace "
                "is NOT a separator because Upstox index keys contain "
                "spaces (e.g. 'NSE_INDEX|Nifty 50')."
            ),
        },
        "client_control_frames": [
            {"action": "subscribe",   "instrument_keys": ["NSE_FO|1", "NSE_FO|2"]},
            {"action": "unsubscribe", "instrument_keys": ["NSE_FO|1"]},
            {"action": "set",         "instrument_keys": ["NSE_FO|9"]},
            {"action": "clear"},
        ],
        "server_frames": {
            "handshake": {
                "type": "subscribed",
                "topic": "feed",
                "status": "connected",
                "instrument_keys": ["NSE_FO|54452"],
                "all": False,
            },
            "tick": {
                "instrument_key": "NSE_FO|54452",
                "timestamp": "2026-10-29T09:15:32.123456+00:00",
                "ltp": 177.35,
                "prev_close": 168.20,
                "volume": 45210,
                "candle_1m": {
                    "open": 176.9, "high": 177.5,
                    "low": 176.7,  "close": 177.35,
                },
                "underlying": "NIFTY",
                "strike_price": 25000,
                "strike_type": "CE",
            },
            "control_confirmations": [
                {"type": "subscribed"},
                {"type": "unsubscribed"},
                {"type": "subscriptions_replaced"},
                {"type": "subscriptions_cleared"},
                {"type": "error"},
            ],
        },
        "close_codes": {
            1000: "Normal closure",
            1008: "Policy violation — list-feeds with no instruments, or > 500",
        },
        "companion_rest": {
            "status":            f"{base_http}/live/status",
            "instruments":       f"{base_http}/live/instruments",
            "resolve":           f"{base_http}/live/instruments/resolve",
            "subscription_status": f"{base_http}/api/upstox/subscription-status",
            "subscribe":         f"{base_http}/api/upstox/subscribe",
        },
    }


def _resolve_base_urls(request: Request) -> tuple[str, str]:
    """Return ``(base_ws, base_http)`` derived from the incoming request."""
    scheme = request.url.scheme  # "http" or "https"
    host = request.headers.get("host") or request.url.netloc
    http = f"{scheme}://{host}"
    ws_scheme = "wss" if scheme == "https" else "ws"
    ws_base = f"{ws_scheme}://{host}"
    return ws_base, http


@router.get(
    "/live/ws-help",
    summary="WebSocket connection guide",
    description=(
        "Returns a self-documenting guide for every WebSocket route. "
        "Supports `?format=text` (default), `?format=html`, or "
        "`?format=json`."
    ),
    tags=["websockets"],
)
@router.get("/ws-help", include_in_schema=False)
def ws_help(
    request: Request,
    format: str = Query(
        default="text",
        description="One of: text | html | json",
        pattern="^(text|html|json)$",
    ),
):
    base_ws, base_http = _resolve_base_urls(request)

    if format == "html":
        return HTMLResponse(content=_ws_help_html(base_ws, base_http))
    if format == "json":
        return _ws_help_json(base_ws, base_http)
    return PlainTextResponse(content=_ws_help_text(base_ws, base_http))


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