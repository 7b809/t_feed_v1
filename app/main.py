import os
import logging

from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.config import settings

from app.database import (
    connect_to_mongo,
    close_mongo_connection,
    load_upstox_token,
)

# Routes
from app.routes.home_routes import router as home_router
from app.routes.options_routes import router as options_router
from app.routes.ema_routes import router as ema_router
from app.routes.feed_routes import router as feed_router

# WebSocket Routes
from app.websocket.feed_websocket import router as feed_websocket_router

# Services
from app.services.daily_refresh_service import refresh_market_data
from app.scheduler.market_scheduler import market_scheduler
from app.services.live_market_feed_service import market_feed_service
from app.services.telegram_service import telegram_service

logger = logging.getLogger("uvicorn")


# ---------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------


def init_logging():
    """
    Ensure log directory exists and initialize logging.
    """

    os.makedirs(settings.LOG_DIR, exist_ok=True)

    logging.basicConfig(
        level=getattr(
            logging,
            settings.LOG_LEVEL.upper(),
            logging.INFO,
        )
    )


def init_database():
    """
    Initialize MongoDB and load Upstox token.
    """

    logger.info("Initializing MongoDB...")

    connect_to_mongo()

    token_loaded = load_upstox_token()

    logger.info("Database initialization completed.")

    if token_loaded:
        telegram_service.send_success(
            title="Database Initialized",
            message="MongoDB connected and Upstox token loaded into memory.",
        )
    else:
        telegram_service.send_warning(
            title="Database Initialized With Warning",
            message="MongoDB connected, but Upstox token was not loaded.",
        )


def load_routes(app: FastAPI):
    """
    Register all API and WebSocket routes.
    """

    # REST APIs
    app.include_router(home_router)
    app.include_router(options_router)
    app.include_router(ema_router)
    app.include_router(feed_router)

    # Custom WebSocket APIs
    app.include_router(feed_websocket_router)


# ---------------------------------------------------------------------
# Application Lifecycle
# ---------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):

    logger.info("=" * 80)
    logger.info("UPSTOX SERVICE STARTING")
    logger.info("=" * 80)

    telegram_service.send_info(
        title="Upstox Service Starting",
        message="Application startup sequence has started.",
    )

    # ----------------------------------------------------------
    # Startup
    # ----------------------------------------------------------

    try:
        init_logging()

        init_database()

        # ------------------------------------------------------
        # Initial market refresh on application startup
        # ------------------------------------------------------

        logger.info("Running initial market refresh...")

        telegram_service.send_info(
            title="Initial Market Refresh Started",
            message=(
                "Refreshing options cache, historical candles, "
                "indicator cache, and live EMA cache."
            ),
        )

        refresh_result = refresh_market_data()

        logger.info(f"Initial refresh completed: {refresh_result}")

        if refresh_result and refresh_result.get("status") == "success":
            telegram_service.send_success(
                title="Initial Market Refresh Completed",
                message="Initial market refresh completed successfully.",
                details=refresh_result,
            )
        else:
            telegram_service.send_error(
                title="Initial Market Refresh Failed",
                message="Initial market refresh did not complete successfully.",
                details=refresh_result,
            )

        # ------------------------------------------------------
        # Start scheduler
        # ------------------------------------------------------

        await market_scheduler.start()

        logger.info("Market scheduler started.")

        telegram_service.send_success(
            title="Market Scheduler Started",
            message=(
                "Daily refresh, token monitoring, websocket connect, "
                "and websocket shutdown scheduler has started."
            ),
            details={
                "daily_refresh_time": settings.DAILY_REFRESH_TIME,
                "websocket_connect_time": settings.WEBSOCKET_CONNECT_TIME,
                "market_close_time": settings.MARKET_CLOSE_TIME,
            },
        )

    except Exception as ex:

        logger.exception(f"Application startup failed: {ex}")

        telegram_service.send_exception(
            title="Upstox Service Startup Failed",
            exception=ex,
            message="Application failed during startup sequence.",
        )

        raise

    yield

    # ----------------------------------------------------------
    # Shutdown
    # ----------------------------------------------------------

    logger.info("=" * 80)
    logger.info("UPSTOX SERVICE SHUTTING DOWN")
    logger.info("=" * 80)

    telegram_service.send_info(
        title="Upstox Service Shutting Down",
        message="Application shutdown sequence has started.",
    )

    try:

        await market_scheduler.stop()

        await market_feed_service.stop()

        close_mongo_connection()

        logger.info("Application shutdown completed.")

        telegram_service.send_success(
            title="Upstox Service Shutdown Completed",
            message="Scheduler stopped, websocket stopped, and MongoDB connection closed.",
        )

    except Exception as ex:

        logger.exception(f"Shutdown error: {ex}")

        telegram_service.send_exception(
            title="Upstox Service Shutdown Error",
            exception=ex,
            message="An error occurred during application shutdown.",
        )


# ---------------------------------------------------------------------
# FastAPI Application
# ---------------------------------------------------------------------

app = FastAPI(
    title="Upstox FastAPI Service",
    lifespan=lifespan,
)

load_routes(app)


# ---------------------------------------------------------------------
# Local Execution
# ---------------------------------------------------------------------

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=True,
    )
