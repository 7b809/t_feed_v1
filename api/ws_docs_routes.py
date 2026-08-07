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

    New flow:
    - Opening Range is calculated for all subscribed instruments.
    - Live EMA crossover is calculated for all initialized instruments.
    - EMA crossover WebSocket payloads include Opening Range range/levels
      for the same instrument when available.
    - Selected OR instrument Telegram alert flow is disabled.
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
        "new_flow": {
            "description": (
                "The app subscribes to configured instruments, calculates Opening Range "
                "levels for every subscribed instrument, calculates live EMA crossovers "
                "for every initialized instrument, and broadcasts EMA crossover events "
                "through WebSocket with that instrument's Opening Range levels when available."
            ),
            "selected_or_instrument_flow": "disabled",
            "selected_or_ema_telegram_alerts": "disabled",
            "ema_websocket_opening_range_enrichment": True,
        },
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
                    "This includes NIFTY index and filtered option contracts. "
                    "It can also receive EMA crossover and Opening Range events "
                    "when broadcast by the backend."
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
                    "strike price and CE/PE type. It may also receive matching "
                    "EMA crossover events and Opening Range events for the same option."
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
                    "Streams live EMA crossover events for all initialized instruments. "
                    "Each EMA crossover payload includes Opening Range range and levels "
                    "for the same instrument when available."
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
                    "previous_ema_fast": 122.1,
                    "previous_ema_slow": 122.4,
                    "previous_signal": "bearish",
                    "current_signal": "bullish",
                    "source": "live_feed",
                    "created_at": "2026-08-06T09:21:05+05:30",
                    "candle": {
                        "timestamp": "2026-08-06T09:21:00+05:30",
                        "timestamp_ms": 1785988260000,
                        "open": 121.5,
                        "high": 126.0,
                        "low": 120.0,
                        "close": 124.5,
                        "volume": 2000,
                    },
                    "info": {
                        "instrument_key": "NSE_FO|41012",
                        "instrument_type": "CE",
                        "strike_price": 24500.0,
                        "trading_symbol": "NIFTY 24500 CE",
                    },
                    "opening_range": {
                        "available": True,
                        "instrument_key": "NSE_FO|41012",
                        "date": "2026-08-06",
                        "source": "intraday_api",
                        "interval": "1minute",
                        "unit": "minutes",
                        "intraday_interval": "1",
                        "opening_range_candle_count": 1,
                        "market_open_time": "09:15",
                        "fetch_time": "09:18",
                        "range": {
                            "open": 121.5,
                            "high": 126.0,
                            "low": 120.0,
                            "close": 124.0,
                            "average": 123.0,
                            "selected_candles_count": 1,
                            "first_candle_time": "2026-08-06T09:15:00+05:30",
                            "last_candle_time": "2026-08-06T09:15:00+05:30",
                        },
                        "levels": {
                            "r1": 124.5,
                            "s1": 121.5,
                            "r2": 129.0,
                            "s2": 117.0,
                            "r3": 132.0,
                            "s3": 114.0,
                            "sub_resistance": 124.5,
                            "sub_support": 121.5,
                            "resistance2": 129.0,
                            "support2": 117.0,
                            "resistance3": 132.0,
                            "support3": 114.0,
                            "r3_threshold": 130.5,
                            "s3_threshold": 115.5,
                        },
                        "touch_status": {
                            "r3_touched": False,
                            "s3_touched": False,
                            "r3_touch_time": None,
                            "s3_touch_time": None,
                            "first_touch_level": None,
                            "first_touch_source": None,
                            "first_touch_time": None,
                            "events": [],
                        },
                        "latest_intraday_close": 124.0,
                        "latest_main_index_ltp": 24580.25,
                    },
                },
                "sample_ema_cross_payload_without_opening_range": {
                    "type": "live_ema_cross",
                    "instrument_key": "NSE_FO|41012",
                    "timestamp": "2026-08-06T09:21:00+05:30",
                    "cross_type": "bullish_cross",
                    "interval_minutes": 1,
                    "close": 124.5,
                    "ema_fast": 123.8,
                    "ema_slow": 122.9,
                    "opening_range": {
                        "available": False,
                        "instrument_key": "NSE_FO|41012",
                        "message": (
                            "Opening Range levels are not available for this instrument."
                        ),
                        "range": None,
                        "levels": None,
                        "touch_status": None,
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
                    "Streams live EMA crossover events for one resolved instrument only. "
                    "Each event includes Opening Range levels for that instrument when available."
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
                    "and CE/PE type, then streams EMA crossover events only for that "
                    "resolved instrument. Each EMA event includes Opening Range levels "
                    "when available."
                ),
            },
            {
                "name": "Global Opening Range Feed",
                "endpoint": "/ws/opening-range",
                "method": "WebSocket",
                "url": f"{base_ws_url}/ws/opening-range",
                "description": (
                    "Streams Opening Range events for all instruments. "
                    "This includes R3/S3 touch events when available. "
                    "Touch events do not permanently select any instrument in the new flow."
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
                    "Streams Opening Range events for one resolved instrument only. "
                    "This does not mean the instrument is selected permanently."
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
                    "and CE/PE type, then streams Opening Range events only for that "
                    "resolved instrument."
                ),
            },
        ],
        "related_http_endpoints": {
            "opening_range_status": "GET /opening-range/status",
            "manual_opening_range_fetch": (
                "POST /opening-range/fetch?candle_count=1&save_results=true&max_workers=8"
            ),
            "opening_range_ema_context": (
                "GET /opening-range/ema-context?strike=24500&striketype=ce"
            ),
            "live_ema_events": (
                "GET /history/live-ema/events?limit=100&include_opening_range=true"
            ),
            "live_ema_instrument": (
                "GET /history/live-ema/instrument?strike=24500&striketype=ce"
                "&include_opening_range=true"
            ),
        },
        "browser_javascript_examples": {
            "all_feeds": {
                "description": "Simple browser JavaScript example for all feeds.",
                "code": (
                    "const ws = new WebSocket('"
                    f"{base_ws_url}/all-feeds"
                    "');\n"
                    "ws.onopen = () => console.log('connected to all feeds');\n"
                    "ws.onmessage = (event) => console.log(JSON.parse(event.data));\n"
                    "ws.onerror = (error) => console.error(error);\n"
                    "ws.onclose = () => console.log('closed');"
                ),
            },
            "ema_crossover": {
                "description": (
                    "Browser JavaScript example for global EMA crossover feed. "
                    "Each event can include opening_range."
                ),
                "code": (
                    "const emaWs = new WebSocket('"
                    f"{base_ws_url}/ws/ema-crossover"
                    "');\n"
                    "emaWs.onopen = () => console.log('connected to EMA feed');\n"
                    "emaWs.onmessage = (event) => {\n"
                    "  const payload = JSON.parse(event.data);\n"
                    "  console.log('EMA event:', payload);\n"
                    "  console.log('Opening Range:', payload.opening_range);\n"
                    "};\n"
                    "emaWs.onerror = (error) => console.error(error);\n"
                    "emaWs.onclose = () => console.log('EMA feed closed');"
                ),
            },
        },
        "websocat_examples": [
            f"websocat {base_ws_url}/all-feeds",
            f"websocat '{base_ws_url}/option?strike=24500&striketype=ce'",
            f"websocat {base_ws_url}/ws/ema-crossover",
            (
                f"websocat '{base_ws_url}/ws/ema-crossover/instrument"
                f"?strike=24500&striketype=ce'"
            ),
            f"websocat {base_ws_url}/ws/opening-range",
            (
                f"websocat '{base_ws_url}/ws/opening-range/instrument"
                f"?strike=24500&striketype=ce'"
            ),
        ],
    }
