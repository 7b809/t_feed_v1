"""Our own WebSocket endpoints for live EMA updates, plus discovery + sample APIs.

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

GET /api/live-ema/ws-samples
    Full reference: client code snippets (Python / browser JS / Node.js /
    websocat / wscat), sample payloads for every message type, connection
    lifecycle, and error codes. Intended for future reference / onboarding.
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
# REST: full sample reference (client code + payloads)
# ---------------------------------------------------------------------------
@router.get("/api/live-ema/ws-samples")
async def ws_samples() -> dict[str, Any]:
    """Return a complete, copy-pasteable reference for the EMA WebSocket API.

    The payload contains:

    * ``overview``        — what the channel is and how it is scheduled.
    * ``endpoints``       — every WS route with templates + examples.
    * ``message_types``   — description of every event type.
    * ``samples``         — client code in several languages.
    * ``sample_messages`` — real payloads for each event type.
    * ``lifecycle``       — sequence of events on connect / disconnect.
    * ``error_codes``     — close codes + payloads the client may receive.
    * ``config``          — the settings that drive the channel.

    All string templates use ``{base_ws_url}`` / ``{instrument_key}`` so the
    caller can substitute their own host and instrument.
    """

    interval = settings.live_ema_interval_seconds
    backend = "ltp" if settings.use_live_ltp_feed else "rest"

    endpoints = [
        {
            "name": "by-instrument",
            "url_template": "/ws/live-ema/by-instrument/{instrument_key}",
            "example": "/ws/live-ema/by-instrument/NSE_INDEX%7CNifty%2050",
        },
        {
            "name": "by-parts",
            "url_template": (
                "/ws/live-ema/by-parts"
                "?underlying={underlying}&strike={strike}&type={type}"
            ),
            "example": "/ws/live-ema/by-parts?underlying=nifty&strike=25000&type=CE",
        },
    ]

    message_types = {
        "snapshot": {
            "when": "Immediately after the socket is accepted.",
            "purpose": "Initial state: instrument metadata + server config.",
            "carries_ema": False,
        },
        "tick": {
            "when": "Every upstream LTP tick (multiple per second).",
            "purpose": "Live price + forming (unclosed) candle.",
            "carries_ema": False,
        },
        "candle_completed": {
            "when": (
                f"Once every {interval} seconds when a bucket closes. "
                "This is the only event that can carry EMA values."
            ),
            "purpose": "Closed OHLC + recomputed fast/slow EMA.",
            "carries_ema": True,
        },
        "ema_cross": {
            "when": (
                "Only when a *completed* candle produces a new bullish / "
                "bearish cross. Never emitted for a partial candle."
            ),
            "purpose": "Actionable alert.",
            "carries_ema": True,
        },
        "error": {
            "when": (
                "On connect if the instrument cannot be resolved, or on "
                "unexpected server-side issues."
            ),
            "purpose": "Human-readable failure reason.",
            "carries_ema": False,
        },
    }

    # ------------------------------------------------------------------
    # Client code samples
    # ------------------------------------------------------------------
    python_sample = '''\
# pip install websockets
import asyncio
import json
from urllib.parse import quote

import websockets


INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"
BASE_WS_URL = "ws://127.0.0.1:8002"


async def main() -> None:
    encoded = quote(INSTRUMENT_KEY, safe="")
    url = f"{BASE_WS_URL}/ws/live-ema/by-instrument/{encoded}"

    async with websockets.connect(url, ping_interval=20) as ws:
        async for raw in ws:
            msg = json.loads(raw)
            kind = msg.get("type")

            if kind == "snapshot":
                print("connected:", msg["instrument_key"])

            elif kind == "tick":
                data = msg["data"]
                forming = data.get("forming_candle") or {}
                print(f"tick ltp={data['ltp']} "
                      f"bucket_close={forming.get('close')}")

            elif kind == "candle_completed":
                data = msg["data"]
                candle = data["candle"]
                ema = data.get("ema")
                cross = data.get("cross")
                print(f"candle closed ts={candle['timestamp']} "
                      f"o={candle['open']} h={candle['high']} "
                      f"l={candle['low']} c={candle['close']} "
                      f"ema={ema} cross={cross}")

            elif kind == "ema_cross":
                data = msg["data"]
                print(f"CROSS {data['type']} ts={data['timestamp']} "
                      f"close={data['close']} "
                      f"fast={data['ema_fast']} slow={data['ema_slow']}")

            elif kind == "error":
                print("server error:", msg["error"])
                break


if __name__ == "__main__":
    asyncio.run(main())
'''

    browser_sample = '''\
// Vanilla browser client — no libraries needed.
const INSTRUMENT_KEY = "NSE_INDEX|Nifty 50";
const BASE_WS_URL = (location.protocol === "https:" ? "wss://" : "ws://")
  + location.host;

const url = `${BASE_WS_URL}/ws/live-ema/by-instrument/`
  + encodeURIComponent(INSTRUMENT_KEY);

const ws = new WebSocket(url);

ws.addEventListener("open", () => console.log("ws open"));

ws.addEventListener("message", (ev) => {
  const msg = JSON.parse(ev.data);

  switch (msg.type) {
    case "snapshot":
      console.log("snapshot:", msg.instrument_key);
      break;

    case "tick": {
      const d = msg.data;
      console.log("tick", d.ltp, d.forming_candle);
      break;
    }

    case "candle_completed": {
      const { candle, ema, cross } = msg.data;
      console.log("candle", candle.timestamp, candle.close, ema, cross);
      break;
    }

    case "ema_cross":
      console.log("CROSS", msg.data.type, msg.data.timestamp);
      break;

    case "error":
      console.error("server error:", msg.error);
      break;
  }
});

ws.addEventListener("close", (ev) => {
  console.warn("ws closed", ev.code, ev.reason);
});

ws.addEventListener("error", (err) => console.error("ws error", err));
'''

    node_sample = '''\
// Node.js 20+ (built-in WebSocket; no extra packages).
const INSTRUMENT_KEY = "NSE_INDEX|Nifty 50";
const BASE_WS_URL = "ws://127.0.0.1:8002";

const url = `${BASE_WS_URL}/ws/live-ema/by-instrument/`
  + encodeURIComponent(INSTRUMENT_KEY);

const ws = new WebSocket(url);

ws.addEventListener("open", () => console.log("ws open"));

ws.addEventListener("message", (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.type === "ema_cross") {
    console.log("CROSS", msg.data);
  } else if (msg.type === "candle_completed") {
    console.log("candle", msg.data.candle.timestamp, msg.data.candle.close);
  } else if (msg.type === "tick") {
    console.log("tick", msg.data.ltp);
  } else if (msg.type === "snapshot") {
    console.log("snapshot", msg.instrument_key);
  } else if (msg.type === "error") {
    console.error("server error", msg.error);
  }
});

ws.addEventListener("close", (ev) => console.warn("closed", ev.code, ev.reason));
ws.addEventListener("error", (err) => console.error("error", err));
'''

    websocat_sample = '''\
# websocat — quickest way to eyeball the stream from a terminal.
# Install: https://github.com/vi/websocat

websocat 'ws://127.0.0.1:8002/ws/live-ema/by-instrument/NSE_INDEX%7CNifty%2050'

# by-parts variant
websocat 'ws://127.0.0.1:8002/ws/live-ema/by-parts?underlying=nifty&strike=25000&type=CE'
'''

    wscat_sample = '''\
# wscat — Node-based alternative to websocat.
# Install: npm i -g wscat

wscat -c 'ws://127.0.0.1:8002/ws/live-ema/by-instrument/NSE_INDEX%7CNifty%2050'

# The server does not expect client -> server payloads; it will simply
# ignore anything you type. Close with Ctrl-C.
'''

    samples = {
        "python": python_sample,
        "browser_javascript": browser_sample,
        "node_javascript": node_sample,
        "websocat": websocat_sample,
        "wscat": wscat_sample,
    }

    # ------------------------------------------------------------------
    # Sample payloads (matching what the server actually emits)
    # ------------------------------------------------------------------
    sample_messages = {
        "snapshot": {
            "type": "snapshot",
            "server_time": "2026-10-10T04:15:00.123456+00:00",
            "instrument_key": "NSE_INDEX|Nifty 50",
            "instrument": {
                "instrument_key": "NSE_INDEX|Nifty 50",
                "trading_symbol": "NIFTY 50",
                "instrument_type": None,
                "strike_price": None,
                "expiry": None,
                "underlying_key": "NSE_INDEX|Nifty 50",
            },
            "server": {
                "use_live_ltp_feed": settings.use_live_ltp_feed,
                "interval_seconds": interval,
            },
        },
        "tick": {
            "type": "tick",
            "server_time": "2026-10-10T04:15:01.555012+00:00",
            "instrument_key": "NSE_INDEX|Nifty 50",
            "data": {
                "ltp": 22680.95,
                "ltt": "1791348706000",
                "cp": 22776.1,
                "forming_candle": {
                    "timestamp": "2026-10-10T09:45:00+05:30",
                    "open": 22678.4,
                    "high": 22682.15,
                    "low": 22677.9,
                    "close": 22680.95,
                    "tick_count": 42,
                    "interval_seconds": interval,
                },
            },
        },
        "candle_completed": {
            "type": "candle_completed",
            "server_time": "2026-10-10T04:16:00.041220+00:00",
            "instrument_key": "NSE_INDEX|Nifty 50",
            "data": {
                "candle": {
                    "timestamp": "2026-10-10T09:45:00+05:30",
                    "open": 22678.4,
                    "high": 22682.15,
                    "low": 22677.9,
                    "close": 22680.95,
                    "volume": 0,
                    "oi": 0,
                    "tick_count": 87,
                    "interval_seconds": interval,
                },
                "ema": {
                    "fast_period": settings.ema_fast_period,
                    "slow_period": settings.ema_slow_period,
                    "ema_fast": 22679.4421,
                    "ema_slow": 22681.0988,
                    "ema_fast_prev": 22678.8123,
                    "ema_slow_prev": 22680.6601,
                },
                "cross": None,
            },
        },
        "ema_cross": {
            "type": "ema_cross",
            "server_time": "2026-10-10T04:16:00.041612+00:00",
            "instrument_key": "NSE_INDEX|Nifty 50",
            "data": {
                "timestamp": "2026-10-10T09:45:00+05:30",
                "type": "bullish",
                "close": 22680.95,
                "ema_fast": 22681.7712,
                "ema_slow": 22681.5104,
            },
        },
        "error": {
            "type": "error",
            "error": "instrument_key not found: NSE_FO|99999",
        },
    }

    lifecycle = [
        {
            "step": 1,
            "who": "client",
            "action": (
                "Open WebSocket to /ws/live-ema/by-instrument/{url-encoded key} "
                "or /ws/live-ema/by-parts?underlying=..."
            ),
        },
        {
            "step": 2,
            "who": "server",
            "action": (
                "Accept the socket. If the instrument cannot be resolved, "
                "emit {type:'error'} and close with code 1008."
            ),
        },
        {
            "step": 3,
            "who": "server",
            "action": "Send a {type:'snapshot'} message with instrument + config.",
        },
        {
            "step": 4,
            "who": "server",
            "action": (
                "Send {type:'tick'} messages for every upstream LTP tick. "
                "Each tick includes the current forming (unclosed) candle."
            ),
        },
        {
            "step": 5,
            "who": "server",
            "action": (
                f"Every {interval}s, when a bucket closes, send "
                "{type:'candle_completed'} with the final OHLC + EMA values."
            ),
        },
        {
            "step": 6,
            "who": "server",
            "action": (
                "When a completed candle produces a new cross, send "
                "{type:'ema_cross'} immediately AFTER the matching "
                "candle_completed."
            ),
        },
        {
            "step": 7,
            "who": "client",
            "action": (
                "Send any text frame (or a periodic ping) to keep the socket "
                "alive through proxies. Payload is ignored by the server."
            ),
        },
        {
            "step": 8,
            "who": "either",
            "action": (
                "On disconnect, the server removes the client from the hub. "
                "There is no resume / replay — reconnect to get a fresh "
                "snapshot."
            ),
        },
    ]

    error_codes = [
        {
            "code": 1008,
            "when": (
                "Sent by the server when the requested instrument cannot be "
                "resolved. An accompanying {type:'error'} payload is sent "
                "before the close."
            ),
            "example_payload": {
                "type": "error",
                "error": "instrument_key not found: NSE_FO|99999",
            },
        },
        {
            "code": 1000,
            "when": "Normal closure — the server or client closed the socket.",
            "example_payload": None,
        },
        {
            "code": 1001,
            "when": "Server going away (application shutdown).",
            "example_payload": None,
        },
        {
            "code": 1006,
            "when": (
                "Abnormal closure — no close frame received. Usually a "
                "network drop or process kill. The client should reconnect."
            ),
            "example_payload": None,
        },
    ]

    return {
        "overview": (
            "Live EMA WebSocket channel. One socket == one instrument. "
            "Ticks arrive as upstream LTP ticks; EMA is recomputed on every "
            "completed candle. Crosses are only ever emitted on completed "
            "candles — a partial candle can never produce an alert. The "
            "channel is a broadcast: the server ignores any payload the "
            "client sends."
        ),
        "backend": backend,
        "interval_seconds": interval,
        "config": {
            "use_live_ltp_feed": settings.use_live_ltp_feed,
            "interval_seconds": settings.live_ema_interval_seconds,
            "ema_fast_period": settings.ema_fast_period,
            "ema_slow_period": settings.ema_slow_period,
            "market_timezone": settings.market_timezone,
            "market_open_time": settings.market_open_time,
            "market_close_time": settings.market_close_time,
        },
        "endpoints": endpoints,
        "message_types": message_types,
        "samples": samples,
        "sample_messages": sample_messages,
        "lifecycle": lifecycle,
        "error_codes": error_codes,
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