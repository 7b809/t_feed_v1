from datetime import datetime, timezone

from fastapi import APIRouter, Query

from services.option_service import options_cache
from ws_feed.broadcaster import broadcaster

router = APIRouter()


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
        "target": "24500.0_CE",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


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

    itype = striketype.upper()
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
    }
