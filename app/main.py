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

# Route imports
from app.routes.home_routes import router as home_router
from app.routes.options_routes import router as options_router
from app.routes.ema_routes import router as ema_router

# Options cache, batch history & indicator imports
from app.upstox_options.fetch_options import get_options_contracts, options_cache
from app.services.options_history_service import (
    batch_history_service,
    options_history_cache,
)
from app.services.indicator_service import indicator_service

logger = logging.getLogger("uvicorn")


def init_logging():
    """Ensure directory exists and initialize logger config."""
    os.makedirs(settings.LOG_DIR, exist_ok=True)
    logging.basicConfig(level=settings.LOG_LEVEL)


def init_database():
    """Synchronous MongoDB initialization steps."""
    connect_to_mongo()
    load_upstox_token()


def init_options_cache():
    """
    Fetches nearest options contracts at startup, populates in-memory cache,
    runs multi-threaded historical candle cross-checking, and calculates/stores
    EMA indicators directly in project memory cache.
    """
    logger.info("Initializing options contracts cache...")
    save_flag = getattr(settings, "SAVE_OPTIONS_DATA", False)

    result = get_options_contracts(
        instrument_key=getattr(settings, "OPTION_INSTRUMENT_KEY", "NSE_INDEX|Nifty 50"),
        filter_nearest=True,
        save_data=save_flag,
    )

    if result:
        logger.info(
            f"Options cache successfully populated! "
            f"Expiry: {options_cache.get('nearest_expiry')} | "
            f"Contracts: {options_cache.get('total_contracts')}"
        )

        # Step 1: Trigger batch history loading and candle processing
        logger.info("Starting historical candle fetch & memory cache pipeline...")
        history_summary = batch_history_service.process_target_options_history(
            min_strike=float(getattr(settings, "STRIKE_FROM", 23000)),
            max_strike=float(getattr(settings, "STRIKE_TO", 25000)),
            save_files=save_flag,
        )
        logger.info(f"History pipeline completed: {history_summary}")

        # Step 2: Calculate and store EMA indicators in memory cache for each contract
        logger.info("Calculating EMA indicators for cached option instruments...")
        processed_emas = 0

        for key, contract_data in options_history_cache.items():
            trading_symbol = contract_data.get("trading_symbol", key)

            # Check multiple potential keys where candles might be stored in contract_data
            candles = (
                contract_data.get("candles")
                or contract_data.get("candle_data")
                or contract_data.get("data")
                or []
            )

            if candles:
                indicator_service.process_and_cache_contract_ema(
                    trading_symbol=trading_symbol,
                    candles=candles,
                    ema_short=9,
                    ema_long=21,
                )
                processed_emas += 1

        logger.info(
            f"Successfully calculated and cached EMAs in memory for {processed_emas} instruments!"
        )

    else:
        logger.warning("Failed to populate options contracts cache at startup.")


def load_routes(app: FastAPI):
    """Includes API routers into the FastAPI application."""
    app.include_router(home_router)
    app.include_router(options_router)
    app.include_router(ema_router)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # App startup sequence
    init_logging()
    init_database()
    init_options_cache()
    yield
    # App shutdown sequence
    close_mongo_connection()


# Initialize FastAPI app
app = FastAPI(title="Upstox FastAPI Service", lifespan=lifespan)

# Attach routes
load_routes(app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=True,
    )