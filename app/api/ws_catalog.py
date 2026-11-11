# app/api/ws_catalog.py
"""
WebSocket discovery / documentation API.

  GET /api/v1/ws/catalog
  GET /api/v1/ws/catalog/{instrument_key}

Covers two live WebSocket surfaces:

  * EMA  — /ws/index/{underlying}
           /ws/ema/options/{underlying}/{contract}
           /ws/ema/{instrument_key}

  * SR   — /ws/sr/{instrument_key}?config=<left_bars>/<right_bars>
           /ws/sr/index/{underlying}?config=<left_bars>/<right_bars>

Index URLs use ``meta["folder"]`` (nifty / banknifty / midcpnifty /
sensex) so multi-word underlyings like "NIFTY BANK" don't produce URLs
with spaces.

SR config format is "<left_bars>/<right_bars>":

  * ``left_bars``  — how many bars to the LEFT of the pivot to compare against
  * ``right_bars`` — how many bars to the RIGHT to confirm the pivot
                     (confirmation delay; small = reactive, large = strong)

Canonical defaults: 15/3, 15/15, 15/30 (see SR_DEFAULT_CONFIGS). The SR
service can be queried for its canonical list at runtime; if it is not
registered (e.g. SR_ENABLED=false) the response falls back to the static
canonical list so documentation is always complete.

Today-only SR policy
--------------------
Every SR WebSocket connection begins with a ``connection_ack`` frame,
followed by one ``snapshot`` per config. All SR payloads (snapshot,
sr_update, heartbeat) carry only pivots whose ``pivot_time`` is today
(IST), tagged with ``"data_scope": "today_only"``. Clients that cache
SR data should drop anything tagged differently.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.config import INDEX_METADATA

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ws-catalog"])

IST = timezone(timedelta(hours=5, minutes=30))

# Fallback when the SR service is unavailable — matches
# app/services/sr_calculator.DEFAULT_CONFIGS.
SR_DEFAULT_CONFIGS_FALLBACK: tuple[str, ...] = ("15/3", "15/15", "15/30")

SR_SCOPE_NOTE = "For further reference, only today's SR levels are used"

# --------------------------------------------------------------------------- #
# EMA channel patterns (unchanged contract)
# --------------------------------------------------------------------------- #
CHANNEL_PATTERNS: dict[str, dict[str, Any]] = {
    "index": {
        "url_template": "/ws/index/{underlying}",
        "description": (
            "Live EMA(9/21) feed for an index. The underlying is lowercased."
        ),
        "example_path": "/ws/index/nifty",
        "known_values": {
            "nifty": "NSE_INDEX|Nifty 50",
            "banknifty": "NSE_INDEX|Nifty Bank",
            "midcpnifty": "NSE_INDEX|NIFTY MID SELECT",
            "sensex": "BSE_INDEX|SENSEX",
        },
        "auto_subscribe": (
            "Indexes are seeded at bootstrap. If missing from "
            "all_instruments.json, INDEX_METADATA is used so the "
            "connection always succeeds."
        ),
    },
    "options": {
        "url_template": "/ws/ema/options/{underlying}/{contract}",
        "description": (
            "Live EMA feed for one option contract. Underlying is "
            "lowercased; contract is '{strike}{CE|PE}' or '{strike}_{CE|PE}'."
        ),
        "example_path": "/ws/ema/options/nifty/22500CE",
        "contract_format": "{strike}{CE|PE}  e.g. 22500CE, 22500PE, 23000_CE",
        "auto_subscribe": (
            "If the contract exists in all_instruments.json but isn't "
            "subscribed yet, the server subscribes it on connect. If the "
            "contract is not in the catalog, the socket is closed with "
            "code 4404."
        ),
    },
    "instrument": {
        "url_template": "/ws/ema/{instrument_key}",
        "description": (
            "Live EMA feed for any instrument by its raw key. "
            "URL-encode '|' as %7C and spaces as %20."
        ),
        "example_path": "/ws/ema/NSE_FO%7C51330",
        "auto_subscribe": (
            "If the instrument exists in all_instruments.json but isn't "
            "subscribed yet, the server subscribes it on connect. If the "
            "instrument is not in the catalog, the socket is closed with "
            "code 4404."
        ),
    },
}

# --------------------------------------------------------------------------- #
# SR channel patterns
# --------------------------------------------------------------------------- #
SR_CHANNEL_PATTERNS: dict[str, dict[str, Any]] = {
    "sr": {
        "url_template": "/ws/sr/{instrument_key}?config={left_bars}/{right_bars}",
        "description": (
            "Live SR (support/resistance) levels for one instrument and one "
            "config. Config format: '<left_bars>/<right_bars>'. "
            "Data is TODAY-ONLY (IST)."
        ),
        "example_path": "/ws/sr/NSE_INDEX%7CNifty%2050?config=15/3",
        "config_format": "<left_bars>/<right_bars>  e.g. 15/3, 15/15, 15/30",
        "config_notes": {
            "left_bars": "bars to the LEFT of the pivot to compare against",
            "right_bars": (
                "bars to the RIGHT to confirm the pivot (confirmation delay); "
                "small = reactive, large = strong"
            ),
        },
        "config_all": (
            "?config=all subscribes to all default configs at once and "
            "replays one snapshot frame per config."
        ),
        "known_configs": list(SR_DEFAULT_CONFIGS_FALLBACK),
        "auto_subscribe": (
            "The (instrument, config) pair is auto-subscribed on connect. "
            "If neither the instrument nor an index alias resolves, the "
            "socket is closed with code 4404."
        ),
        "data_scope": "today_only",
        "scope_note": SR_SCOPE_NOTE,
        "initial_handshake": [
            "connection_ack  (once, before any snapshot)",
            "snapshot        (one frame per config, today-only)",
            "heartbeat / sr_update (live, today-only)",
        ],
    },
    "sr_index": {
        "url_template": "/ws/sr/index/{underlying}?config={left_bars}/{right_bars}",
        "description": (
            "Index alias for the SR WebSocket. Underlying is lowercased "
            "(nifty / banknifty / midcpnifty / sensex). Data is today-only."
        ),
        "example_path": "/ws/sr/index/nifty?config=15/3",
        "known_values": {
            "nifty": "NSE_INDEX|Nifty 50",
            "banknifty": "NSE_INDEX|Nifty Bank",
            "midcpnifty": "NSE_INDEX|NIFTY MID SELECT",
            "sensex": "BSE_INDEX|SENSEX",
        },
    },
}

# --------------------------------------------------------------------------- #
# EMA event types
# --------------------------------------------------------------------------- #
EVENT_TYPES: dict[str, dict[str, Any]] = {
    "snapshot": {
        "description": "Full runtime state, once per connection.",
        "when": "once per connection",
        "sample": {
            "event": "snapshot",
            "instrument_key": "NSE_INDEX|Nifty 50",
            "meta": {
                "instrument_key": "NSE_INDEX|Nifty 50",
                "trading_symbol": "NIFTY",
                "underlying": "NIFTY",
                "strike_price": None,
                "strike_type": "INDEX",
                "expiry": None,
                "canonical": "NIFTY",
                "folder": "nifty",
                "priority": 1,
            },
            "ema_fast": 22497.41,
            "ema_slow": 22495.78,
            "last_ts": "2026-10-23T15:13:00+05:30",
            "last_close": 22498.15,
            "ema_fast_prev": 22497.12,
            "ema_slow_prev": 22495.6,
            "last_ts_prev": "2026-10-23T15:12:00+05:30",
            "last_close_prev": 22497.85,
            "prev_snapshot_time": "2026-10-23T15:13:10+05:30",
            "cycle_time": "2026-10-23T15:14:10+05:30",
            "cycle_number": 42,
            "candle_count": 3000,
            "cross_count": 130,
            "cross_count_today": 12,
            "today_ist_date": "2026-10-23",
            "last_cross": {
                "timestamp": "2026-10-23T15:13:00+05:30",
                "type": "bullish",
                "fast_ema": 22497.41,
                "slow_ema": 22495.78,
                "close": 22498.15,
                "volume": 0.0,
                "volume_diff": 0.0,
                "ema_diff": 1.63,
                "score": 0,
                "max_score": 0,
                "valid": True,
                "filters": {},
            },
            "last_cross_time": "2026-10-23T15:13:00+05:30",
            "last_payload": None,
        },
    },
    "last_cross": {
        "description": "Replay of last known cross (conditional).",
        "when": "once per connection (conditional)",
        "sample": {
            "event": "last_cross",
            "instrument_key": "NSE_INDEX|Nifty 50",
            "trading_symbol": "NIFTY",
            "close": 22498.15,
            "ema9": 22497.41,
            "ema21": 22495.78,
            "last_cross_time": "2026-10-23T15:13:00+05:30",
        },
    },
    "cross": {
        "description": "Live EMA(9/21) crossover.",
        "when": "on each detected crossover",
        "sample": {
            "event": "cross",
            "instrument_key": "NSE_FO|51330",
            "trading_symbol": "NIFTY 22500 CE 27 OCT 26",
            "close": 190.0,
            "ema9": 189.24,
            "ema21": 182.99,
            "last_cross_time": "2026-10-23T15:12:00+05:30",
        },
    },
    "no_cross": {
        "description": "Cycle with no crossover.",
        "when": "on each cycle with no crossover",
        "sample": {
            "event": "no_cross",
            "instrument_key": "NSE_FO|51330",
            "trading_symbol": "NIFTY 22500 CE 27 OCT 26",
            "last_cross_time": "2026-10-23T15:12:00+05:30",
        },
    },
    "heartbeat": {
        "description": "Per-cycle status heartbeat (EMA).",
        "when": "once per cycle (default) or every Nth cycle",
        "sample": {
            "event": "heartbeat",
            "cycle": 42,
            "cycle_started_at": "2026-10-23T15:14:10+05:30",
            "cycle_completed_at": "2026-10-23T15:14:11+05:30",
            "duration_seconds": 0.847,
            "instrument_key": "NSE_INDEX|Nifty 50",
            "trading_symbol": "NIFTY",
            "status": "ok",
            "error": None,
            "ema9": 22497.41,
            "ema21": 22495.78,
            "last_close": 22498.15,
            "last_ts": "2026-10-23T15:13:00+05:30",
            "last_cross_time": "2026-10-23T15:13:00+05:30",
        },
    },
}

# --------------------------------------------------------------------------- #
# SR level schema (shared between snapshot / sr_update / sr_current_snapshots)
# --------------------------------------------------------------------------- #
SR_LEVEL_SCHEMA: dict[str, Any] = {
    "description": (
        "Every entry in `resistances` and `supports` (and every element of "
        "`sr_current_snapshots[cfg].resistances / .supports` from "
        "/api/v1/ws/catalog) carries this shape. Note: only levels whose "
        "`pivot_time` is today (IST) are returned."
    ),
    "fields": {
        "level": "'support' or 'resistance'",
        "price": "pivot high (resistance) or pivot low (support), rounded to 4 dp",
        "pivot_bar_index": "index of the pivot bar in the candle list",
        "pivot_time": "ISO timestamp (+05:30) of the pivot bar",
        "confirmed_bar_index": "pivot_bar_index + right_bars",
        "confirmed_time": "ISO timestamp (+05:30) of the confirmation bar",
        "confirmed_time_price": "close of the confirmation bar",
        "left_bars": "left window used (matches the config)",
        "right_bars": "right (confirmation) window used (matches the config)",
    },
    "sample": {
        "level": "support",
        "price": 43.25,
        "pivot_bar_index": 445,
        "pivot_time": "2026-11-09T09:15:00+05:30",
        "confirmed_bar_index": 448,
        "confirmed_time": "2026-11-09T09:18:00+05:30",
        "confirmed_time_price": 87.85,
        "left_bars": 15,
        "right_bars": 3,
    },
}

# --------------------------------------------------------------------------- #
# SR event types
# --------------------------------------------------------------------------- #
SR_EVENT_TYPES: dict[str, dict[str, Any]] = {
    "connection_ack": {
        "description": (
            "Emitted ONCE per WebSocket connection, immediately after the "
            "socket is accepted and BEFORE any snapshot. Confirms the feed "
            "is live and declares the today-only SR policy."
        ),
        "when": "once per connection, first frame",
        "sample": {
            "event": "connection_ack",
            "status": "connected",
            "message": "Feed connected successfully",
            "instrument_key": "NSE_INDEX|Nifty 50",
            "configs": ["15/3", "15/15", "15/30"],
            "data_scope": "today_only",
            "scope_note": SR_SCOPE_NOTE,
        },
    },
    "sr_snapshot": {
        "description": (
            "Initial SR levels for one (instrument, config). One frame per "
            "config on connect. Contains ONLY pivots whose pivot_time is "
            "today (IST). Every entry in `resistances` and `supports` "
            "follows the SR level schema."
        ),
        "when": "once per connection per config",
        "level_schema": "sr_level_schema",
        "sample": {
            "event": "snapshot",
            "instrument_key": "NSE_INDEX|Nifty 50",
            "config": "15/3",
            "left_bars": 15,
            "right_bars": 3,
            "current_price": 22512.6,
            "last_ts": "2026-11-09T15:29:00+05:30",
            "cycle_number": 42,
            "cycle_time": "2026-11-09T15:29:10+05:30",
            "candle_count": 3375,
            # ---- today-only markers ----
            "feed_status": "connected",
            "feed_message": "Feed connected successfully",
            "data_scope": "today_only",
            "today_ist_date": "2026-11-09",
            "scope_note": SR_SCOPE_NOTE,
            "resistances": [
                {
                    "level": "resistance",
                    "price": 22540.0,
                    "pivot_bar_index": 3358,
                    "pivot_time": "2026-11-09T14:13:00+05:30",
                    "confirmed_bar_index": 3361,
                    "confirmed_time": "2026-11-09T14:16:00+05:30",
                    "confirmed_time_price": 22530.0,
                    "left_bars": 15,
                    "right_bars": 3,
                }
            ],
            "supports": [
                {
                    "level": "support",
                    "price": 22480.5,
                    "pivot_bar_index": 3350,
                    "pivot_time": "2026-11-09T14:05:00+05:30",
                    "confirmed_bar_index": 3353,
                    "confirmed_time": "2026-11-09T14:08:00+05:30",
                    "confirmed_time_price": 22495.0,
                    "left_bars": 15,
                    "right_bars": 3,
                }
            ],
        },
    },
    "sr_update": {
        "description": (
            "SR levels changed for one (instrument, config) — at least one "
            "resistance or support identity (level / price / pivot_bar_index) "
            "differs from the previous cycle. Only today's pivots are "
            "included. Every entry follows the SR level schema."
        ),
        "when": "on any change to the today-only resistance/support lists",
        "level_schema": "sr_level_schema",
        "sample": {
            "event": "sr_update",
            "instrument_key": "NSE_INDEX|Nifty 50",
            "trading_symbol": "NIFTY",
            "config": "15/3",
            "current_price": 22512.6,
            "last_ts": "2026-11-09T15:29:00+05:30",
            "data_scope": "today_only",
            "today_ist_date": "2026-11-09",
            "resistances": [
                {
                    "level": "resistance",
                    "price": 22540.0,
                    "pivot_bar_index": 3358,
                    "pivot_time": "2026-11-09T14:13:00+05:30",
                    "confirmed_bar_index": 3361,
                    "confirmed_time": "2026-11-09T14:16:00+05:30",
                    "confirmed_time_price": 22530.0,
                    "left_bars": 15,
                    "right_bars": 3,
                }
            ],
            "supports": [
                {
                    "level": "support",
                    "price": 22480.5,
                    "pivot_bar_index": 3350,
                    "pivot_time": "2026-11-09T14:05:00+05:30",
                    "confirmed_bar_index": 3353,
                    "confirmed_time": "2026-11-09T14:08:00+05:30",
                    "confirmed_time_price": 22495.0,
                    "left_bars": 15,
                    "right_bars": 3,
                }
            ],
        },
    },
    "sr_heartbeat": {
        "description": (
            "Per-cycle SR heartbeat for one (instrument, config). Counts and "
            "nearest levels are derived from today's pivots only."
        ),
        "when": "every SR_HEARTBEAT_CYCLES cycles per config",
        "sample": {
            "event": "heartbeat",
            "instrument_key": "NSE_INDEX|Nifty 50",
            "trading_symbol": "NIFTY",
            "config": "15/3",
            "cycle": 42,
            "cycle_time": "2026-11-09T15:29:10+05:30",
            "current_price": 22512.6,
            "last_ts": "2026-11-09T15:29:00+05:30",
            "nearest_resistance": 22540.0,
            "nearest_support": 22480.5,
            "status": "ok",
            "data_scope": "today_only",
        },
    },
}

CLOSE_CODES: dict[int, dict[str, str]] = {
    1000: {"name": "Normal Closure", "reason": "Client disconnected normally."},
    1001: {"name": "Going Away", "reason": "Server shutting down."},
    1011: {"name": "Internal Error", "reason": "Unexpected server-side error."},
    4400: {
        "name": "Bad Request",
        "reason": "Invalid options contract format, SR config, or missing ?config=.",
    },
    4404: {
        "name": "Not Found",
        "reason": "Instrument not found in catalog / SR auto-subscribe failed.",
    },
}

CLIENT_BEHAVIOUR: dict[str, Any] = {
    "keepalive": {
        "description": "Send any text frame as a keepalive ping.",
        "recommended_interval_seconds": 30,
        "example_payload": "ping",
    },
    "reconnect": {
        "description": "Reconnect with exponential backoff (1s → 30s capped).",
        "on_4400": "Fix the contract format / SR config; do not retry.",
        "on_4404": "Instrument unavailable; refresh or pick another; do not retry.",
        "on_other_close": "Retry with backoff.",
    },
    "ema_message_sequence": [
        "snapshot (always)",
        "last_cross (only when a cross exists)",
        "live: heartbeat / cross / no_cross",
    ],
    "sr_message_sequence": [
        "connection_ack (always, first frame)",
        "snapshot (one frame per config, today-only)",
        "live: heartbeat / sr_update (per config, today-only)",
    ],
    "sr_data_scope": {
        "description": (
            "Every SR payload (snapshot / sr_update / heartbeat) carries "
            "`data_scope: \"today_only\"` and only includes pivots whose "
            "pivot_time is today (IST). Clients should drop any SR cache "
            "entry whose today_ist_date is not the current IST date."
        ),
        "scope_note": SR_SCOPE_NOTE,
    },
}

CLIENT_EXAMPLES: dict[str, str] = {
    "python": (
        "import asyncio\n"
        "import websockets\n"
        "\n"
        "async def main():\n"
        "    uri = 'ws://<host>:<port>/ws/index/nifty'\n"
        "    async with websockets.connect(uri) as ws:\n"
        "        async for msg in ws:\n"
        "            print(msg)\n"
        "\n"
        "asyncio.run(main())"
    ),
    "javascript": (
        "const ws = new WebSocket('ws://<host>:<port>/ws/index/nifty');\n"
        "ws.onmessage = (evt) => console.log(JSON.parse(evt.data));\n"
        "setInterval(() => {\n"
        "  if (ws.readyState === WebSocket.OPEN) ws.send('ping');\n"
        "}, 30000);"
    ),
    "cli": (
        "# websocat: https://github.com/vi/websocat\n"
        "websocat ws://<host>:<port>/ws/index/nifty"
    ),
    "sr_python": (
        "import asyncio\n"
        "import websockets\n"
        "\n"
        "async def main():\n"
        "    uri = 'ws://<host>:<port>/ws/sr/index/nifty?config=15/3'\n"
        "    async with websockets.connect(uri) as ws:\n"
        "        async for msg in ws:\n"
        "            print(msg)\n"
        "\n"
        "asyncio.run(main())"
    ),
    "sr_javascript": (
        "const ws = new WebSocket(\n"
        "  'ws://<host>:<port>/ws/sr/index/nifty?config=15/3');\n"
        "ws.onmessage = (evt) => console.log(JSON.parse(evt.data));\n"
        "setInterval(() => {\n"
        "  if (ws.readyState === WebSocket.OPEN) ws.send('ping');\n"
        "}, 30000);"
    ),
    "sr_cli": (
        "# websocat: https://github.com/vi/websocat\n"
        "websocat 'ws://<host>:<port>/ws/sr/index/nifty?config=15/3'"
    ),
}

AUTO_SUBSCRIBE: dict[str, Any] = {
    "ema": {
        "description": (
            "EMA options and raw-instrument endpoints auto-subscribe the "
            "target instrument on connect, provided it exists in "
            "all_instruments.json."
        ),
        "rest_prewarm": (
            "POST /api/v1/ema/subscribe/{instrument_key} subscribes without "
            "holding a WebSocket."
        ),
    },
    "sr": {
        "description": (
            "SR endpoints auto-subscribe the (instrument, config) pair on "
            "connect. ?config=all subscribes to every default config."
        ),
        "rest_prewarm": (
            "POST /api/v1/sr/subscribe/{instrument_key}?config=15/3 "
            "(use ?config=all to subscribe all defaults)."
        ),
    },
    "catalog_refresh": (
        "POST /api/v1/jobs/mega-archive/hard-refresh "
        "(X-API-Key required when API_KEY is set)."
    ),
}

SR_REST_ENDPOINTS: dict[str, str] = {
    "configs": "GET  /api/v1/sr/configs",
    "status": "GET  /api/v1/sr/status",
    "latest": "GET  /api/v1/sr/{instrument_key}/latest?config=15/3  (or ?config=all)",
    "subscribe": "POST /api/v1/sr/subscribe/{instrument_key}?config=15/3  (or ?config=all)",
    "unsubscribe": "POST /api/v1/sr/unsubscribe/{instrument_key}?config=15/3  (or ?config=all)",
}

# --------------------------------------------------------------------------- #
# Service accessors
# --------------------------------------------------------------------------- #
def _now_ist_iso() -> str:
    return datetime.now(IST).isoformat()

def _service(request: Request):
    svc = getattr(request.app.state, "ema_feed_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="EMA service unavailable")
    return svc

def _sr_service(request: Request):
    """Return the SR service if registered, otherwise None (SR_ENABLED=false)."""
    return getattr(request.app.state, "sr_feed_service", None)

def _sr_default_configs(request: Request) -> list[str]:
    svc = _sr_service(request)
    if svc is not None:
        try:
            configs = list(svc.default_configs())
            if configs:
                return configs
        except Exception:  # noqa: BLE001
            logger.debug("SR default_configs() raised; using fallback", exc_info=True)
    return list(SR_DEFAULT_CONFIGS_FALLBACK)

def _resolve_proto_host(request: Request) -> tuple[str, str]:
    proto = (
        request.headers.get("x-forwarded-proto") or request.url.scheme
    ).split(",")[0].strip().lower()
    host = (
        request.headers.get("x-forwarded-host") or request.url.netloc
    ).split(",")[0].strip()
    return proto, host

def _ws_base(request: Request) -> str:
    proto, host = _resolve_proto_host(request)
    scheme = "wss" if proto == "https" else "ws"
    return f"{scheme}://{host}"

def _http_base(request: Request) -> str:
    proto, host = _resolve_proto_host(request)
    scheme = "https" if proto == "https" else "http"
    return f"{scheme}://{host}"

def _format_strike(strike: Any) -> str:
    try:
        n = float(strike)
    except (TypeError, ValueError):
        return str(strike)
    if n.is_integer():
        return str(int(n))
    return str(n)

def _index_tag(meta: dict[str, Any]) -> str:
    """Folder → canonical → underlying.lower() for index URLs / channels."""
    tag = (
        meta.get("folder")
        or meta.get("canonical")
        or (meta.get("underlying") or "")
    )
    return (tag or "").strip().lower()

# --------------------------------------------------------------------------- #
# EMA URL / channel builders
# --------------------------------------------------------------------------- #
def _ws_urls_for(meta: dict[str, Any], base: str) -> dict[str, str]:
    key = meta.get("instrument_key") or ""
    underlying = (meta.get("underlying") or "").lower()
    strike_type = (meta.get("strike_type") or "").upper()
    strike_price = meta.get("strike_price")
    urls: dict[str, str] = {}

    if key:
        urls["instrument"] = f"{base}/ws/ema/{quote(key, safe='')}"

    if strike_type == "INDEX" or not strike_type:
        tag = _index_tag(meta)
        if tag:
            urls["index"] = f"{base}/ws/index/{tag}"
    else:
        if underlying and strike_price is not None:
            urls["options"] = (
                f"{base}/ws/ema/options/{underlying}/"
                f"{_format_strike(strike_price)}{strike_type}"
            )
    return urls

def _channels_for(meta: dict[str, Any]) -> list[str]:
    key = meta.get("instrument_key") or ""
    underlying = (meta.get("underlying") or "").lower()
    strike_type = (meta.get("strike_type") or "").upper()
    channels: list[str] = []
    if key:
        channels.append(f"instrument:{key}")
    if strike_type == "INDEX" or not strike_type:
        tag = _index_tag(meta)
        if tag:
            channels.append(f"index:{tag}")
    elif underlying and meta.get("strike_price") is not None:
        channels.append(
            f"options:{underlying}:"
            f"{_format_strike(meta['strike_price'])}{strike_type}"
        )
    return channels

def _compact_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "ema9": snapshot.get("ema_fast"),
        "ema21": snapshot.get("ema_slow"),
        "last_close": snapshot.get("last_close"),
        "last_ts": snapshot.get("last_ts"),
        "cross_count": snapshot.get("cross_count"),
        "cross_count_today": snapshot.get("cross_count_today"),
        "cycle_number": snapshot.get("cycle_number"),
    }

def _best_url(urls: dict[str, str]) -> str:
    return (
        urls.get("options")
        or urls.get("index")
        or urls.get("instrument")
        or ""
    )

# --------------------------------------------------------------------------- #
# SR URL / channel builders
# --------------------------------------------------------------------------- #
def _sr_url_for(meta: dict[str, Any], base: str, config: str) -> str:
    key = meta.get("instrument_key") or ""
    if not key:
        return ""
    # Keep '/' in the query value readable (servers accept it unencoded).
    return f"{base}/ws/sr/{quote(key, safe='')}?config={config}"

def _sr_urls_for(
    meta: dict[str, Any],
    base: str,
    configs: list[str],
) -> dict[str, str]:
    """Return {'config:<left_bars>/<right_bars>': url, ..., 'all': url}."""
    urls: dict[str, str] = {}
    for cfg in configs:
        url = _sr_url_for(meta, base, cfg)
        if url:
            urls[f"config:{cfg}"] = url
    key = meta.get("instrument_key") or ""
    if key:
        urls["all"] = f"{base}/ws/sr/{quote(key, safe='')}?config=all"
    return urls

def _sr_channels_for(meta: dict[str, Any], configs: list[str]) -> list[str]:
    key = meta.get("instrument_key") or ""
    if not key:
        return []
    return [f"sr:{key}:{cfg}" for cfg in configs]

def _best_sr_url(meta: dict[str, Any], base: str, configs: list[str]) -> str:
    if not configs:
        return ""
    return _sr_url_for(meta, base, configs[0])

def _sr_compact_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    res = snapshot.get("resistances") or []
    sup = snapshot.get("supports") or []
    return {
        "current_price": snapshot.get("current_price"),
        "last_ts": snapshot.get("last_ts"),
        "nearest_resistance": res[0]["price"] if res else None,
        "nearest_support": sup[0]["price"] if sup else None,
        "resistance_count": len(res),
        "support_count": len(sup),
        "data_scope": snapshot.get("data_scope", "today_only"),
        "today_ist_date": snapshot.get("today_ist_date"),
    }

def _sr_current_snapshots(
    request: Request,
    meta: dict[str, Any],
    configs: list[str],
) -> dict[str, dict[str, Any]]:
    """Snapshot of all known SR states for an instrument (best-effort)."""
    svc = _sr_service(request)
    if svc is None:
        return {}
    key = meta.get("instrument_key") or ""
    if not key:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for cfg in configs:
        try:
            snap = svc.snapshot(key, cfg)
        except Exception:  # noqa: BLE001
            snap = None
        if snap is not None:
            out[cfg] = _sr_compact_state(snap)
    return out

def _sr_status_safe(request: Request) -> dict[str, Any]:
    svc = _sr_service(request)
    if svc is None:
        return {"available": False}
    try:
        data = svc.status()
        return {"available": True, **data}
    except Exception:  # noqa: BLE001
        logger.exception("SR status() failed")
        return {"available": False, "error": "status() raised"}

# --------------------------------------------------------------------------- #
# GET /api/v1/ws/catalog
# --------------------------------------------------------------------------- #
@router.get("/api/v1/ws/catalog")
def ws_catalog(
    request: Request,
    include_available: bool = Query(True),
    available_limit: int = Query(20, ge=0, le=500),
    available_offset: int = Query(0, ge=0),
    underlying: str | None = Query(None),
    strike_type: str | None = Query(None),
) -> dict[str, Any]:
    svc = _service(request)
    base = _ws_base(request)
    http_base = _http_base(request)
    broadcaster = svc.broadcaster
    client_counts = broadcaster.channel_counts()

    sr_configs = _sr_default_configs(request)
    sr_service_available = _sr_service(request) is not None

    subscribed_keys = svc.subscribed_keys()

    subscribed_instruments: list[dict[str, Any]] = []
    option_underlyings: set[str] = set()

    for key in subscribed_keys:
        snap = svc.snapshot(key)
        if snap is None:
            continue
        meta = dict(snap.get("meta") or {})
        meta["instrument_key"] = key
        urls = _ws_urls_for(meta, base)
        channels = _channels_for(meta)
        sr_urls = _sr_urls_for(meta, base, sr_configs)
        sr_channels = _sr_channels_for(meta, sr_configs)
        st = (meta.get("strike_type") or "").upper()
        if st in {"CE", "PE"} and meta.get("underlying"):
            option_underlyings.add(str(meta["underlying"]).upper())

        subscribed_instruments.append(
            {
                "instrument_key": key,
                "trading_symbol": meta.get("trading_symbol"),
                "underlying": meta.get("underlying"),
                "strike_price": meta.get("strike_price"),
                "strike_type": meta.get("strike_type"),
                "expiry": meta.get("expiry"),
                "canonical": meta.get("canonical"),
                "folder": meta.get("folder"),
                # EMA surface
                "ws_urls": urls,
                "active_channels": channels,
                "client_counts": {
                    ch: int(client_counts.get(ch, 0)) for ch in channels
                },
                "current_state": _compact_state(snap),
                # SR surface
                "sr_ws_urls": sr_urls,
                "sr_active_channels": sr_channels,
                "sr_client_counts": {
                    ch: int(client_counts.get(ch, 0)) for ch in sr_channels
                },
                "sr_current_snapshots": _sr_current_snapshots(
                    request, meta, sr_configs
                ),
                "sr_data_scope": "today_only",
            }
        )

    available: dict[str, Any] | None = None
    if include_available:
        underlying_filter = (underlying or "").strip().upper() or None
        strike_type_filter = (strike_type or "").strip().upper() or None
        subscribed_set = set(subscribed_keys)

        try:
            catalog = svc.instruments.all()
        except Exception:  # noqa: BLE001
            logger.exception("Could not read instruments catalog")
            catalog = {}

        matched = 0
        page_items: list[dict[str, Any]] = []
        has_more = False

        for key, meta in catalog.items():
            if key in subscribed_set:
                continue
            if underlying_filter and (
                (meta.get("underlying") or "").upper() != underlying_filter
            ):
                continue
            if strike_type_filter and (
                (meta.get("strike_type") or "").upper() != strike_type_filter
            ):
                continue
            matched += 1
            if matched <= available_offset:
                continue
            if len(page_items) >= available_limit:
                has_more = True
                continue

            m = dict(meta)
            m["instrument_key"] = key
            urls = _ws_urls_for(m, base)
            sr_urls = _sr_urls_for(m, base, sr_configs)

            page_items.append(
                {
                    "instrument_key": key,
                    "trading_symbol": meta.get("trading_symbol"),
                    "underlying": meta.get("underlying"),
                    "strike_price": meta.get("strike_price"),
                    "strike_type": meta.get("strike_type"),
                    "expiry": meta.get("expiry"),
                    "canonical": meta.get("canonical"),
                    "folder": meta.get("folder"),
                    "ws_urls": urls,
                    "recommended_url": _best_url(urls),
                    "sr_ws_urls": sr_urls,
                    "sr_recommended_url": _best_sr_url(m, base, sr_configs),
                }
            )

        available = {
            "total_matched": matched,
            "total_in_catalog": len(catalog),
            "filters": {
                "underlying": underlying_filter,
                "strike_type": strike_type_filter,
            },
            "returned": len(page_items),
            "offset": available_offset,
            "limit": available_limit,
            "has_more": has_more,
            "next_offset": (
                available_offset + len(page_items) if has_more else None
            ),
            "items": page_items,
        }

    # EMA channel pattern descriptions (unchanged shape).
    patterns: dict[str, dict[str, Any]] = {
        "index": {
            **{k: v for k, v in CHANNEL_PATTERNS["index"].items() if k != "example_path"},
            "example_url": f"{base}{CHANNEL_PATTERNS['index']['example_path']}",
            "available_indexes": [
                {
                    "canonical": m.get("canonical"),
                    "folder": m.get("folder"),
                    "instrument_key": k,
                    "priority": m.get("priority"),
                }
                for k, m in sorted(
                    INDEX_METADATA.items(),
                    key=lambda kv: int(kv[1].get("priority", 999)),
                )
            ],
        },
        "options": {
            **{k: v for k, v in CHANNEL_PATTERNS["options"].items() if k != "example_path"},
            "example_url": f"{base}{CHANNEL_PATTERNS['options']['example_path']}",
            "underlyings_seen": sorted(option_underlyings),
        },
        "instrument": {
            **{k: v for k, v in CHANNEL_PATTERNS["instrument"].items() if k != "example_path"},
            "example_url": f"{base}{CHANNEL_PATTERNS['instrument']['example_path']}",
        },
    }

    # SR channel pattern descriptions.
    sr_patterns: dict[str, dict[str, Any]] = {
        "sr": {
            **{k: v for k, v in SR_CHANNEL_PATTERNS["sr"].items() if k != "example_path"},
            "example_url": f"{base}{SR_CHANNEL_PATTERNS['sr']['example_path']}",
            "default_configs": list(sr_configs),
            "service_available": sr_service_available,
        },
        "sr_index": {
            **{k: v for k, v in SR_CHANNEL_PATTERNS["sr_index"].items() if k != "example_path"},
            "example_url": f"{base}{SR_CHANNEL_PATTERNS['sr_index']['example_path']}",
            "available_indexes": [
                {
                    "canonical": m.get("canonical"),
                    "folder": m.get("folder"),
                    "instrument_key": k,
                    "priority": m.get("priority"),
                }
                for k, m in sorted(
                    INDEX_METADATA.items(),
                    key=lambda kv: int(kv[1].get("priority", 999)),
                )
            ],
        },
    }

    how_to_connect_new_instrument = {
        "step_1_find": {
            "description": "Search the catalog for the instrument you want.",
            "example": (
                f"{http_base}/api/v1/ws/catalog"
                f"?underlying=NIFTY&strike_type=CE&available_limit=10"
            ),
        },
        "step_2_connect_ema": {
            "description": (
                "Open a WebSocket to one of the URLs in ws_urls. Options "
                "and raw-instrument endpoints auto-subscribe on connect."
            ),
            "example": f"{base}/ws/ema/options/nifty/22500CE",
        },
        "step_2_connect_sr": {
            "description": (
                "Open a WebSocket to one of the URLs in sr_ws_urls, or "
                "append ?config=<left_bars>/<right_bars> to "
                "/ws/sr/{instrument_key}. ?config=all subscribes every "
                "default config at once."
            ),
            "example": f"{base}/ws/sr/index/nifty?config=15/3",
        },
        "step_3_verify": {
            "description": (
                "EMA: first message is a snapshot; then heartbeat / cross / "
                "no_cross. SR: first message is connection_ack, then one "
                "snapshot per config (today-only SR levels), then "
                "heartbeat / sr_update. Every SR level in the snapshot / "
                "sr_update payloads follows sr_level_schema and carries "
                "data_scope=\"today_only\"."
            ),
        },
        "if_not_in_catalog": {
            "description": (
                "The instrument is not in all_instruments.json. Refresh the "
                "catalog by pulling a fresh archive from MongoDB."
            ),
            "example": (
                "POST /api/v1/jobs/mega-archive/hard-refresh "
                "(X-API-Key required when API_KEY is set)"
            ),
        },
    }

    return {
        "version": request.app.version,
        "generated_at": _now_ist_iso(),
        "base_url": base,
        "http_base_url": http_base,
        # EMA surface
        "channel_patterns": patterns,
        "event_types": EVENT_TYPES,
        "close_codes": CLOSE_CODES,
        "client_behaviour": CLIENT_BEHAVIOUR,
        "client_examples": CLIENT_EXAMPLES,
        "auto_subscribe": AUTO_SUBSCRIBE,
        "subscribed_instruments": subscribed_instruments,
        "available_instruments": available,
        # SR surface
        "sr_default_configs": list(sr_configs),
        "sr_channel_patterns": sr_patterns,
        "sr_event_types": SR_EVENT_TYPES,
        "sr_level_schema": SR_LEVEL_SCHEMA,
        "sr_rest_endpoints": SR_REST_ENDPOINTS,
        "sr_status": _sr_status_safe(request),
        # Shared
        "how_to_connect_new_instrument": how_to_connect_new_instrument,
        "single_instrument_lookup": {
            "url_template": "/api/v1/ws/catalog/{instrument_key}",
            "example": (
                f"{http_base}/api/v1/ws/catalog/"
                f"{quote('NSE_FO|51330', safe='')}"
            ),
            "note": "URL-encode '|' as %7C and spaces as %20.",
        },
    }

# --------------------------------------------------------------------------- #
# GET /api/v1/ws/catalog/{instrument_key}
# --------------------------------------------------------------------------- #
@router.get("/api/v1/ws/catalog/{instrument_key:path}")
def ws_catalog_instrument(
    request: Request,
    instrument_key: str,
) -> dict[str, Any]:
    svc = _service(request)
    base = _ws_base(request)
    broadcaster = svc.broadcaster
    client_counts = broadcaster.channel_counts()

    sr_configs = _sr_default_configs(request)
    sr_svc = _sr_service(request)

    snap = svc.snapshot(instrument_key)
    meta = svc.instruments.get(instrument_key)

    if snap is None and meta is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Instrument '{instrument_key}' is not in the catalog and "
                f"not currently subscribed."
            ),
        )

    is_subscribed = snap is not None
    if meta is None and snap is not None:
        meta = dict(snap.get("meta") or {})
    meta = dict(meta or {})
    meta["instrument_key"] = instrument_key

    urls = _ws_urls_for(meta, base)
    channels = _channels_for(meta)
    best_url = _best_url(urls)

    sr_urls = _sr_urls_for(meta, base, sr_configs)
    sr_channels = _sr_channels_for(meta, sr_configs)
    best_sr_url = _best_sr_url(meta, base, sr_configs)

    response: dict[str, Any] = {
        "instrument_key": instrument_key,
        "trading_symbol": meta.get("trading_symbol"),
        "underlying": meta.get("underlying"),
        "strike_price": meta.get("strike_price"),
        "strike_type": meta.get("strike_type"),
        "expiry": meta.get("expiry"),
        "canonical": meta.get("canonical"),
        "folder": meta.get("folder"),
        "is_subscribed": is_subscribed,
        # EMA surface
        "ws_urls": urls,
        "recommended_url": best_url,
        "active_channels": channels,
        "client_counts": {
            ch: int(client_counts.get(ch, 0)) for ch in channels
        },
        # SR surface
        "sr_ws_urls": sr_urls,
        "sr_recommended_url": best_sr_url,
        "sr_active_channels": sr_channels,
        "sr_client_counts": {
            ch: int(client_counts.get(ch, 0)) for ch in sr_channels
        },
        "sr_default_configs": list(sr_configs),
        "sr_service_available": sr_svc is not None,
        "sr_data_scope": "today_only",
        "sr_scope_note": SR_SCOPE_NOTE,
    }

    if is_subscribed and snap is not None:
        response["current_snapshot"] = snap
        last_cross = snap.get("last_cross")
        if last_cross:
            response["live_last_cross"] = {
                "event": "last_cross",
                "instrument_key": instrument_key,
                "trading_symbol": meta.get("trading_symbol"),
                "close": (
                    last_cross.get("close")
                    if last_cross.get("close") is not None
                    else last_cross.get("fast_ema")
                ),
                "ema9": last_cross.get("fast_ema"),
                "ema21": last_cross.get("slow_ema"),
                "last_cross_time": last_cross.get("timestamp"),
            }
    else:
        response["how_to_connect"] = (
            f"Open a WebSocket to {best_url}. The endpoint will "
            f"auto-subscribe this instrument on connect."
        )

    # SR snapshot detail (per config) — only when the SR service is available.
    if sr_svc is not None:
        sr_states: dict[str, dict[str, Any]] = {}
        for cfg in sr_configs:
            try:
                s = sr_svc.snapshot(instrument_key, cfg)
            except Exception:  # noqa: BLE001
                s = None
            if s is not None:
                sr_states[cfg] = s
        if sr_states:
            response["sr_current_snapshots"] = sr_states
        else:
            response["sr_how_to_connect"] = (
                f"Open a WebSocket to {best_sr_url}. The endpoint will "
                f"auto-subscribe this instrument for the requested config. "
                f"Data is today-only."
            )

    response["connect_request"] = {
        "method": "GET",
        "url": best_url,
        "headers": {
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Version": "13",
        },
        "auth": "none",
    }

    response["sr_connect_requests"] = {
        cfg: {
            "method": "GET",
            "url": sr_urls.get(f"config:{cfg}", ""),
            "headers": {
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Version": "13",
            },
            "auth": "none",
            "initial_frames": ["connection_ack", "snapshot"],
        }
        for cfg in sr_configs
        if sr_urls.get(f"config:{cfg}")
    }
    if sr_urls.get("all"):
        response["sr_connect_requests"]["all"] = {
            "method": "GET",
            "url": sr_urls["all"],
            "headers": {
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Version": "13",
            },
            "auth": "none",
            "note": "Subscribes every default config; one snapshot per config.",
            "initial_frames": ["connection_ack"] + ["snapshot"] * len(sr_configs),
        }

    # Preserve the existing EMA event contract exactly.
    response["messages_you_will_receive"] = list(EVENT_TYPES.keys())
    response["event_types"] = EVENT_TYPES
    response["sr_messages_you_will_receive"] = list(SR_EVENT_TYPES.keys())
    response["sr_event_types"] = SR_EVENT_TYPES
    response["sr_level_schema"] = SR_LEVEL_SCHEMA
    response["close_codes"] = CLOSE_CODES
    response["client_examples"] = CLIENT_EXAMPLES
    return response