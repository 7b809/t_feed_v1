from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from threading import Thread
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import (
    CORSMiddleware,
)

from api.ema_routes import (
    router as ema_router,
)
from api.logs_routes import router as logs_router

from api.refresh_routes import (
    configure_refresh_routes,
    router as refresh_router,
)
from api.websocket_routes import (
    ALL_WEBSOCKET_PATH,
    FULL_WEBSOCKET_PATH,
    MULTIPLE_WEBSOCKET_PATH,
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
from services.telegram_service import (
    telegram_service,
)
from services.websocket_manager import (
    websocket_manager,
)

logger = get_logger(__file__)

DEBUG_MODE = False

runtime: EmaRuntime | None = None
scheduler: Scheduler | None = None
scheduler_thread: Thread | None = None


def _debug(
    message: str,
    *args: Any,
) -> None:
    if not DEBUG_MODE:
        return

    if args:
        message = message.format(*args)

    print(f"[MAIN DEBUG] {message}")


def _get_bool_setting(
    name: str,
    default: bool,
) -> bool:
    value = getattr(
        config,
        name,
        default,
    )

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        return value.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
            "enabled",
        }

    return bool(value)


def _get_cors_origins() -> list[str]:
    value = getattr(
        config,
        "CORS_ALLOWED_ORIGINS",
        [
            "http://localhost",
            "http://localhost:3000",
            "http::4200",
            "http://localhost:5173",
            "http://127.0.0.1",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:4200",
            "http://127.0.0.1:5173",
        ],
    )

    if isinstance(value, str):
        return [origin.strip() for origin in value.split(",") if origin.strip()]

    if isinstance(
        value,
        (list, tuple, set),
    ):
        return [str(origin).strip() for origin in value if str(origin).strip()]

    return []


def _log_registered_routes(
    application: FastAPI,
) -> None:
    registered_paths = {
        str(
            getattr(
                route,
                "path",
                "",
            )
        )
        for route in application.routes
    }

    required_routes = {
        FULL_WEBSOCKET_PATH,
        ALL_WEBSOCKET_PATH,
        MULTIPLE_WEBSOCKET_PATH,
        (f"{FULL_WEBSOCKET_PATH}/" "available"),
        (f"{FULL_WEBSOCKET_PATH}/" "{instrument_key:path}"),
    }

    for route in application.routes:
        logger.info(
            "Registered API route " "path=%s name=%s methods=%s " "type=%s",
            getattr(
                route,
                "path",
                None,
            ),
            getattr(
                route,
                "name",
                None,
            ),
            getattr(
                route,
                "methods",
                None,
            ),
            type(route).__name__,
        )

    for required_path in sorted(required_routes):
        if required_path not in registered_paths:
            logger.error(
                "Required WebSocket route " "is not registered path=%s",
                required_path,
            )


def _send_telegram_message(
    message: str,
    operation: str,
) -> None:
    try:
        telegram_service.send(message)
    except Exception:
        logger.exception(
            "%s Telegram notification failed",
            operation,
        )


@asynccontextmanager
async def lifespan(
    application: FastAPI,
):
    global runtime
    global scheduler
    global scheduler_thread

    _debug("Lifespan startup initiated")

    logger.info("NIFTY EMA service startup initiated")

    startup_completed = False

    try:
        event_loop = asyncio.get_running_loop()

        runtime = EmaRuntime(
            token_service=token_service,
            telegram_service=telegram_service,
            websocket_manager=(websocket_manager),
            websocket_loop=event_loop,
        )

        ema_query_service.set_runtime(runtime)

        scheduler = Scheduler(
            token_service,
            runtime,
            telegram_service,
        )

        application.state.ema_runtime = runtime
        application.state.runtime = runtime

        application.state.ema_scheduler = scheduler
        application.state.scheduler = scheduler

        application.state.ema_query_service = ema_query_service

        application.state.ema_websocket_manager = websocket_manager
        application.state.websocket_manager = websocket_manager

        configure_refresh_routes(scheduler)

        scheduler_thread = Thread(
            target=scheduler.run,
            name="ema-scheduler",
            daemon=True,
        )

        scheduler_thread.start()

        logger.info(
            "NIFTY EMA scheduler thread " "started thread=%s",
            scheduler_thread.name,
        )

        await asyncio.to_thread(
            _send_telegram_message,
            ("NIFTY EMA crossover " "service started"),
            "Startup",
        )

        startup_completed = True

        logger.info(
            "NIFTY EMA service startup " "completed websocket_path=%s",
            FULL_WEBSOCKET_PATH,
        )

        yield

    except Exception:
        logger.exception("NIFTY EMA service startup failed")
        raise

    finally:
        logger.info(
            "NIFTY EMA service shutdown initiated " "startup_completed=%s",
            startup_completed,
        )

        if scheduler is not None:
            try:
                scheduler.stop()

                logger.info("EMA scheduler stop requested")

            except Exception:
                logger.exception("Failed to stop EMA scheduler")

        if scheduler_thread is not None:
            try:
                await asyncio.to_thread(
                    scheduler_thread.join,
                    10,
                )

                if scheduler_thread.is_alive():
                    logger.warning(
                        "EMA scheduler thread did " "not stop within timeout"
                    )
                else:
                    logger.info("EMA scheduler thread stopped")

            except Exception:
                logger.exception("Failed while joining " "scheduler thread")

        try:
            await websocket_manager.close_all()

            logger.info("EMA WebSocket connections closed")

        except Exception:
            logger.exception("Failed to close EMA WebSocket " "connections")

        if runtime is not None:
            try:
                runtime.close()

                logger.info("EMA runtime closed")

            except Exception:
                logger.exception("Failed to close EMA runtime")

        try:
            token_service.close()

            logger.info("Token service closed")

        except Exception:
            logger.exception("Failed to close token service")

        await asyncio.to_thread(
            _send_telegram_message,
            ("NIFTY EMA crossover " "service stopped"),
            "Shutdown",
        )

        try:
            close_method = getattr(
                telegram_service,
                "close",
                None,
            )

            if callable(close_method):
                close_result = close_method()

                if asyncio.iscoroutine(close_result):
                    await close_result

                logger.info("Telegram service closed")

        except Exception:
            logger.exception("Failed to close Telegram service")

        application.state.ema_runtime = None
        application.state.runtime = None

        application.state.ema_scheduler = None
        application.state.scheduler = None

        application.state.ema_query_service = None

        application.state.ema_websocket_manager = None
        application.state.websocket_manager = None

        ema_query_service.set_runtime(None)

        runtime = None
        scheduler = None
        scheduler_thread = None

        logger.info("NIFTY EMA service shutdown completed")


app = FastAPI(
    title="NIFTY EMA Crossover Service",
    version="1.0.0",
    lifespan=lifespan,
)

cors_origins = _get_cors_origins()

cors_allow_credentials = _get_bool_setting(
    "CORS_ALLOW_CREDENTIALS",
    True,
)

if "*" in cors_origins and cors_allow_credentials:
    logger.warning(
        "CORS wildcard origin cannot be "
        "used safely with credentials. "
        "CORS credentials are disabled."
    )

    cors_allow_credentials = False

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=(cors_allow_credentials),
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "Content-Disposition",
        "X-Request-ID",
    ],
    max_age=600,
)

logger.info(
    "CORS configured origins=%s " "allow_credentials=%s",
    cors_origins,
    cors_allow_credentials,
)

app.include_router(refresh_router)

app.include_router(ema_router)

app.include_router(websocket_router)
app.include_router(logs_router)

@app.get(
    "/",
    tags=["System"],
)
def root() -> dict[str, Any]:
    websocket_status = websocket_manager.get_status()

    ema_api_prefix = getattr(
        config,
        "EMA_API_PREFIX",
        "/ema/apis",
    )

    return {
        "service": ("NIFTY EMA Crossover Service"),
        "version": "1.0.0",
        "status": "running",
        "endpoints": {
            "root": "/",
            "ema_health": (f"{ema_api_prefix}/health"),
            "ema_state": (f"{ema_api_prefix}/state"),
            "ema_contracts": (f"{ema_api_prefix}/contracts"),
            "ema_latest": (f"{ema_api_prefix}/latest"),
            "ema_history": (f"{ema_api_prefix}/history"),
            "ema_intraday": (f"{ema_api_prefix}/intraday"),
            "ema_crossovers": (f"{ema_api_prefix}/crossovers"),
            "ema_query": (f"{ema_api_prefix}/query"),
            "ema_hard_refresh": (f"{ema_api_prefix}/hard-refresh"),
            "websocket_discovery": (f"{FULL_WEBSOCKET_PATH}/available"),
            "websocket_dynamic": (FULL_WEBSOCKET_PATH),
            "websocket_all": (ALL_WEBSOCKET_PATH),
            "websocket_multiple": (MULTIPLE_WEBSOCKET_PATH),
            "websocket_single": (f"{FULL_WEBSOCKET_PATH}/" "{encoded_instrument_key}"),
        },
        "websocket_examples": {
            "dynamic_connection": (FULL_WEBSOCKET_PATH),
            "dynamic_single_subscription": {
                "action": "subscribe",
                "instrument_key": ("NSE_FO|57002"),
            },
            "dynamic_multiple_subscription": {
                "action": "subscribe",
                "instrument_keys": [
                    "NSE_FO|57002",
                    "NSE_FO|57003",
                ],
            },
            "dynamic_all_subscription": {
                "action": "subscribe",
                "all": True,
            },
            "all_instruments": (ALL_WEBSOCKET_PATH),
            "multiple_instruments": (
                f"{MULTIPLE_WEBSOCKET_PATH}"
                "?instrument_keys="
                "NSE_FO%7C57002,"
                "NSE_FO%7C57003"
            ),
            "single_instrument": (f"{FULL_WEBSOCKET_PATH}/" "NSE_FO%7C57002"),
        },
        "websocket_status": (websocket_status),
    }


_log_registered_routes(app)


def main() -> None:
    logger.info(
        "NIFTY EMA API starting on %s:%s",
        config.API_HOST,
        config.API_PORT,
    )

    uvicorn.run(
        app,
        host=config.API_HOST,
        port=config.API_PORT,
        log_level=(config.LOG_LEVEL.lower()),
    )


if __name__ == "__main__":
    main()
