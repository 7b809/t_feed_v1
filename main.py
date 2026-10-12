"""Foundational FastAPI app for home, charts, market history and token lifecycle."""

import asyncio
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.candles_routes import router as candles_router
from api.chart_routes import router as chart_router
from api.chart_websocket_routes import router as chart_websocket_router
from api.health_routes import router as health_router
from api.home_routes import router as home_router
from api.logs_api import router as logs_router
from api.token_store_routes import router as token_router
from core import config
from core.logger import get_logger
from services.telegram_service import telegram_service
from services.token_service import token_service
from token_tasks.telegram_token_bot import telegram_token_bot
from token_tasks.token_monitor import check_upstox_token_validity

logger = get_logger(__file__)


def build_token_monitor_scheduler(scheduler_class=BackgroundScheduler):
    scheduler = scheduler_class(timezone=config.MARKET_TIMEZONE)
    scheduler.add_job(
        check_upstox_token_validity,
        "interval",
        minutes=config.UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES,
        id="token_lifecycle_check",
        replace_existing=True,
        kwargs={"send_success_message": False},
    )
    scheduler.start()
    return scheduler


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    scheduler = None
    telegram_started = False
    try:
        await asyncio.to_thread(token_service.refresh_tokens)
        try:
            await asyncio.to_thread(check_upstox_token_validity, False)
        except Exception as exc:
            logger.warning("Startup token check failed (%s).", type(exc).__name__)

        scheduler = build_token_monitor_scheduler()
        telegram_started = telegram_token_bot.start()
        logger.info(
            "Application startup complete. token_monitor_interval_minutes=%s, telegram_polling=%s",
            config.UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES,
            telegram_started,
        )
        yield
    finally:
        if telegram_started:
            telegram_token_bot.stop()
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)
        telegram_service.send_shutdown_message(details="Foundational application shutdown completed.")
        logger.info("Application shutdown complete.")


app = FastAPI(title="Market Data Foundation", lifespan=app_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(home_router)
app.include_router(logs_router)
app.include_router(health_router)
app.include_router(chart_router)
app.include_router(chart_websocket_router)
app.include_router(candles_router)
app.include_router(token_router)

