"""Foundational FastAPI app for home, charts, market history, token
lifecycle, backup extraction, and the live feed / EMA-cross engine."""

import asyncio
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core import config
from core.logger import get_logger
from routes.admin_routes import router as admin_router
from routes.live_routes import router as live_router
from routes.log_routes import router as log_router
from routes.page_routes import router as page_router            # <-- NEW
from routes.upstox_routes import router as upstox_router
from services.backup_data_service import backup_data_service
from services.instrument_registry import instrument_registry
from services.live_broadcast import live_broadcast
from services.live_cross_store import live_cross_store
from services.live_ema_engine import live_ema_engine
from services.live_feed_service import live_feed_service
from services.telegram_service import telegram_service
from services.token_service import token_service
from token_tasks.daily_refresh import run_daily_refresh
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
    if config.DAILY_REFRESH_ENABLED:
        scheduler.add_job(
            run_daily_refresh,
            "cron",
            day_of_week=config.DAILY_REFRESH_DAYS,
            hour=config.DAILY_REFRESH_HOUR,
            minute=config.DAILY_REFRESH_MINUTE,
            id="daily_refresh",
            replace_existing=True,
            misfire_grace_time=300,
        )
    scheduler.start()
    return scheduler


def _wire_cross_handler(loop) -> None:
    def on_cross(entry, cross):
        try:
            live_cross_store.save(entry, cross)
        except Exception:
            logger.exception(
                "Failed persisting live cross for %s", entry["instrument_key"]
            )
        try:
            live_broadcast.publish_sync("crosses", entry, {"cross": cross})
        except Exception:
            logger.exception("Failed broadcasting live cross")

    live_ema_engine.set_cross_handler(on_cross)


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    scheduler = None
    telegram_started = False
    loop = asyncio.get_running_loop()
    live_broadcast.set_loop(loop)

    try:
        telegram_service.send_message(
            "Application Starting",
            "Market engine starting: refreshing token, fetching backup, starting live feed.",
            level="STARTUP",
        )
        await asyncio.to_thread(token_service.refresh_tokens)
        try:
            await asyncio.to_thread(check_upstox_token_validity, False)
        except Exception as exc:
            logger.warning("Startup token check failed (%s).", type(exc).__name__)

        try:
            backup = await asyncio.to_thread(backup_data_service.refresh)
            logger.info("Startup backup: %s", backup)
            await asyncio.to_thread(instrument_registry.load)
        except Exception as exc:
            logger.exception("Startup backup/registry load failed")
            telegram_service.send_message(
                "Backup Load Failed",
                f"{type(exc).__name__}: {exc}",
                level="ERROR",
            )

        live_feed_service.set_loop(loop)
        _wire_cross_handler(loop)
        live_cross_store.reset_all()
        try:
            await asyncio.to_thread(live_feed_service.start)
        except Exception:
            logger.exception("Live feed start failed")

        scheduler = build_token_monitor_scheduler()
        telegram_started = telegram_token_bot.start()

        logger.info(
            "Application startup complete. feed_instruments=%d telegram_polling=%s",
            len(instrument_registry.all()),
            telegram_started,
        )
        telegram_service.send_message(
            "Application Started",
            (
                f"Instruments loaded: {len(instrument_registry.all())}\n"
                f"Live feed running: {live_feed_service.status().get('running')}"
            ),
            level="SUCCESS",
        )
        yield
    finally:
        telegram_service.send_message(
            "Application Stopping",
            "Market engine shutting down.",
            level="INFO",
        )
        try:
            live_feed_service.stop()
        except Exception:
            logger.exception("Live feed stop failed")
        if telegram_started:
            telegram_token_bot.stop()
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)
        telegram_service.send_shutdown_message(
            details="Application shutdown completed."
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

app.include_router(page_router)             # <-- NEW
app.include_router(live_router)
app.include_router(admin_router)
app.include_router(upstox_router)
app.include_router(log_router)