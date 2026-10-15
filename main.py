"""Foundational FastAPI app for home, charts, market history, token
lifecycle, backup extraction, live feed / EMA-cross engine, Opening
Range levels, and the multi-stage index strategy pipeline.

Project startup delegates to the shared ``_run_full_refresh_sync``
routine so that project startup, the scheduled daily refresh, and the
manual hard-refresh endpoint all execute the exact same task list in
the same order:

    1.  Telegram start notification
    2.  Cleanup transient folders
    3.  Refresh token cache from MongoDB
    4.  Validate Upstox token
    5.  Backup extract from MongoDB
    6.  Reload instrument registry
    6b. Pre-register core + strategy indexes
    7.  Wire broadcast loop + cross handler
    8.  Reset today's live-cross state
    9.  EMA backfill seed (backup + intraday)
    10. Opening Range (OR) level calculation
    10b. Strategy stage 1 (buffer filter + last-day-cross)
    10c. Strategy stage 2 (EMA-cross isolation, paired CE/PE)
    10d. Strategy stage 3 tracker start (R2 activation watcher)
    11. Start / restart live feed
    12. Start scheduler (idempotent)
    13. Start Telegram bot (idempotent)
    14. Telegram completion notification
    15. Return structured result

Shutdown handling is owned by the lifespan only.

Admin surfaces mounted by this module:

    Folder API            routes/folder_routes.py
    Opening Range API     routes/opening_range_routes.py
    Strategy API          routes/strategy_routes.py
    Strategy Dashboard    routes/dashboard_routes.py
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.logger import get_logger
from routes.admin_routes import _run_full_refresh_sync
from routes.admin_routes import router as admin_router
from routes.dashboard_routes import router as dashboard_router
from routes.folder_routes import router as folder_router
from routes.live_routes import router as live_router
from routes.log_routes import router as log_router
from routes.opening_range_routes import router as opening_range_router
from routes.page_routes import router as page_router
from routes.strategy_routes import router as strategy_router
from routes.upstox_routes import router as upstox_router
from services.live_feed_service import live_feed_service
from services.scheduler_service import scheduler_service
from services.strategy_live_tracker_service import (
    strategy_live_tracker_service,
)
from services.telegram_service import telegram_service
from token_tasks.telegram_token_bot import telegram_token_bot

logger = get_logger(__file__)


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()

    try:
        # ---- Unified refresh (tasks 1..15) -------------------------------
        result = await asyncio.to_thread(
            _run_full_refresh_sync, "startup", loop
        )

        logger.info(
            "Application startup complete. feed_instruments=%d "
            "telegram_polling=%s strategy_indexes=%d tracker=%s",
            (result.get("registry") or {}).get("count", 0),
            result.get("telegram_started"),
            len((result.get("strategy") or {}).get("indexes", [])),
            result.get("tracker_started"),
        )
        telegram_service.send_message(
            "Application Started",
            (
                f"Instruments loaded: "
                f"{(result.get('registry') or {}).get('count', 0)}\n"
                f"OR levels: "
                f"{(result.get('opening_range') or {}).get('calculated')}"
                f"/{(result.get('opening_range') or {}).get('total')}\n"
                f"Strategy indexes: "
                f"{len((result.get('strategy') or {}).get('indexes', []))}\n"
                f"Stage 3 tracker: {result.get('tracker_started')}\n"
                f"Live feed running: {result.get('feed_restarted')}"
            ),
            level="SUCCESS",
        )
        yield

    finally:
        # ---- Shutdown handling -------------------------------------------
        telegram_service.send_message(
            "Application Stopping",
            "Market engine shutting down.",
            level="INFO",
        )
        try:
            live_feed_service.stop()
        except Exception:
            logger.exception("Live feed stop failed")
        try:
            strategy_live_tracker_service.stop()
        except Exception:
            logger.exception("Strategy tracker stop failed")
        try:
            telegram_token_bot.stop()
        except Exception:
            logger.exception("Telegram token bot stop failed")
        try:
            scheduler_service.shutdown()
        except Exception:
            logger.exception("Scheduler shutdown failed")
        telegram_service.send_message(
            "Application Stopped",
            "Market engine shutdown complete.",
            level="INFO",
        )
        logger.info("Application shutdown complete.")


app = FastAPI(title="Market Data Foundation", lifespan=app_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(page_router)
app.include_router(live_router)
app.include_router(admin_router)
app.include_router(folder_router)
app.include_router(opening_range_router)
app.include_router(strategy_router)
app.include_router(dashboard_router)
app.include_router(upstox_router)
app.include_router(log_router)