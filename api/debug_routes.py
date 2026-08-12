from datetime import datetime, timezone

from fastapi import APIRouter, Query

from core import config
from services.option_service import options_cache
from ws_feed.broadcaster import broadcaster

router = APIRouter()


# ============================================================
# Live EMA Mode Helper
# ============================================================


def get_live_ema_calculation_mode_text() -> str:
    """
    Returns configured live EMA calculation mode.

    LIVE_EMA_CALCULATION_MODE = False
        completed candle close based EMA calculation.

    LIVE_EMA_CALCULATION_MODE = True
        live tick/LTP based EMA calculation.
    """

    return (
        "tick_ltp"
        if bool(getattr(config, "LIVE_EMA_CALCULATION_MODE", False))
        else "candle_close"
    )


def get_live_ema_calculation_mode_payload() -> dict:
    """
    Returns live EMA mode payload for debug/API responses.
    """

    flag = bool(getattr(config, "LIVE_EMA_CALCULATION_MODE", False))
    mode = get_live_ema_calculation_mode_text()

    return {
        "flag": flag,
        "mode": mode,
        "description": (
            "live tick/LTP based EMA calculation"
            if flag
            else "completed candle close based EMA calculation"
        ),
    }


# ============================================================
# Test Broadcast Routes
# ============================================================


@router.get("/test-broadcast")
async def test_broadcast():
    """
    Local debug endpoint.

    Use this to verify /ws and /all-feeds without depending on Upstox live ticks.
    """

    sample_tick = {
        "fullFeed": {
            "marketFF": {
                "ltpc": {
                    "ltp": 24500,
                    "cp": 24450,
                    "ltt": 0,
                    "ltq": 50,
                },
                "marketOHLC": {
                    "ohlc": [
                        {
                            "interval": "1d",
                            "open": 24400,
                            "high": 24550,
                            "low": 24350,
                            "vol": 100000,
                        }
                    ]
                },
                "atp": 24480,
                "vtt": 100000,
                "oi": 0,
            }
        }
    }

    contract_info = {
        "instrument_key": "NSE_INDEX|Nifty 50",
        "instrument_type": "INDEX",
        "strike_price": None,
        "expiry": None,
        "trading_symbol": "NIFTY 50",
        "underlying_type": "INDEX",
        "underlying_symbol": "NIFTY 50",
    }

    await broadcaster.broadcast_tick(
        instrument_key="NSE_INDEX|Nifty 50",
        tick_raw=sample_tick,
        contract_info=contract_info,
    )

    return {
        "status": "sent",
        "message": "Test broadcast sent to connected /ws and /all-feeds clients.",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
    }


@router.get("/test-broadcast-option")
async def test_broadcast_option():
    """
    Local debug endpoint for /option clients.

    Connect first to:
        ws://127.0.0.1:8000/option?strike=24500&striketype=ce

    Then call:
        http://127.0.0.1:8000/test-broadcast-option
    """

    sample_tick = {
        "fullFeed": {
            "marketFF": {
                "ltpc": {
                    "ltp": 120.5,
                    "cp": 115.0,
                    "ltt": 0,
                    "ltq": 75,
                },
                "marketOHLC": {
                    "ohlc": [
                        {
                            "interval": "1d",
                            "open": 110.0,
                            "high": 130.0,
                            "low": 100.0,
                            "vol": 250000,
                        }
                    ]
                },
                "atp": 118.5,
                "vtt": 250000,
                "oi": 500000,
                "iv": 12.5,
                "optionGreeks": {
                    "delta": 0.52,
                    "theta": -8.2,
                    "gamma": 0.001,
                    "vega": 10.4,
                    "rho": 1.25,
                },
                "marketLevel": {
                    "bidAskQuote": [
                        {
                            "bidQ": 100,
                            "bidP": 120.0,
                            "askQ": 150,
                            "askP": 121.0,
                        }
                    ]
                },
            }
        }
    }

    contract_info = {
        "instrument_key": "TEST_NSE_FO|24500CE",
        "instrument_type": "CE",
        "strike_price": 24500.0,
        "expiry": options_cache.get("nearest_expiry"),
        "trading_symbol": "NIFTY 24500 CE TEST",
        "underlying_type": "INDEX",
        "underlying_symbol": "NIFTY",
    }

    await broadcaster.broadcast_tick(
        instrument_key="TEST_NSE_FO|24500CE",
        tick_raw=sample_tick,
        contract_info=contract_info,
    )

    return {
        "status": "sent",
        "message": "Test option broadcast sent to connected /option clients for 24500 CE.",
        "target": "24500.0_CE_0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
    }


@router.get("/test-broadcast-ema")
async def test_broadcast_ema(
    strike: float = Query(
        default=24500.0,
        description="Option strike price for test EMA event.",
    ),
    striketype: str = Query(
        default="ce",
        description="Option type: ce or pe.",
    ),
    cross_type: str = Query(
        default="bullish_cross",
        description="EMA cross type: bullish_cross or bearish_cross.",
    ),
):
    """
    Local debug endpoint for EMA crossover WebSocket clients.

    Connect first to:
        ws://127.0.0.1:8000/ws/ema-crossover

    Or instrument-specific:
        ws://127.0.0.1:8000/ws/ema-crossover/instrument?instrument_key=TEST_NSE_FO|24500CE

    Or option:
        ws://127.0.0.1:8000/option?strike=24500&striketype=ce

    Then call:
        http://127.0.0.1:8000/test-broadcast-ema
    """

    option_type = str(striketype or "ce").upper()

    if option_type not in ["CE", "PE"]:
        option_type = "CE"

    if cross_type not in ["bullish_cross", "bearish_cross"]:
        cross_type = "bullish_cross"

    instrument_key = f"TEST_NSE_FO|{int(float(strike))}{option_type}"

    mode_payload = get_live_ema_calculation_mode_payload()
    ema_mode = mode_payload.get("mode")

    contract_info = {
        "instrument_key": instrument_key,
        "instrument_type": option_type,
        "strike_price": float(strike),
        "expiry": options_cache.get("nearest_expiry"),
        "trading_symbol": f"NIFTY {int(float(strike))} {option_type} TEST",
        "underlying_type": "INDEX",
        "underlying_symbol": "NIFTY",
    }

    now_ts = datetime.now(timezone.utc).isoformat()

    if ema_mode == "tick_ltp":
        ema_event = {
            "type": "live_ema_cross",
            "instrument_key": instrument_key,
            "timestamp": now_ts,
            "timestamp_ms": None,
            "cross_type": cross_type,
            "interval_minutes": 0,
            "close": 120.5,
            "ltp": 120.5,
            "ema_fast_period": getattr(config, "LIVE_EMA_FAST_PERIOD", 9),
            "ema_slow_period": getattr(config, "LIVE_EMA_SLOW_PERIOD", 21),
            "ema_fast": 121.25,
            "ema_slow": 120.95,
            "previous_ema_fast": 120.80,
            "previous_ema_slow": 120.90,
            "previous_signal": "bearish",
            "current_signal": "bullish",
            "source": "debug_live_tick",
            "ema_calculation_mode": "tick_ltp",
            "created_at": now_ts,
            "tick": {
                "timestamp": now_ts,
                "timestamp_ms": None,
                "ltp": 120.5,
                "ltq": 75,
                "ltt": 0,
            },
            "candle": None,
            "contract_info": contract_info,
            "info": contract_info,
            "telegram_alert_scope": "isolated_instrument_only",
            "broadcast_scope": "all_instruments",
        }
    else:
        ema_event = {
            "type": "live_ema_cross",
            "instrument_key": instrument_key,
            "timestamp": now_ts,
            "timestamp_ms": None,
            "cross_type": cross_type,
            "interval_minutes": getattr(config, "LIVE_EMA_INTERVAL_MINUTES", 1),
            "close": 120.5,
            "ema_fast_period": getattr(config, "LIVE_EMA_FAST_PERIOD", 9),
            "ema_slow_period": getattr(config, "LIVE_EMA_SLOW_PERIOD", 21),
            "ema_fast": 121.25,
            "ema_slow": 120.95,
            "previous_ema_fast": 120.80,
            "previous_ema_slow": 120.90,
            "previous_signal": "bearish",
            "current_signal": "bullish",
            "source": "debug_candle_close",
            "ema_calculation_mode": "candle_close",
            "created_at": now_ts,
            "candle": {
                "timestamp": now_ts,
                "timestamp_ms": None,
                "open": 118.0,
                "high": 123.0,
                "low": 117.5,
                "close": 120.5,
                "volume": 250000,
            },
            "tick": None,
            "contract_info": contract_info,
            "info": contract_info,
            "telegram_alert_scope": "isolated_instrument_only",
            "broadcast_scope": "all_instruments",
        }

    await broadcaster.broadcast_ema_cross(ema_event)

    return {
        "status": "sent",
        "message": "Test EMA crossover broadcast sent to connected EMA, all-feeds, and matching option clients.",
        "target_instrument_key": instrument_key,
        "target_option": f"{float(strike)}_{option_type}",
        "cross_type": cross_type,
        "timestamp": now_ts,
        "live_ema_calculation": mode_payload,
        "event": ema_event,
    }


@router.get("/test-broadcast-opening-range")
async def test_broadcast_opening_range(
    strike: float = Query(
        default=24500.0,
        description="Option strike price for test Opening Range event.",
    ),
    striketype: str = Query(
        default="ce",
        description="Option type: ce or pe.",
    ),
    level: str = Query(
        default="R3",
        description="Opening Range level: R2, R3, S2, or S3.",
    ),
):
    """
    Local debug endpoint for Opening Range WebSocket clients.

    Connect first to:
        ws://127.0.0.1:8000/ws/opening-range

    Or instrument-specific:
        ws://127.0.0.1:8000/ws/opening-range/instrument?instrument_key=TEST_NSE_FO|24500CE

    Or option:
        ws://127.0.0.1:8000/option?strike=24500&striketype=ce

    Then call:
        http://127.0.0.1:8000/test-broadcast-opening-range
    """

    option_type = str(striketype or "ce").upper()

    if option_type not in ["CE", "PE"]:
        option_type = "CE"

    level_upper = str(level or "R3").upper()

    if level_upper not in ["R2", "R3", "S2", "S3"]:
        level_upper = "R3"

    instrument_key = f"TEST_NSE_FO|{int(float(strike))}{option_type}"

    now_ts = datetime.now(timezone.utc).isoformat()

    contract_info = {
        "instrument_key": instrument_key,
        "instrument_type": option_type,
        "strike_price": float(strike),
        "expiry": options_cache.get("nearest_expiry"),
        "trading_symbol": f"NIFTY {int(float(strike))} {option_type} TEST",
        "underlying_type": "INDEX",
        "underlying_symbol": "NIFTY",
    }

    opening_range_event = {
        "type": "opening_range_touch",
        "instrument_key": instrument_key,
        "level": level_upper,
        "level_value": 130.0,
        "trigger_price": 131.25,
        "trigger_field": "high" if level_upper.startswith("R") else "low",
        "touch_time": now_ts,
        "source": "debug_api",
        "date": datetime.now(timezone.utc).date().isoformat(),
        "main_index_ltp": 24520.0,
        "distance_from_index": abs(float(strike) - 24520.0),
        "alert_key": f"{instrument_key}_{level_upper}",
        "contract_info": contract_info,
        "info": contract_info,
        "candle": {
            "timestamp": now_ts,
            "open": 118.0,
            "high": 131.25,
            "low": 117.5,
            "close": 129.5,
            "volume": 250000,
            "oi": 0,
        },
        "broadcast_scope": "all_instruments",
        "created_at": now_ts,
    }

    await broadcaster.broadcast_opening_range(opening_range_event)

    return {
        "status": "sent",
        "message": "Test Opening Range broadcast sent to connected Opening Range, all-feeds, and matching option clients.",
        "target_instrument_key": instrument_key,
        "target_option": f"{float(strike)}_{option_type}",
        "level": level_upper,
        "timestamp": now_ts,
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "event": opening_range_event,
    }


# ============================================================
# Cache Debug Routes
# ============================================================


@router.get("/debug/cache")
async def debug_cache():
    """Returns current in-memory cache details for local debugging."""

    cache_data = options_cache.get("data", [])

    return {
        "nearest_expiry": options_cache.get("nearest_expiry"),
        "total_contracts": options_cache.get("total_contracts"),
        "subscribed_keys_count": len(options_cache.get("subscribed_keys", [])),
        "sample_subscribed_keys": options_cache.get("subscribed_keys", [])[:5],
        "sample_contract": cache_data[0] if cache_data else None,
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
    }


@router.get("/debug/find-option")
async def debug_find_option(
    strike: float = Query(..., description="Strike Price, e.g., 24500"),
    striketype: str = Query(..., description="Option type: ce or pe"),
):
    """
    Debug endpoint to verify whether a specific option exists in options_cache.

    Example:
        http://127.0.0.1:8000/debug/find-option?strike=24500&striketype=ce
    """

    itype = str(striketype).upper()
    cache_data = options_cache.get("data", [])

    matches = []

    for item in cache_data:
        item_strike = item.get("strike_price")
        item_type = item.get("instrument_type")

        try:
            if float(item_strike) == float(strike) and str(item_type).upper() == itype:
                matches.append(item)
        except Exception:
            continue

    return {
        "search": {
            "strike": strike,
            "striketype": itype,
        },
        "matches_count": len(matches),
        "matches": matches,
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
    }
