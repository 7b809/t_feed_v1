"""Read-only API exposing the WebSocket message catalog.

The live-EMA WebSocket channel emits four message types
(``snapshot``, ``tick``, ``candle_completed``, ``ema_cross``, plus
``error``). Every subscriber to the same instrument gets the same
shapes. This module publishes concrete, realistic samples of those
shapes so clients, docs, Postman collections, and QA fixtures can be
generated from a single source of truth without reading server code.

Endpoints
---------
GET /api/ws-samples
    The full catalog: every category, every sample, every message
    payload. Includes a short ``overview`` and the resolved
    ``base_ws_url`` for the calling request.

GET /api/ws-samples/categories
    A lightweight index — category name, count, sample ids.

GET /api/ws-samples/main-index
    Index instruments only (Nifty 50, SENSEX, Bank Nifty, ...).

GET /api/ws-samples/options
    F&O instruments only (CE / PE / FUT).

GET /api/ws-samples/{category}
    Generic lookup by category name (``main-index`` or ``options``).
    Returns 404 for unknown categories.

GET /api/ws-samples/sample/{sample_id}
    A single sample by id (e.g. ``nifty-50-index``).

Notes
-----
* Values inside the sample payloads are illustrative. They match the
  server's real field names and value types — a client can parse a
  sample directly using the same code that parses a live frame.
* ``connect_url_template`` uses ``{base_ws_url}`` and
  ``{encoded_instrument_key}`` placeholders. ``connect_path`` is the
  ready-to-append path (instrument key URL-encoded).
* ``connect_url_example`` embeds the base URL resolved for the current
  request, so the examples are copy-pasteable.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/ws-samples", tags=["ws-samples"])


# ---------------------------------------------------------------------------
# Static sample catalog
# ---------------------------------------------------------------------------
# Every entry has the same shape:
#
#   {
#     "id":              stable identifier, e.g. "nifty-50-index",
#     "category":        "main-index" | "options",
#     "label":           human-readable name,
#     "instrument_key":  the WS subscription key,
#     "instrument":      the "instrument" block of a snapshot message,
#     "connect_path":    "/ws/live-ema/by-instrument/<encoded key>",
#     "connect_by_parts": optional, present for F&O instruments,
#     "messages": {
#         "snapshot":         {...},
#         "tick":             {...},
#         "candle_completed": {...},
#         "ema_cross":        {...},
#     },
#   }
# ---------------------------------------------------------------------------

_MAIN_INDEX_SAMPLES: list[dict[str, Any]] = [
    {
        "id": "nifty-50-index",
        "category": "main-index",
        "label": "Nifty 50 Index",
        "instrument_key": "NSE_INDEX|Nifty 50",
        "instrument": {
            "instrument_key": "NSE_INDEX|Nifty 50",
            "trading_symbol": "NIFTY 50",
            "instrument_type": None,
            "strike_price": None,
            "expiry": None,
            "underlying_key": "NSE_INDEX|Nifty 50",
        },
        "connect_path": (
            "/ws/live-ema/by-instrument/NSE_INDEX%7CNifty%2050"
        ),
        "messages": {
            "snapshot": {
                "type": "snapshot",
                "server_time": "2026-10-13T03:55:00.142871+00:00",
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
                    "use_live_ltp_feed": True,
                    "interval_seconds": 60,
                },
            },
            "tick": {
                "type": "tick",
                "server_time": "2026-10-13T03:55:01.587214+00:00",
                "instrument_key": "NSE_INDEX|Nifty 50",
                "data": {
                    "ltp": 22680.95,
                    "ltt": "1791348706000",
                    "cp": 22776.10,
                    "forming_candle": {
                        "timestamp": "2026-10-13T09:25:00+05:30",
                        "open": 22678.40,
                        "high": 22682.15,
                        "low": 22677.90,
                        "close": 22680.95,
                        "tick_count": 42,
                        "interval_seconds": 60,
                    },
                },
            },
            "candle_completed": {
                "type": "candle_completed",
                "server_time": "2026-10-13T03:56:00.041220+00:00",
                "instrument_key": "NSE_INDEX|Nifty 50",
                "data": {
                    "candle": {
                        "timestamp": "2026-10-13T09:25:00+05:30",
                        "open": 22678.40,
                        "high": 22682.15,
                        "low": 22677.90,
                        "close": 22680.95,
                        "volume": 0,
                        "oi": 0,
                        "tick_count": 87,
                        "interval_seconds": 60,
                    },
                    "ema": {
                        "fast_period": 9,
                        "slow_period": 21,
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
                "server_time": "2026-10-13T03:57:00.041612+00:00",
                "instrument_key": "NSE_INDEX|Nifty 50",
                "data": {
                    "timestamp": "2026-10-13T09:26:00+05:30",
                    "type": "bullish",
                    "close": 22682.35,
                    "ema_fast": 22681.7712,
                    "ema_slow": 22681.5104,
                },
            },
        },
    },
    {
        "id": "sensex-index",
        "category": "main-index",
        "label": "BSE SENSEX Index",
        "instrument_key": "BSE_INDEX|SENSEX",
        "instrument": {
            "instrument_key": "BSE_INDEX|SENSEX",
            "trading_symbol": "SENSEX",
            "instrument_type": None,
            "strike_price": None,
            "expiry": None,
            "underlying_key": "BSE_INDEX|SENSEX",
        },
        "connect_path": "/ws/live-ema/by-instrument/BSE_INDEX%7CSENSEX",
        "messages": {
            "snapshot": {
                "type": "snapshot",
                "server_time": "2026-10-13T03:55:00.187113+00:00",
                "instrument_key": "BSE_INDEX|SENSEX",
                "instrument": {
                    "instrument_key": "BSE_INDEX|SENSEX",
                    "trading_symbol": "SENSEX",
                    "instrument_type": None,
                    "strike_price": None,
                    "expiry": None,
                    "underlying_key": "BSE_INDEX|SENSEX",
                },
                "server": {
                    "use_live_ltp_feed": True,
                    "interval_seconds": 60,
                },
            },
            "tick": {
                "type": "tick",
                "server_time": "2026-10-13T03:55:01.612044+00:00",
                "instrument_key": "BSE_INDEX|SENSEX",
                "data": {
                    "ltp": 74512.30,
                    "ltt": "1791348707123",
                    "cp": 74688.55,
                    "forming_candle": {
                        "timestamp": "2026-10-13T09:25:00+05:30",
                        "open": 74508.10,
                        "high": 74516.45,
                        "low": 74505.80,
                        "close": 74512.30,
                        "tick_count": 37,
                        "interval_seconds": 60,
                    },
                },
            },
            "candle_completed": {
                "type": "candle_completed",
                "server_time": "2026-10-13T03:56:00.052815+00:00",
                "instrument_key": "BSE_INDEX|SENSEX",
                "data": {
                    "candle": {
                        "timestamp": "2026-10-13T09:25:00+05:30",
                        "open": 74508.10,
                        "high": 74516.45,
                        "low": 74505.80,
                        "close": 74512.30,
                        "volume": 0,
                        "oi": 0,
                        "tick_count": 74,
                        "interval_seconds": 60,
                    },
                    "ema": {
                        "fast_period": 9,
                        "slow_period": 21,
                        "ema_fast": 74511.2089,
                        "ema_slow": 74513.4417,
                        "ema_fast_prev": 74510.3344,
                        "ema_slow_prev": 74513.8890,
                    },
                    "cross": None,
                },
            },
            "ema_cross": {
                "type": "ema_cross",
                "server_time": "2026-10-13T03:58:00.061901+00:00",
                "instrument_key": "BSE_INDEX|SENSEX",
                "data": {
                    "timestamp": "2026-10-13T09:27:00+05:30",
                    "type": "bearish",
                    "close": 74505.85,
                    "ema_fast": 74509.8821,
                    "ema_slow": 74510.0913,
                },
            },
        },
    },
]


_OPTION_SAMPLES: list[dict[str, Any]] = [
    {
        "id": "nifty-ce-option",
        "category": "options",
        "label": "NIFTY 22650 CE (Call)",
        "instrument_key": "NSE_FO|44694",
        "instrument": {
            "instrument_key": "NSE_FO|44694",
            "trading_symbol": "NIFTY 22650 CE 30 OCT 26",
            "instrument_type": "CE",
            "strike_price": 22650.0,
            "expiry": "2026-10-30",
            "underlying_key": "NSE_INDEX|Nifty 50",
            "underlying_symbol": "NIFTY",
            "lot_size": 75,
        },
        "connect_path": "/ws/live-ema/by-instrument/NSE_FO%7C44694",
        "connect_by_parts": (
            "/ws/live-ema/by-parts"
            "?underlying=nifty&strike=22650&type=CE"
        ),
        "messages": {
            "snapshot": {
                "type": "snapshot",
                "server_time": "2026-10-13T03:55:00.221508+00:00",
                "instrument_key": "NSE_FO|44694",
                "instrument": {
                    "instrument_key": "NSE_FO|44694",
                    "trading_symbol": "NIFTY 22650 CE 30 OCT 26",
                    "instrument_type": "CE",
                    "strike_price": 22650.0,
                    "expiry": "2026-10-30",
                    "underlying_key": "NSE_INDEX|Nifty 50",
                    "underlying_symbol": "NIFTY",
                    "lot_size": 75,
                },
                "server": {
                    "use_live_ltp_feed": True,
                    "interval_seconds": 60,
                },
            },
            "tick": {
                "type": "tick",
                "server_time": "2026-10-13T03:55:01.640312+00:00",
                "instrument_key": "NSE_FO|44694",
                "data": {
                    "ltp": 142.35,
                    "ltt": "1791348707455",
                    "cp": 138.90,
                    "forming_candle": {
                        "timestamp": "2026-10-13T09:25:00+05:30",
                        "open": 141.80,
                        "high": 143.10,
                        "low": 141.55,
                        "close": 142.35,
                        "tick_count": 29,
                        "interval_seconds": 60,
                    },
                },
            },
            "candle_completed": {
                "type": "candle_completed",
                "server_time": "2026-10-13T03:56:00.063447+00:00",
                "instrument_key": "NSE_FO|44694",
                "data": {
                    "candle": {
                        "timestamp": "2026-10-13T09:25:00+05:30",
                        "open": 141.80,
                        "high": 143.10,
                        "low": 141.55,
                        "close": 142.35,
                        "volume": 0,
                        "oi": 0,
                        "tick_count": 61,
                        "interval_seconds": 60,
                    },
                    "ema": {
                        "fast_period": 9,
                        "slow_period": 21,
                        "ema_fast": 142.1084,
                        "ema_slow": 142.4419,
                        "ema_fast_prev": 141.9877,
                        "ema_slow_prev": 142.5103,
                    },
                    "cross": None,
                },
            },
            "ema_cross": {
                "type": "ema_cross",
                "server_time": "2026-10-13T03:59:00.070118+00:00",
                "instrument_key": "NSE_FO|44694",
                "data": {
                    "timestamp": "2026-10-13T09:28:00+05:30",
                    "type": "bullish",
                    "close": 144.05,
                    "ema_fast": 143.7112,
                    "ema_slow": 143.6904,
                },
            },
        },
    },
    {
        "id": "nifty-pe-option",
        "category": "options",
        "label": "NIFTY 22700 PE (Put)",
        "instrument_key": "NSE_FO|44710",
        "instrument": {
            "instrument_key": "NSE_FO|44710",
            "trading_symbol": "NIFTY 22700 PE 30 OCT 26",
            "instrument_type": "PE",
            "strike_price": 22700.0,
            "expiry": "2026-10-30",
            "underlying_key": "NSE_INDEX|Nifty 50",
            "underlying_symbol": "NIFTY",
            "lot_size": 75,
        },
        "connect_path": "/ws/live-ema/by-instrument/NSE_FO%7C44710",
        "connect_by_parts": (
            "/ws/live-ema/by-parts"
            "?underlying=nifty&strike=22700&type=PE"
        ),
        "messages": {
            "snapshot": {
                "type": "snapshot",
                "server_time": "2026-10-13T03:55:00.251902+00:00",
                "instrument_key": "NSE_FO|44710",
                "instrument": {
                    "instrument_key": "NSE_FO|44710",
                    "trading_symbol": "NIFTY 22700 PE 30 OCT 26",
                    "instrument_type": "PE",
                    "strike_price": 22700.0,
                    "expiry": "2026-10-30",
                    "underlying_key": "NSE_INDEX|Nifty 50",
                    "underlying_symbol": "NIFTY",
                    "lot_size": 75,
                },
                "server": {
                    "use_live_ltp_feed": True,
                    "interval_seconds": 60,
                },
            },
            "tick": {
                "type": "tick",
                "server_time": "2026-10-13T03:55:01.663145+00:00",
                "instrument_key": "NSE_FO|44710",
                "data": {
                    "ltp": 78.55,
                    "ltt": "1791348707788",
                    "cp": 75.20,
                    "forming_candle": {
                        "timestamp": "2026-10-13T09:25:00+05:30",
                        "open": 78.10,
                        "high": 79.05,
                        "low": 77.85,
                        "close": 78.55,
                        "tick_count": 24,
                        "interval_seconds": 60,
                    },
                },
            },
            "candle_completed": {
                "type": "candle_completed",
                "server_time": "2026-10-13T03:56:00.075844+00:00",
                "instrument_key": "NSE_FO|44710",
                "data": {
                    "candle": {
                        "timestamp": "2026-10-13T09:25:00+05:30",
                        "open": 78.10,
                        "high": 79.05,
                        "low": 77.85,
                        "close": 78.55,
                        "volume": 0,
                        "oi": 0,
                        "tick_count": 55,
                        "interval_seconds": 60,
                    },
                    "ema": {
                        "fast_period": 9,
                        "slow_period": 21,
                        "ema_fast": 78.4128,
                        "ema_slow": 78.5509,
                        "ema_fast_prev": 78.5011,
                        "ema_slow_prev": 78.5822,
                    },
                    "cross": None,
                },
            },
            "ema_cross": {
                "type": "ema_cross",
                "server_time": "2026-10-13T03:57:00.080411+00:00",
                "instrument_key": "NSE_FO|44710",
                "data": {
                    "timestamp": "2026-10-13T09:26:00+05:30",
                    "type": "bearish",
                    "close": 77.85,
                    "ema_fast": 78.2890,
                    "ema_slow": 78.3312,
                },
            },
        },
    },
    {
        "id": "nifty-future",
        "category": "options",
        "label": "NIFTY Futures (current month)",
        "instrument_key": "NSE_FO|44812",
        "instrument": {
            "instrument_key": "NSE_FO|44812",
            "trading_symbol": "NIFTY FUT 30 OCT 26",
            "instrument_type": "FUT",
            "strike_price": None,
            "expiry": "2026-10-30",
            "underlying_key": "NSE_INDEX|Nifty 50",
            "underlying_symbol": "NIFTY",
            "lot_size": 75,
        },
        "connect_path": "/ws/live-ema/by-instrument/NSE_FO%7C44812",
        "connect_by_parts": (
            "/ws/live-ema/by-parts"
            "?underlying=nifty&type=FUT"
        ),
        "messages": {
            "snapshot": {
                "type": "snapshot",
                "server_time": "2026-10-13T03:55:00.281004+00:00",
                "instrument_key": "NSE_FO|44812",
                "instrument": {
                    "instrument_key": "NSE_FO|44812",
                    "trading_symbol": "NIFTY FUT 30 OCT 26",
                    "instrument_type": "FUT",
                    "strike_price": None,
                    "expiry": "2026-10-30",
                    "underlying_key": "NSE_INDEX|Nifty 50",
                    "underlying_symbol": "NIFTY",
                    "lot_size": 75,
                },
                "server": {
                    "use_live_ltp_feed": True,
                    "interval_seconds": 60,
                },
            },
            "tick": {
                "type": "tick",
                "server_time": "2026-10-13T03:55:01.689477+00:00",
                "instrument_key": "NSE_FO|44812",
                "data": {
                    "ltp": 22708.65,
                    "ltt": "1791348707932",
                    "cp": 22812.10,
                    "forming_candle": {
                        "timestamp": "2026-10-13T09:25:00+05:30",
                        "open": 22705.10,
                        "high": 22711.45,
                        "low": 22703.80,
                        "close": 22708.65,
                        "tick_count": 33,
                        "interval_seconds": 60,
                    },
                },
            },
            "candle_completed": {
                "type": "candle_completed",
                "server_time": "2026-10-13T03:56:00.089912+00:00",
                "instrument_key": "NSE_FO|44812",
                "data": {
                    "candle": {
                        "timestamp": "2026-10-13T09:25:00+05:30",
                        "open": 22705.10,
                        "high": 22711.45,
                        "low": 22703.80,
                        "close": 22708.65,
                        "volume": 0,
                        "oi": 0,
                        "tick_count": 68,
                        "interval_seconds": 60,
                    },
                    "ema": {
                        "fast_period": 9,
                        "slow_period": 21,
                        "ema_fast": 22707.8821,
                        "ema_slow": 22709.1044,
                        "ema_fast_prev": 22707.3120,
                        "ema_slow_prev": 22709.5502,
                    },
                    "cross": None,
                },
            },
            "ema_cross": {
                "type": "ema_cross",
                "server_time": "2026-10-13T03:58:00.095118+00:00",
                "instrument_key": "NSE_FO|44812",
                "data": {
                    "timestamp": "2026-10-13T09:27:00+05:30",
                    "type": "bullish",
                    "close": 22712.55,
                    "ema_fast": 22710.4031,
                    "ema_slow": 22710.2810,
                },
            },
        },
    },
]


_CATALOG: dict[str, list[dict[str, Any]]] = {
    "main-index": _MAIN_INDEX_SAMPLES,
    "options": _OPTION_SAMPLES,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resolve_base_ws_url(request: Request) -> str:
    """Resolve the caller-facing WebSocket base URL.

    Order of precedence matches ``app/api/routes.py``:

    1. ``settings.public_ws_base_url``
    2. ``X-Forwarded-Proto`` / ``X-Forwarded-Host``
    3. The incoming request's scheme + Host header
    """
    configured = getattr(settings, "public_ws_base_url", None)
    if configured:
        return str(configured).rstrip("/")

    forwarded_proto = request.headers.get("x-forwarded-proto")
    scheme = (
        forwarded_proto.split(",")[0].strip()
        if forwarded_proto
        else request.url.scheme
    )
    ws_scheme = "wss" if scheme in ("https", "wss") else "ws"

    forwarded_host = request.headers.get("x-forwarded-host")
    host = (
        forwarded_host.split(",")[0].strip()
        if forwarded_host
        else request.headers.get("host")
    ) or request.url.netloc

    return f"{ws_scheme}://{host}"


def _enrich(
    sample: dict[str, Any], base_ws_url: str
) -> dict[str, Any]:
    """Return a copy of ``sample`` with concrete connect URLs attached."""
    encoded = quote(sample["instrument_key"], safe="")

    by_instrument = f"{base_ws_url}/ws/live-ema/by-instrument/{encoded}"
    by_parts = (
        f"{base_ws_url}{sample['connect_by_parts']}"
        if sample.get("connect_by_parts")
        else None
    )

    return {
        **sample,
        "connect_url_template": (
            "{base_ws_url}/ws/live-ema/by-instrument/"
            "{encoded_instrument_key}"
        ),
        "connect_url": by_instrument,
        "connect_url_by_parts": by_parts,
    }


def _category_payload(
    category: str, base_ws_url: str
) -> dict[str, Any]:
    samples = _CATALOG[category]
    return {
        "category": category,
        "count": len(samples),
        "samples": [_enrich(s, base_ws_url) for s in samples],
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("")
async def all_samples(request: Request) -> dict[str, Any]:
    """Return every sample, grouped by category.

    Includes a short ``overview`` of the message types, the resolved
    ``base_ws_url`` for the calling request, and per-sample connect URLs.
    """
    base_ws_url = _resolve_base_ws_url(request)

    categories = {
        name: _category_payload(name, base_ws_url)
        for name in _CATALOG
    }

    return {
        "overview": (
            "Reference catalog of the live-EMA WebSocket message shapes. "
            "Every sample uses the same field names and value types the "
            "server emits on a real subscription. Use 'connect_url' to "
            "open a socket and reproduce the same stream."
        ),
        "base_ws_url": base_ws_url,
        "config": {
            "use_live_ltp_feed": settings.use_live_ltp_feed,
            "interval_seconds": settings.live_ema_interval_seconds,
            "ema_fast_period": settings.ema_fast_period,
            "ema_slow_period": settings.ema_slow_period,
            "market_timezone": settings.market_timezone,
            "market_open_time": settings.market_open_time,
            "market_close_time": settings.market_close_time,
        },
        "message_types": {
            "snapshot": (
                "Sent immediately after connect with the latest known "
                "state for the instrument."
            ),
            "tick": "Every upstream LTP tick; includes the forming candle.",
            "candle_completed": (
                "A bucket closed. Includes the completed OHLC and the "
                "recomputed EMA fast/slow values."
            ),
            "ema_cross": (
                "A new cross detected on a *completed* candle. Never "
                "emitted for a partial candle."
            ),
            "error": "Human-readable error (e.g. unknown instrument).",
        },
        "categories": {
            name: {
                "count": entry["count"],
                "sample_ids": [
                    s["id"] for s in entry["samples"]
                ],
            }
            for name, entry in categories.items()
        },
        "samples": categories,
    }


@router.get("/categories")
async def list_categories() -> dict[str, Any]:
    """Return a lightweight index of every category and its sample ids."""
    return {
        "count": len(_CATALOG),
        "categories": [
            {
                "name": name,
                "count": len(samples),
                "sample_ids": [s["id"] for s in samples],
            }
            for name, samples in _CATALOG.items()
        ],
    }


@router.get("/main-index")
async def main_index_samples(request: Request) -> dict[str, Any]:
    """Return index-instrument samples only (Nifty 50, SENSEX, ...)."""
    return _category_payload("main-index", _resolve_base_ws_url(request))


@router.get("/options")
async def options_samples(request: Request) -> dict[str, Any]:
    """Return F&O samples only (CE / PE / FUT)."""
    return _category_payload("options", _resolve_base_ws_url(request))


@router.get("/sample/{sample_id}")
async def single_sample(sample_id: str, request: Request) -> dict[str, Any]:
    """Return one sample by id, e.g. ``nifty-50-index``."""
    base_ws_url = _resolve_base_ws_url(request)

    for samples in _CATALOG.values():
        for sample in samples:
            if sample["id"] == sample_id:
                return _enrich(sample, base_ws_url)

    raise HTTPException(
        status_code=404,
        detail=f"Unknown sample id: {sample_id}",
    )


@router.get("/{category}")
async def samples_by_category(
    category: str, request: Request
) -> dict[str, Any]:
    """Generic category lookup. Declared last so it never shadows a
    more specific route above it.
    """
    if category not in _CATALOG:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Unknown category: {category}. "
                f"Available: {sorted(_CATALOG.keys())}"
            ),
        )
    return _category_payload(category, _resolve_base_ws_url(request))