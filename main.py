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

from services.telegram_bot_service import telegram_bot_service

logger = get_logger("app")


@asynccontextmanager
async def lifespan(_: FastAPI):

    # Start MongoDB connection.
    await connect_to_mongo()

    logger.info(
        "%s started",
        settings.app_name,
    )

    # Start Telegram bot.
    telegram_bot_service.start()

    # Send application startup message.
    telegram_bot_service.send_startup_message()

    yield

    # Stop Telegram bot.
    telegram_bot_service.stop()

    # Close MongoDB connection.
    await close_mongo_connection()

    logger.info(
        "%s stopped",
        settings.app_name,
    )


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=("Receives isolated EMA alert payloads " "and stores them in MongoDB."),
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
