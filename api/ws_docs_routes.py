from fastapi import APIRouter, Request

from core.logger import get_logger

logger = get_logger(__file__)

router = APIRouter()


@router.get("/docs/websockets")
async def get_websocket_docs(request: Request):
    """
    Returns WebSocket endpoint documentation and sample connection URLs.

    Note:
    FastAPI Swagger/OpenAPI does not execute WebSocket routes directly.
    This HTTP endpoint exposes WebSocket connection examples so they are visible
    from /docs.
    """

    host = request.headers.get("host", "127.0.0.1:8000")
    scheme = request.url.scheme

    ws_scheme = "wss" if scheme == "https" else "ws"
    base_ws_url = f"{ws_scheme}://{host}"

    return {
        "status": "success",
        "title": "WebSocket Connection Examples",
        "note": (
            "Swagger UI does not directly execute WebSocket routes. "
            "Use these URLs in browser JavaScript, Postman, Hoppscotch, "
            "websocat, or any WebSocket client."
        ),
        "base_ws_url": base_ws_url,
        "websockets": [
            {
                "name": "All Feeds",
                "endpoint": "/all-feeds",
                "method": "WebSocket",
                "url": f"{base_ws_url}/all-feeds",
                "alternate_url": f"{base_ws_url}/ws",
                "description": (
                    "Streams live ticks for all subscribed instruments. "
                    "This includes NIFTY index and filtered option contracts."
                ),
                "sample_connected_response": {
                    "type": "connected",
                    "endpoint": "/all-feeds",
                    "message": (
                        "Connected to all feeds websocket. "
                        "Waiting for live market ticks."
                    ),
                    "subscribed_instruments": 83,
                },
                "sample_live_tick_payload": {
                    "type": "live_tick",
                    "interval": 0,
                    "instrument_key": "NSE_FO|41012",
                    "ltp": 120.5,
                    "close": 115.0,
                    "change": 5.5,
                    "change_pct": 4.78,
                    "open": 110.0,
                    "high": 130.0,
                    "low": 100.0,
                    "volume": 250000,
                    "oi": 500000,
                    "info": {
                        "instrument_key": "NSE_FO|41012",
                        "instrument_type": "CE",
                        "strike_price": 24500.0,
                        "trading_symbol": "NIFTY 24500 CE",
                    },
                },
            },
            {
                "name": "Single Option Feed",
                "endpoint": "/option",
                "method": "WebSocket",
                "url": f"{base_ws_url}/option?strike=24500&striketype=ce",
                "alternate_examples": [
                    f"{base_ws_url}/option?strike=24500&striketype=pe",
                    f"{base_ws_url}/option?strike=24600&striketype=ce",
                ],
                "query_parameters": {
                    "strike": {
                        "required": True,
                        "example": 24500,
                        "description": "Option strike price.",
                    },
                    "striketype": {
                        "required": True,
                        "example": "ce",
                        "allowed_values": ["ce", "pe", "CE", "PE"],
                        "description": "Option type.",
                    },
                },
                "description": (
                    "Streams live ticks for one option contract based on "
                    "strike price and CE/PE type."
                ),
                "sample_connected_response": {
                    "type": "connected",
                    "endpoint": "/option",
                    "strike": 24500,
                    "striketype": "CE",
                    "message": (
                        "Connected to option websocket. "
                        "Waiting for matching option ticks."
                    ),
                },
            },
            {
                "name": "Global EMA Crossover Feed",
                "endpoint": "/ws/ema-crossover",
                "method": "WebSocket",
                "url": f"{base_ws_url}/ws/ema-crossover",
                "description": (
                    "Streams live EMA crossover events for all instruments."
                ),
                "sample_connected_response": {
                    "type": "connected",
                    "endpoint": "/ws/ema-crossover",
                    "message": (
                        "Connected to EMA Crossover feed. "
                        "Waiting for crossover signals."
                    ),
                },
                "sample_ema_cross_payload": {
                    "type": "live_ema_cross",
                    "instrument_key": "NSE_FO|41012",
                    "timestamp": "2026-08-06T09:21:00+05:30",
                    "cross_type": "bullish_cross",
                    "interval_minutes": 1,
                    "close": 124.5,
                    "ema_fast_period": 9,
                    "ema_slow_period": 21,
                    "ema_fast": 123.8,
                    "ema_slow": 122.9,
                    "source": "live_feed",
                    "info": {
                        "instrument_type": "CE",
                        "strike_price": 24500.0,
                        "trading_symbol": "NIFTY 24500 CE",
                    },
                },
            },
            {
                "name": "Instrument-Specific EMA Crossover Feed By Instrument Key",
                "endpoint": "/ws/ema-crossover/instrument",
                "method": "WebSocket",
                "url": (
                    f"{base_ws_url}/ws/ema-crossover/instrument"
                    f"?instrument_key=NSE_INDEX%7CNifty%2050"
                ),
                "alternate_examples": [
                    (
                        f"{base_ws_url}/ws/ema-crossover/instrument"
                        f"?instrument_key=NSE_FO%7C41012"
                    )
                ],
                "query_parameters": {
                    "instrument_key": {
                        "required": False,
                        "example": "NSE_INDEX|Nifty 50",
                        "description": (
                            "Direct instrument key. Use URL encoding for pipe and spaces."
                        ),
                    },
                    "strike": {
                        "required": False,
                        "example": 24500,
                        "description": (
                            "Option strike. Used only when instrument_key is not provided."
                        ),
                    },
                    "striketype": {
                        "required": False,
                        "example": "ce",
                        "allowed_values": ["ce", "pe", "CE", "PE"],
                        "description": (
                            "Option type. Used only when instrument_key is not provided."
                        ),
                    },
                },
                "description": (
                    "Streams live EMA crossover events for one selected instrument only."
                ),
                "sample_connected_response": {
                    "type": "connected",
                    "endpoint": "/ws/ema-crossover/instrument",
                    "instrument_key": "NSE_INDEX|Nifty 50",
                    "message": (
                        "Connected to instrument-specific EMA crossover feed. "
                        "Waiting for crossover signals."
                    ),
                },
            },
            {
                "name": "Instrument-Specific EMA Crossover Feed By Strike",
                "endpoint": "/ws/ema-crossover/instrument",
                "method": "WebSocket",
                "url": (
                    f"{base_ws_url}/ws/ema-crossover/instrument"
                    f"?strike=24500&striketype=ce"
                ),
                "alternate_examples": [
                    (
                        f"{base_ws_url}/ws/ema-crossover/instrument"
                        f"?strike=24500&striketype=pe"
                    )
                ],
                "description": (
                    "Resolves option instrument from options_cache using strike "
                    "and CE/PE type, then streams EMA crossover events only for that instrument."
                ),
            },
            {
                "name": "Global Opening Range Feed",
                "endpoint": "/ws/opening-range",
                "method": "WebSocket",
                "url": f"{base_ws_url}/ws/opening-range",
                "description": (
                    "Streams Opening Range events for all instruments. "
                    "This includes R3/S3 touch events when available."
                ),
                "sample_connected_response": {
                    "type": "connected",
                    "endpoint": "/ws/opening-range",
                    "message": (
                        "Connected to Opening Range feed. "
                        "Waiting for opening range events."
                    ),
                },
                "sample_opening_range_touch_payload": {
                    "type": "opening_range_touch",
                    "instrument_key": "NSE_FO|41012",
                    "level": "R3",
                    "level_value": 126.75,
                    "trigger_price": 128.5,
                    "trigger_field": "high",
                    "touch_time": "2026-08-06T09:16:00+05:30",
                    "source": "intraday_backfill_scan",
                    "date": "2026-08-06",
                    "main_index_ltp": 24580.25,
                    "distance_from_index": 19.75,
                    "alert_key": "NSE_FO|41012_R3",
                    "contract_info": {
                        "instrument_key": "NSE_FO|41012",
                        "instrument_type": "CE",
                        "strike_price": 24600.0,
                        "trading_symbol": "NIFTY 24600 CE",
                    },
                },
            },
            {
                "name": "Instrument-Specific Opening Range Feed By Instrument Key",
                "endpoint": "/ws/opening-range/instrument",
                "method": "WebSocket",
                "url": (
                    f"{base_ws_url}/ws/opening-range/instrument"
                    f"?instrument_key=NSE_INDEX%7CNifty%2050"
                ),
                "alternate_examples": [
                    (
                        f"{base_ws_url}/ws/opening-range/instrument"
                        f"?instrument_key=NSE_FO%7C41012"
                    )
                ],
                "query_parameters": {
                    "instrument_key": {
                        "required": False,
                        "example": "NSE_INDEX|Nifty 50",
                        "description": (
                            "Direct instrument key. Use URL encoding for pipe and spaces."
                        ),
                    },
                    "strike": {
                        "required": False,
                        "example": 24500,
                        "description": (
                            "Option strike. Used only when instrument_key is not provided."
                        ),
                    },
                    "striketype": {
                        "required": False,
                        "example": "ce",
                        "allowed_values": ["ce", "pe", "CE", "PE"],
                        "description": (
                            "Option type. Used only when instrument_key is not provided."
                        ),
                    },
                },
                "description": (
                    "Streams Opening Range events for one selected instrument only."
                ),
                "sample_connected_response": {
                    "type": "connected",
                    "endpoint": "/ws/opening-range/instrument",
                    "instrument_key": "NSE_INDEX|Nifty 50",
                    "message": (
                        "Connected to instrument-specific Opening Range feed. "
                        "Waiting for opening range events."
                    ),
                },
            },
            {
                "name": "Instrument-Specific Opening Range Feed By Strike",
                "endpoint": "/ws/opening-range/instrument",
                "method": "WebSocket",
                "url": (
                    f"{base_ws_url}/ws/opening-range/instrument"
                    f"?strike=24500&striketype=ce"
                ),
                "alternate_examples": [
                    (
                        f"{base_ws_url}/ws/opening-range/instrument"
                        f"?strike=24500&striketype=pe"
                    )
                ],
                "description": (
                    "Resolves option instrument from options_cache using strike "
                    "and CE/PE type, then streams Opening Range events only for that instrument."
                ),
            },
        ],
        "browser_javascript_example": {
            "description": "Simple browser JavaScript example.",
            "code": (
                "const ws = new WebSocket('"
                f"{base_ws_url}/all-feeds"
                "');\n"
                "ws.onopen = () => console.log('connected');\n"
                "ws.onmessage = (event) => console.log(JSON.parse(event.data));\n"
                "ws.onerror = (error) => console.error(error);\n"
                "ws.onclose = () => console.log('closed');"
            ),
        },
        "websocat_examples": [
            f"websocat {base_ws_url}/all-feeds",
            f"websocat {base_ws_url}/option?strike=24500\\&striketype=ce",
            f"websocat {base_ws_url}/ws/ema-crossover",
            (
                f"websocat {base_ws_url}/ws/ema-crossover/instrument"
                f"?strike=24500\\&striketype=ce"
            ),
            f"websocat {base_ws_url}/ws/opening-range",
            (
                f"websocat {base_ws_url}/ws/opening-range/instrument"
                f"?strike=24500\\&striketype=ce"
            ),
        ],
    }
