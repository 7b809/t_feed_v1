import asyncio
import json
import uvicorn
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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
    
    # Load options contracts and save output
    result = get_options_contracts(save_data=True)

    if not result:
        logger.error("Failed to load option contracts for subscription.")
        return

    subscribed_keys = options_cache.get("subscribed_keys", [])
    
    logger.info("================ SUBSCRIPTION SUMMARY ================")
    logger.info(f"Main Security: {getattr(config, 'MAIN_NIFTY_SECURITY', 'NSE_INDEX|Nifty 50')}")
    logger.info(f"Strike Price Range: {getattr(config, 'STRIKE_FROM', 'N/A')} to {getattr(config, 'STRIKE_TO', 'N/A')}")
    logger.info(f"Total Subscribed Instruments (Index + Options): {len(subscribed_keys)}")
    logger.info("=======================================================")


def run_initial_startup():
    """Initial synchronous load when the application starts up."""
    logger.info("Initializing application startup sequence...")

    # 1. Fetch access token from DB into memory
    token_service.refresh_tokens()

    # 2. Fetch option contracts and update subscription keys
    load_and_subscribe_instruments()

    # 3. Startup Cache & Sample Log
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

    # Refresh token every 60 minutes
    scheduler.add_job(
        func=token_service.refresh_tokens,
        trigger="interval",
        minutes=config.REFRESH_INTERVAL_MINUTES,
        id="token_refresh_job",
        replace_existing=True,
    )

    # Refresh option contracts Mon-Fri at 09:00 AM IST
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

    # 1. Run blocking synchronous initial startup in an executor so it doesn't block the loop
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, run_initial_startup)

    # 2. Start background scheduler
    scheduler = start_scheduler()

    # 3. Start Upstox WebSocket Streamer as a background task
    streamer_task = asyncio.create_task(upstox_streamer.start())
    
    yield  # Allows server startup to complete and handle incoming WebSockets immediately

    # Shutdown sequence
    logger.info("Executing lifespan shutdown sequence...")
    await upstox_streamer.stop()
    streamer_task.cancel()

    if scheduler and scheduler.running:
        logger.info("Shutting down background scheduler...")
        scheduler.shutdown()


# Initialize FastAPI App
app = FastAPI(title="Option Feed Engine", lifespan=lifespan)

# Add CORSMiddleware to accept requests/WebSockets from file:// or any origin
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
    
    # Check status of underlying services
    has_token = bool(token_service.get_access_token())
    cached_keys_count = len(options_cache.get("subscribed_keys", []))
    connected_clients = broadcaster.get_active_connections_count() if hasattr(broadcaster, "get_active_connections_count") else 0

    # Determine overall system status
    is_healthy = has_token and cached_keys_count > 0

    return {
        "status": "healthy" if is_healthy else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": {
            "token_service": "active" if has_token else "missing_token",
            "options_cache": f"loaded ({cached_keys_count} keys)" if cached_keys_count > 0 else "empty",
            "websocket_feed": "active"
        },
        "metrics": {
            "subscribed_instruments": cached_keys_count,
            "connected_ws_clients": connected_clients
        }
    }


@app.websocket("/ws")
@app.websocket("/all-feeds")
async def websocket_all_feeds(websocket: WebSocket):
    """WebSocket endpoint returning live market feeds for all loaded contracts."""
    # Complete the handshake before registering with broadcaster
    await websocket.accept()

    if hasattr(broadcaster, "connect_all_feeds"):
        await broadcaster.connect_all_feeds(websocket)
    else:
        await broadcaster.connect(websocket)

    try:
        while True:
            # Keeps endpoint connection open without requiring text frames from client
            await asyncio.sleep(3600)
    except WebSocketDisconnect:
        if hasattr(broadcaster, "disconnect_all_feeds"):
            broadcaster.disconnect_all_feeds(websocket)
        else:
            broadcaster.disconnect(websocket)


@app.websocket("/option")
async def websocket_option(
    websocket: WebSocket,
    strike: float = Query(..., description="Strike Price, e.g., 23450"),
    striketype: str = Query(..., description="Option type: 'pe' or 'ce'")
):
    """WebSocket endpoint filtering live feeds by strike price and type (CE/PE)."""
    itype = striketype.upper()
    if itype not in ["CE", "PE"]:
        await websocket.close(code=1008, reason="striketype must be 'ce' or 'pe'")
        return

    # Complete the handshake before registering with broadcaster
    await websocket.accept()

    if hasattr(broadcaster, "connect_option"):
        await broadcaster.connect_option(websocket, strike_price=strike, instrument_type=itype)
        try:
            while True:
                # Keeps endpoint connection open without requiring text frames from client
                await asyncio.sleep(3600)
        except WebSocketDisconnect:
            broadcaster.disconnect_option(websocket, strike, itype)
    else:
        await broadcaster.connect(websocket)
        try:
            while True:
                await asyncio.sleep(3600)
        except WebSocketDisconnect:
            broadcaster.disconnect(websocket)


if __name__ == "__main__":
    logger.info("Starting FastAPI server with WebSockets on http://0.0.0.0:8000 ...")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")