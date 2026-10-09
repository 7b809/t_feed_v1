from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import settings
from app.core.logger import get_logger
from app.services.ema_cross_store import ema_cross_store
from app.services.historical_candle_store import historical_candle_store
from app.services.live_ema_cross_store import live_ema_cross_store
from app.services.subscription_store import subscription_store

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown."""

    # ---- Job 1: ordered, de-duplicated subscriptions from upstream -----
    try:
        await subscription_store.refresh(reason="application-startup")
    except Exception:
        logger.warning("Application started without fresh subscription data")

    instruments = subscription_store.snapshot().get("active", [])

    # ---- Job 2: last N days of candles --------------------------------
    try:
        summary = await historical_candle_store.ensure_recent_candles(
            instruments, reason="application-startup"
        )
        logger.info("Startup historical job summary: %s", summary)
    except Exception:
        logger.warning("Historical candle job did not complete at startup")

    # ---- Job 3: today's intraday candles ------------------------------
    try:
        summary = await historical_candle_store.ensure_intraday_candles(
            instruments, reason="application-startup"
        )
        logger.info("Startup intraday job summary: %s", summary)
    except Exception:
        logger.warning("Intraday candle job did not complete at startup")

    # ---- Job 4: batch EMA crosses -------------------------------------
    try:
        summary = await ema_cross_store.compute_all(
            instruments, reason="application-startup"
        )
        logger.info("Startup EMA cross job summary: %s", summary)
    except Exception:
        logger.warning("EMA cross job did not complete at startup")

    # ---- Job 5: live EMA cross polling --------------------------------
    if settings.live_ema_enabled:
        try:
            await live_ema_cross_store.start(
                lambda: subscription_store.snapshot().get("active", [])
            )
        except Exception:
            logger.exception("Live EMA cross job failed to start")
    else:
        logger.info("Live EMA cross job disabled via LIVE_EMA_ENABLED")

    yield

    # ---- Shutdown -----------------------------------------------------
    if settings.live_ema_enabled:
        try:
            await live_ema_cross_store.stop()
        except Exception:
            logger.exception("Live EMA cross job failed to stop cleanly")

    logger.info("Application shutdown completed")