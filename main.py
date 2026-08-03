import asyncio
import json
import uvicorn
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, status
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core import config
from core.logger import get_logger
from services.option_service import get_options_contracts, options_cache
from services.token_service import token_service
from services.upstox_websocket import upstox_streamer
from ws_feed.broadcaster import broadcaster

logger = get_logger(__file__)


def load_and_subscribe_instruments():
    """Fetches option contracts, updates memory cache, and logs subscription details."""
    logger.info("Executing contract load and subscription workflow...")

    result = get_options_contracts(save_data=True)

    if not result:
        logger.error("Failed to load option contracts for subscription.")
        return

    subscribed_keys = options_cache.get("subscribed_keys", [])

    logger.info("================ SUBSCRIPTION SUMMARY ================")
    logger.info(
        f"Main Security: {getattr(config, 'MAIN_NIFTY_SECURITY', 'NSE_INDEX|Nifty 50')}"
    )
    logger.info(
        f"Strike Price Range: {getattr(config, 'STRIKE_FROM', 'N/A')} "
        f"to {getattr(config, 'STRIKE_TO', 'N/A')}"
    )
    logger.info(
        f"Total Subscribed Instruments (Index + Options): {len(subscribed_keys)}"
    )
    logger.info("=======================================================")


def run_initial_startup():
    """Initial synchronous load when the application starts up."""
    logger.info("Initializing application startup sequence...")

    # 1. Fetch access token from DB into memory
    token_service.refresh_tokens()

    # 2. Fetch option contracts and update subscription keys
    load_and_subscribe_instruments()

    # 3. Startup cache and sample log
    current_token = token_service.get_access_token()
    doc = token_service.get_token_document()
    cached_data = options_cache.get("data", [])
    sample_contract = cached_data[0] if cached_data else None

    logger.info("=== Memory Cache State Summary ===")
    logger.info(f"Access Token: {current_token[:15]}..." if current_token else "No Token")
    logger.info(f"Token Updated At: {doc.get('updated_at') if doc else 'N/A'}")
    logger.info(f"Nearest Expiry in Cache: {options_cache.get('nearest_expiry')}")
    logger.info(f"Total Cached Contracts: {options_cache.get('total_contracts')}")

    if sample_contract:
        logger.info(f"Sample Contract:\n{json.dumps(sample_contract, indent=2)}")
    else:
        logger.info("Sample Contract: None (Cache Empty)")


def start_scheduler() -> BackgroundScheduler:
    """Configures and starts background cron/interval tasks."""
    scheduler = BackgroundScheduler()

    scheduler.add_job(
        func=token_service.refresh_tokens,
        trigger="interval",
        minutes=config.REFRESH_INTERVAL_MINUTES,
        id="token_refresh_job",
        replace_existing=True,
    )

    scheduler.add_job(
        func=load_and_subscribe_instruments,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=9,
            minute=0,
            timezone="Asia/Kolkata",
        ),
        id="options_contracts_daily_job",
        replace_existing=True,
    )

    scheduler.start()

    logger.info(
        f"Scheduler active: Token refresh every {config.REFRESH_INTERVAL_MINUTES} mins | "
        f"Options fetch scheduled for Mon-Fri at 09:00 AM IST."
    )

    return scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handles async lifecycle startup/shutdown events for FastAPI."""
    logger.info("Executing lifespan startup sequence...")

    scheduler = None

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, run_initial_startup)

        scheduler = start_scheduler()

        # upstox_streamer.start() already creates its own background task
        await upstox_streamer.start()

        logger.info("Application startup completed successfully.")

        yield

    finally:
        logger.info("Executing lifespan shutdown sequence...")

        await upstox_streamer.stop()

        if scheduler and scheduler.running:
            logger.info("Shutting down background scheduler...")
            scheduler.shutdown()


app = FastAPI(title="Option Feed Engine", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", status_code=status.HTTP_200_OK)
async def get_health_status():
    """Returns the application health and system status in JSON format."""

    has_token = bool(token_service.get_access_token())
    cached_keys_count = len(options_cache.get("subscribed_keys", []))

    connected_clients = (
        broadcaster.get_active_connections_count()
        if hasattr(broadcaster, "get_active_connections_count")
        else 0
    )

    websocket_running = bool(getattr(upstox_streamer, "is_running", False))

    is_healthy = has_token and cached_keys_count > 0 and websocket_running

    return {
        "status": "healthy" if is_healthy else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": {
            "token_service": "active" if has_token else "missing_token",
            "options_cache": (
                f"loaded ({cached_keys_count} keys)"
                if cached_keys_count > 0
                else "empty"
            ),
            "websocket_feed": "active" if websocket_running else "inactive",
        },
        "metrics": {
            "subscribed_instruments": cached_keys_count,
            "connected_ws_clients": connected_clients,
        },
    }


@app.websocket("/ws")
@app.websocket("/all-feeds")
async def websocket_all_feeds(websocket: WebSocket):
    """WebSocket endpoint returning live market feeds for all loaded contracts."""

    logger.info(f"Incoming websocket request for /all-feeds from {websocket.client}")

    try:
        await websocket.accept()
        logger.info("WebSocket accepted for /all-feeds")

        if hasattr(broadcaster, "connect_all_feeds"):
            await broadcaster.connect_all_feeds(websocket)
        else:
            await broadcaster.connect(websocket)

        await websocket.send_json(
            {
                "type": "connected",
                "endpoint": "/all-feeds",
                "message": "Connected to all feeds websocket. Waiting for live market ticks.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "subscribed_instruments": len(options_cache.get("subscribed_keys", [])),
            }
        )

        while True:
            try:
                client_message = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=30,
                )

                if client_message:
                    await websocket.send_json(
                        {
                            "type": "client_message_received",
                            "message": client_message,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )

            except asyncio.TimeoutError:
                await websocket.send_json(
                    {
                        "type": "ping",
                        "endpoint": "/all-feeds",
                        "message": "WebSocket alive. Waiting for live ticks.",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )

    except WebSocketDisconnect:
        logger.info("Client disconnected from /all-feeds websocket")

    except Exception as ex:
        logger.error(f"/all-feeds websocket error: {type(ex).__name__}: {ex}")

    finally:
        if hasattr(broadcaster, "disconnect_all_feeds"):
            broadcaster.disconnect_all_feeds(websocket)
        else:
            broadcaster.disconnect(websocket)


@app.websocket("/option")
async def websocket_option(
    websocket: WebSocket,
    strike: float = Query(..., description="Strike Price, e.g., 24500"),
    striketype: str = Query(..., description="Option type: ce or pe"),
):
    """WebSocket endpoint filtering live feeds by strike price and CE/PE."""

    itype = striketype.upper()

    logger.info(
        f"Incoming websocket request for /option from {websocket.client}. "
        f"strike={strike}, striketype={itype}"
    )

    if itype not in ["CE", "PE"]:
        try:
            await websocket.accept()
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "Invalid striketype. Value must be 'ce' or 'pe'.",
                    "received": striketype,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            await websocket.close(code=1008, reason="striketype must be 'ce' or 'pe'")
        except Exception as ex:
            logger.error(
                f"Error while closing invalid /option websocket: "
                f"{type(ex).__name__}: {ex}"
            )
        return

    try:
        await websocket.accept()
        logger.info(f"WebSocket accepted for /option {strike}_{itype}")

        if hasattr(broadcaster, "connect_option"):
            await broadcaster.connect_option(
                websocket,
                strike_price=strike,
                instrument_type=itype,
            )
        else:
            await broadcaster.connect(websocket)

        logger.info(f"Option client registered for {strike}_{itype}")

        await websocket.send_json(
            {
                "type": "connected",
                "endpoint": "/option",
                "strike": strike,
                "striketype": itype,
                "message": "Connected to option websocket. Waiting for matching option ticks.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

        while True:
            try:
                client_message = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=30,
                )

                if client_message:
                    await websocket.send_json(
                        {
                            "type": "client_message_received",
                            "message": client_message,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )

            except asyncio.TimeoutError:
                await websocket.send_json(
                    {
                        "type": "ping",
                        "endpoint": "/option",
                        "strike": strike,
                        "striketype": itype,
                        "message": "WebSocket alive. Waiting for matching option ticks.",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )

    except WebSocketDisconnect:
        logger.info(f"Client disconnected from /option websocket: {strike}_{itype}")

    except Exception as ex:
        logger.error(
            f"/option websocket error for {strike}_{itype}: "
            f"{type(ex).__name__}: {ex}"
        )

    finally:
        if hasattr(broadcaster, "disconnect_option"):
            broadcaster.disconnect_option(websocket, strike, itype)
        else:
            broadcaster.disconnect(websocket)


@app.get("/test-broadcast")
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


@app.get("/test-broadcast-option")
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


@app.get("/debug/cache")
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


@app.get("/debug/find-option")
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


if __name__ == "__main__":
    logger.info("Starting FastAPI server with WebSockets on http://0.0.0.0:8000 ...")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")