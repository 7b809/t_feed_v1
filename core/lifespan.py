"""
core/lifespan.py

Owns the FastAPI startup / shutdown sequence.

Startup order:
    1. Register the running event loop with ws_manager (fanout).
    2. Connect to MongoDB.
    3. Load the token into memory.
    4. Prepare the option runtime folder.
    5. Hydrate option cache from runtime snapshots (if fresh).
    6. Start the 30-min token refresh scheduler.
    7. Auto-connect streamers + subscribe enabled indexes.
    8. Load option contracts (per OPTIONS_LOAD_* flags).
    9. Load historical + intraday candles for every option contract (readonly, write-once).
   10. Start the daily candle refresh scheduler (09:00 IST by default).
   11. If OPTIONS_SUBSCRIBE_ALL_ON_STARTUP=true, bulk-subscribe option keys.

main.py just hands this to FastAPI — no business logic lives here.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from core.config import core_config, mongo_manager
from core.logger import get_logger
from token_tasks import scheduler
from token_tasks.config import token_config
from token_tasks.service import token_service
from upstox_app.candle.candle_scheduler import candle_scheduler
from upstox_app.candle.candle_service import candle_service
from upstox_app.common.config import upstox_config
from upstox_app.option.option_service import hydrate_from_runtime, load_enabled_indexes
from upstox_app.option.option_storage import option_storage
from upstox_app.streamer.streamer_manager import (
    auto_connect_enabled,
    index_subscription_enabled,
    option_subscription_enabled,
    start_all,
    stop_all,
    subscribe_enabled_indexes,
    subscribe_option_contracts,
)
from upstox_app.streamer.ws_manager import set_event_loop

logger = get_logger(__name__)


@asynccontextmanager
async def app_lifespan(_app) -> AsyncIterator[None]:
    logger.info("=" * 60)
    logger.info("Starting %s v%s", core_config.APP_NAME, core_config.APP_VERSION)

    # 0) Register event loop for thread-safe WS fanout
    set_event_loop(asyncio.get_running_loop())

    # 1) MongoDB
    try:
        mongo_manager.connect()
        if mongo_manager.ping():
            logger.info("MongoDB connected successfully")
        else:
            logger.error("MongoDB ping failed")
    except Exception as exc:  # noqa: BLE001
        logger.exception("MongoDB connection failed: %s", exc)

    # 2) Token load
    if token_config.LOAD_ON_STARTUP:
        try:
            await asyncio.to_thread(token_service.load_token)
            logger.info("Initial token load completed")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Initial token load failed: %s", exc)
    else:
        logger.info("Skipping startup token load")

    # 3) Option runtime folder
    try:
        option_storage.ensure_dirs()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Option storage init failed: %s", exc)

    # 4) Hydrate option cache from runtime snapshots
    try:
        hydrated = await asyncio.to_thread(hydrate_from_runtime)
        logger.info("Option cache hydrated from disk | indexes=%d", hydrated)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Runtime hydration failed: %s", exc)

    # 5) Token refresh scheduler
    try:
        scheduler.start_scheduler()
        logger.info("Token refresh scheduler started")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to start token refresh scheduler: %s", exc)

    # 6) Streamers
    if auto_connect_enabled():
        try:
            logger.info("Auto-connecting streamers on startup")
            start_result = await asyncio.to_thread(start_all)
            logger.info("Streamer start result | %s", start_result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to auto-connect streamers: %s", exc)

    if index_subscription_enabled():
        try:
            sub_result = await asyncio.to_thread(subscribe_enabled_indexes)
            logger.info("Index subscription result | %s", sub_result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to subscribe enabled indexes: %s", exc)

    # 7) Option chains
    if upstox_config.OPTIONS_LOAD_ON_STARTUP:
        try:
            logger.info(
                "Loading option contracts for enabled indexes | all_expiries=%s",
                upstox_config.OPTIONS_LOAD_ALL_EXPIRIES,
            )
            result = await asyncio.to_thread(load_enabled_indexes)
            logger.info("Option load result | %s", result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Option load on startup failed: %s", exc)
    else:
        logger.info("Option load on startup disabled")

    # 8) Candles (readonly, write-once, double-check missing per contract)
    if upstox_config.CANDLES_ENABLED and upstox_config.CANDLES_LOAD_ON_STARTUP:
        try:
            logger.info(
                "Loading candles for enabled indexes | days=%s unit=%s interval=%s intraday=%s",
                upstox_config.CANDLES_DAYS,
                upstox_config.CANDLES_UNIT,
                upstox_config.CANDLES_INTERVAL,
                upstox_config.CANDLES_INCLUDE_INTRADAY,
            )
            candle_summary = await asyncio.to_thread(candle_service.ensure_all_enabled)
            for name, res in candle_summary.get("results", {}).items():
                logger.info(
                    "Candles | index=%s | total=%d | present=%d | missing_before=%d | fetched=%d | failed=%d",
                    name,
                    res.get("total_contracts", 0),
                    res.get("already_present", 0),
                    res.get("missing_before", 0),
                    res.get("fetched", 0),
                    res.get("failed", 0),
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Candle load on startup failed: %s", exc)
    else:
        logger.info("Candle load on startup disabled")

    # 9) Start the daily candle refresh scheduler (fires at CANDLES_DAILY_REFRESH_TIME IST)
    try:
        candle_scheduler.start()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to start candle daily refresh scheduler: %s", exc)

    # 10) Bulk subscribe fetched option keys (gated)
    if option_subscription_enabled():
        try:
            logger.info("Bulk-subscribing option contracts on startup")
            sub_result = await asyncio.to_thread(subscribe_option_contracts)
            logger.info("Option subscription result | %s", sub_result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Option subscription on startup failed: %s", exc)
    else:
        logger.info("Option bulk-subscribe on startup disabled")

    logger.info("Startup complete — app is ready")
    logger.info("=" * 60)

    try:
        yield
    finally:
        logger.info("Shutting down application...")

        # Stop the daily candle refresh scheduler first, before the
        # streamers, so no new refresh task can start mid-shutdown.
        try:
            await candle_scheduler.stop()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error stopping candle daily refresh scheduler: %s", exc)

        try:
            stop_result = await asyncio.to_thread(stop_all)
            logger.info("Streamers stopped | %s", stop_result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error stopping streamers: %s", exc)

        try:
            await scheduler.stop_scheduler()
            logger.info("Token refresh scheduler stopped")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error stopping scheduler: %s", exc)

        try:
            mongo_manager.close()
            logger.info("MongoDB connection closed")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error closing MongoDB connection: %s", exc)

        logger.info("Shutdown complete")