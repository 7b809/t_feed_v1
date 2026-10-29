# app/api/ws_catalog.py
"""
WebSocket discovery / documentation API.

  GET /api/v1/ws/catalog
  GET /api/v1/ws/catalog/{instrument_key}

Index URLs use ``meta["folder"]`` (nifty / banknifty / midcpnifty /
sensex) so multi-word underlyings like "NIFTY BANK" don't produce URLs
with spaces.
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
        "description": "Per-cycle status heartbeat.",
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

CLOSE_CODES: dict[int, dict[str, str]] = {
    1000: {"name": "Normal Closure", "reason": "Client disconnected normally."},
    1001: {"name": "Going Away", "reason": "Server shutting down."},
    1011: {"name": "Internal Error", "reason": "Unexpected server-side error."},
    4400: {"name": "Bad Request", "reason": "Invalid options contract format."},
    4404: {"name": "Not Found", "reason": "Instrument not found in catalog."},
}

CLIENT_BEHAVIOUR: dict[str, Any] = {
    "keepalive": {
        "description": "Send any text frame as a keepalive ping.",
        "recommended_interval_seconds": 30,
        "example_payload": "ping",
    },
    "reconnect": {
        "description": "Reconnect with exponential backoff (1s → 30s capped).",
        "on_4400": "Fix the contract format; do not retry.",
        "on_4404": "Instrument unavailable; refresh or pick another; do not retry.",
        "on_other_close": "Retry with backoff.",
    },
    "message_sequence": [
        "snapshot (always)",
        "last_cross (only when a cross exists)",
        "live: heartbeat / cross / no_cross",
    ],
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
}

AUTO_SUBSCRIBE: dict[str, Any] = {
    "description": (
        "Options and raw-instrument WebSocket endpoints auto-subscribe the "
        "target instrument on connect, provided it exists in "
        "all_instruments.json."
    ),
    "rest_prewarm": (
        "POST /api/v1/ema/subscribe/{instrument_key} subscribes without "
        "holding a WebSocket."
    ),
    "catalog_refresh": (
        "POST /api/v1/jobs/mega-archive/hard-refresh "
        "(X-API-Key required when API_KEY is set)."
    ),
}


def _now_ist_iso() -> str:
    return datetime.now(IST).isoformat()


def _service(request: Request):
    svc = getattr(request.app.state, "ema_feed_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="EMA service unavailable")
    return svc


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
                "ws_urls": urls,
                "active_channels": channels,
                "client_counts": {
                    ch: int(client_counts.get(ch, 0)) for ch in channels
                },
                "current_state": _compact_state(snap),
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

    how_to_connect_new_instrument = {
        "step_1_find": {
            "description": "Search the catalog for the instrument you want.",
            "example": (
                f"{http_base}/api/v1/ws/catalog"
                f"?underlying=NIFTY&strike_type=CE&available_limit=10"
            ),
        },
        "step_2_connect": {
            "description": (
                "Open a WebSocket to one of the URLs in ws_urls. Options "
                "and raw-instrument endpoints auto-subscribe on connect."
            ),
            "example": f"{base}/ws/ema/options/nifty/22500CE",
        },
        "step_3_verify": {
            "description": (
                "First message is a snapshot. Subsequent cycles emit "
                "heartbeat / cross / no_cross."
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
        "channel_patterns": patterns,
        "event_types": EVENT_TYPES,
        "close_codes": CLOSE_CODES,
        "client_behaviour": CLIENT_BEHAVIOUR,
        "client_examples": CLIENT_EXAMPLES,
        "auto_subscribe": AUTO_SUBSCRIBE,
        "how_to_connect_new_instrument": how_to_connect_new_instrument,
        "subscribed_instruments": subscribed_instruments,
        "available_instruments": available,
        "single_instrument_lookup": {
            "url_template": "/api/v1/ws/catalog/{instrument_key}",
            "example": (
                f"{http_base}/api/v1/ws/catalog/"
                f"{quote('NSE_FO|51330', safe='')}"
            ),
            "note": "URL-encode '|' as %7C and spaces as %20.",
        },
    }


@router.get("/api/v1/ws/catalog/{instrument_key:path}")
def ws_catalog_instrument(
    request: Request,
    instrument_key: str,
) -> dict[str, Any]:
    svc = _service(request)
    base = _ws_base(request)
    broadcaster = svc.broadcaster
    client_counts = broadcaster.channel_counts()

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
        "ws_urls": urls,
        "recommended_url": best_url,
        "active_channels": channels,
        "client_counts": {
            ch: int(client_counts.get(ch, 0)) for ch in channels
        },
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
    response["messages_you_will_receive"] = list(EVENT_TYPES.keys())
    response["event_types"] = EVENT_TYPES
    response["close_codes"] = CLOSE_CODES
    response["client_examples"] = CLIENT_EXAMPLES
    return response