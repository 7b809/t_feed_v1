# main.py

from __future__ import annotations

from contextlib import asynccontextmanager
from threading import Thread
from typing import Any

import uvicorn
from fastapi import FastAPI

from api.ema_routes import (
    router as ema_router,
)
from api.refresh_routes import (
    configure_refresh_routes,
    router as refresh_router,
)
from api.websocket_routes import (
    router as websocket_router,
)
from core import config
from core.logger import get_logger
from core.token_service import token_service
from services.ema_query_service import (
    ema_query_service,
)
from services.runtime import EmaRuntime
from services.scheduler import Scheduler
from services.telegram_service import telegram_service
from services.websocket_manager import (
    websocket_manager,
)

logger = get_logger(__file__)


# ---------------------------------------------------------------------------
# Debug Configuration
# ---------------------------------------------------------------------------

DEBUG_MODE = False


def _debug(message: str, *args: Any) -> None:
    """
    Print debug information only when DEBUG_MODE is enabled.
    """
    if not DEBUG_MODE:
        return

    if args:
        message = message.format(*args)

    print(f"[MAIN DEBUG] {message}")


# ---------------------------------------------------------------------------
# Global application services
# ---------------------------------------------------------------------------

runtime: EmaRuntime | None = None
scheduler: Scheduler | None = None
scheduler_thread: Thread | None = None


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(
    application: FastAPI,
):
    """
    FastAPI application lifecycle.

    Startup:
        1. Create EMA runtime.
        2. Create scheduler.
        3. Configure API dependencies.
        4. Start scheduler thread.

    Shutdown:
        1. Stop scheduler.
        2. Wait for scheduler thread.
        3. Close runtime.
        4. Close token service.
        5. Close Telegram service.
    """

    global runtime
    global scheduler
    global scheduler_thread

    _debug("Lifespan startup initiated")

    logger.info("NIFTY EMA service startup initiated")

    try:
        # ---------------------------------------------------------------
        # Create runtime
        # ---------------------------------------------------------------

        _debug("Creating EmaRuntime")

        runtime = EmaRuntime(
            token_service,
            telegram_service,
        )

        _debug(
            "EmaRuntime created: {}",
            type(runtime).__name__,
        )

        # ---------------------------------------------------------------
        # Create scheduler
        # ---------------------------------------------------------------

        _debug("Creating Scheduler")

        scheduler = Scheduler(
            token_service,
            runtime,
            telegram_service,
        )

        _debug(
            "Scheduler created: {}",
            type(scheduler).__name__,
        )

        # ---------------------------------------------------------------
        # Store dependencies in FastAPI application state
        # ---------------------------------------------------------------

        _debug("Storing dependencies in application.state")

        application.state.ema_runtime = runtime
        application.state.ema_scheduler = scheduler
        application.state.ema_query_service = ema_query_service
        application.state.ema_websocket_manager = websocket_manager

        # ---------------------------------------------------------------
        # Configure refresh controller
        # ---------------------------------------------------------------

        _debug("Configuring refresh routes with scheduler")

        configure_refresh_routes(scheduler)

        # ---------------------------------------------------------------
        # Start scheduler in background thread
        # ---------------------------------------------------------------

        _debug("Starting scheduler background thread")

        scheduler_thread = Thread(
            target=scheduler.run,
            name="ema-scheduler",
            daemon=True,
        )

        scheduler_thread.start()

        _debug(
            "Scheduler thread started name={} daemon={}",
            scheduler_thread.name,
            scheduler_thread.daemon,
        )

        logger.info("NIFTY EMA scheduler thread started")

        # ---------------------------------------------------------------
        # Notify service startup
        # ---------------------------------------------------------------

        try:
            _debug("Sending startup Telegram notification")

            telegram_service.send("NIFTY EMA crossover service started")

        except Exception:
            logger.exception("Startup Telegram notification failed")

        _debug("Lifespan startup completed; yielding control")

        logger.info("NIFTY EMA service startup completed")

        yield

    except Exception:
        logger.exception("NIFTY EMA service startup failed")
        raise

    finally:
        _debug("Lifespan shutdown initiated")

        logger.info("NIFTY EMA service shutdown initiated")

        # ---------------------------------------------------------------
        # Stop scheduler
        # ---------------------------------------------------------------

        if scheduler is not None:
            try:
                _debug("Requesting scheduler stop")

                scheduler.stop()

                logger.info("EMA scheduler stop requested")

            except Exception:
                logger.exception("Failed to stop EMA scheduler")

        # ---------------------------------------------------------------
        # Wait for scheduler thread
        # ---------------------------------------------------------------

        if scheduler_thread is not None:
            try:
                _debug(
                    "Joining scheduler thread name={}",
                    scheduler_thread.name,
                )

                scheduler_thread.join(timeout=10)

                if scheduler_thread.is_alive():
                    _debug("Scheduler thread still alive after join timeout")

                    logger.warning(
                        "EMA scheduler thread did not " "stop within timeout"
                    )

                else:
                    _debug("Scheduler thread stopped cleanly")

                    logger.info("EMA scheduler thread stopped")

            except Exception:
                logger.exception("Failed while joining scheduler thread")

        # ---------------------------------------------------------------
        # Close runtime
        # ---------------------------------------------------------------

        if runtime is not None:
            try:
                _debug("Closing runtime")

                runtime.close()

                logger.info("EMA runtime closed")

            except Exception:
                logger.exception("Failed to close EMA runtime")

        # ---------------------------------------------------------------
        # Close token service
        # ---------------------------------------------------------------

        try:
            _debug("Closing token service")

            token_service.close()

            logger.info("Token service closed")

        except Exception:
            logger.exception("Failed to close token service")

        # ---------------------------------------------------------------
        # Notify service shutdown
        # ---------------------------------------------------------------

        try:
            _debug("Sending shutdown Telegram notification")

            telegram_service.send("NIFTY EMA crossover service stopped")

        except Exception:
            logger.exception("Shutdown Telegram notification failed")

        # ---------------------------------------------------------------
        # Close Telegram service
        # ---------------------------------------------------------------

        try:
            close_method = getattr(
                telegram_service,
                "close",
                None,
            )

            if callable(close_method):
                _debug("Closing Telegram service")

                close_method()

                logger.info("Telegram service closed")

            else:
                _debug("Telegram service has no close() method; skipping")

        except Exception:
            logger.exception("Failed to close Telegram service")

        _debug("Lifespan shutdown completed")

        logger.info("NIFTY EMA service shutdown completed")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------


_debug("Initializing FastAPI application")

app = FastAPI(
    title="NIFTY EMA Crossover Service",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

_debug("Registering API routers")

app.include_router(refresh_router)

app.include_router(ema_router)

app.include_router(websocket_router)

_debug("All API routers registered")


# ---------------------------------------------------------------------------
# Application health metadata
# ---------------------------------------------------------------------------


@app.get(
    "/",
    tags=["System"],
)
def root() -> dict[str, Any]:
    """
    Basic service information.
    """

    _debug("Root endpoint requested")

    return {
        "service": "NIFTY EMA Crossover Service",
        "version": "1.0.0",
        "status": "running",
        "endpoints": {
            "health": "/api/health",
            "state": "/api/state",
            "ema_api": "/api/ema",
            "websocket": "/ws/ema",
        },
    }


# ---------------------------------------------------------------------------
# Local application runner
# ---------------------------------------------------------------------------


def main() -> None:
    """
    Start the FastAPI application using Uvicorn.
    """

    _debug(
        "Starting Uvicorn host={} port={} log_level={}",
        config.API_HOST,
        config.API_PORT,
        config.LOG_LEVEL.lower(),
    )

    logger.info(
        "NIFTY EMA API starting on %s:%s",
        config.API_HOST,
        config.API_PORT,
    )

    uvicorn.run(
        app,
        host=config.API_HOST,
        port=config.API_PORT,
        log_level=config.LOG_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()
