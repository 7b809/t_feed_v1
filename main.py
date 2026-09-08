from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.routes.health import router as health_router
from api.routes.order_requests import router as order_requests_router

from core.config import settings
from core.database import (
    close_mongo_connection,
    connect_to_mongo,
)
from core.logger import get_logger

from services.telegram_bot_service import (
    telegram_bot_service,
)
from services.token_service import (
    token_service,
)

logger = get_logger("app")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await connect_to_mongo()

    token_loaded = await token_service.start()

    if settings.upstox_token_required_on_startup and not token_loaded:
        logger.error(
            "Application startup aborted. " "Upstox access token is unavailable."
        )

        await token_service.stop()
        await close_mongo_connection()

        raise RuntimeError("Upstox access token is unavailable.")

    logger.info(
        "%s started token_available=%s",
        settings.app_name,
        token_service.has_access_token(),
    )

    telegram_bot_service.start()
    telegram_bot_service.send_startup_message()

    try:
        yield

    finally:
        telegram_bot_service.stop()

        await token_service.stop()

        await close_mongo_connection()

        logger.info(
            "%s stopped",
            settings.app_name,
        )


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=(
        "Receives isolated EMA alert payloads, "
        "selects instruments, executes orders, "
        "and stores request/execution data."
    ),
    lifespan=lifespan,
)


app.include_router(
    health_router,
)

app.include_router(
    order_requests_router,
    prefix="/api/v1",
)


@app.get(
    "/",
    tags=["Root"],
)
async def root() -> dict[str, str]:
    return {
        "message": settings.app_name,
        "docs": "/docs",
        "health": "/health",
        "order_requests": "/api/v1/order-requests",
    }
