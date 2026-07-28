# app/services/daily_refresh_service.py

import logging

from app.config import settings

from app.database import (
    refresh_token_if_changed,
    token_state,
)

from app.upstox_services.fetch_options import (
    get_options_contracts,
    options_cache,
)

from app.services.options_history_service import (
    batch_history_service,
    options_history_cache,
)

from app.services.indicator_service import (
    indicator_service,
    indicator_cache,
)

from app.services.live_ema_service import (
    live_ema_service,
)

logger = logging.getLogger("uvicorn")


def clear_all_runtime_caches():
    """
    Clears all application runtime caches.
    """

    logger.info("Clearing application caches...")

    options_cache["nearest_expiry"] = None
    options_cache["total_contracts"] = 0
    options_cache["data"] = []

    options_history_cache.clear()

    indicator_cache.clear()

    try:
        live_ema_service.clear()
    except Exception:
        pass

    logger.info("Application caches cleared successfully.")


def sync_token():
    """
    Synchronize token from MongoDB if
    updated_at has changed.
    """

    changed = refresh_token_if_changed()

    if changed:
        logger.info("Detected token change. Token refreshed in memory.")

    if not token_state.access_token:
        raise Exception("Upstox access token not available.")


def calculate_all_option_emas():
    """
    Calculate EMA indicators for all
    option contracts stored in history cache.
    """

    processed_emas = 0

    logger.info("Calculating EMA indicators for all processed contracts...")

    for instrument_key, contract_data in options_history_cache.items():

        trading_symbol = contract_data.get(
            "trading_symbol",
            instrument_key,
        )

        candles = (
            contract_data.get("candles")
            or contract_data.get("candle_data")
            or contract_data.get("data")
            or []
        )

        if not candles:

            logger.warning(
                f"No candles available for "
                f"{trading_symbol}. Skipping EMA calculation."
            )

            continue

        result = indicator_service.process_and_cache_contract_ema(
            trading_symbol=trading_symbol,
            candles=candles,
            ema_short=settings.EMA_SHORT_PERIOD,
            ema_long=settings.EMA_LONG_PERIOD,
        )

        if result:
            processed_emas += 1

    logger.info(f"EMA calculation completed. " f"Processed={processed_emas}")

    return processed_emas


def initialize_live_cache():
    """
    Initialize live EMA cache from
    historical EMA data.
    """

    logger.info("Initializing Live EMA runtime cache...")

    initialized = live_ema_service.initialize_from_historical_cache()

    logger.info(f"Live EMA cache initialized for " f"{initialized} instruments.")

    return initialized


def refresh_market_data():
    """
    Complete market refresh process.

    Flow:

    1. Sync latest token
    2. Clear caches
    3. Load latest option contracts
    4. Load historical candles
    5. Calculate EMA indicators
    6. Initialize live EMA cache

    Called by:

    - Startup
    - Daily scheduler
    """

    logger.info("=" * 80)
    logger.info("STARTING MARKET REFRESH")
    logger.info("=" * 80)

    save_flag = getattr(
        settings,
        "SAVE_OPTIONS_DATA",
        False,
    )

    try:

        # =====================================================
        # STEP 1
        # Sync latest token
        # =====================================================

        sync_token()

        # =====================================================
        # STEP 2
        # Clear caches
        # =====================================================

        clear_all_runtime_caches()

        # =====================================================
        # STEP 3
        # Load latest option contracts
        # =====================================================

        logger.info("Fetching latest option contracts...")

        result = get_options_contracts(
            instrument_key=settings.OPTION_INSTRUMENT_KEY,
            filter_nearest=True,
            save_data=save_flag,
        )

        if not result:

            logger.error("Failed to fetch option contracts.")

            return {
                "status": "error",
                "message": "Failed to fetch option contracts",
            }

        logger.info(
            f"Options Loaded | "
            f"Expiry={options_cache.get('nearest_expiry')} | "
            f"Contracts={options_cache.get('total_contracts')}"
        )

        # =====================================================
        # STEP 4
        # Historical candles
        # =====================================================

        logger.info("Loading historical candles...")

        history_summary = batch_history_service.process_target_options_history(
            min_strike=float(settings.STRIKE_FROM),
            max_strike=float(settings.STRIKE_TO),
            save_files=save_flag,
        )

        logger.info(f"Historical Processing Complete: " f"{history_summary}")

        # =====================================================
        # STEP 5
        # EMA calculations
        # =====================================================

        total_emas = calculate_all_option_emas()

        logger.info(f"EMA Calculation Completed for " f"{total_emas} instruments")

        # =====================================================
        # STEP 6
        # Initialize live cache
        # =====================================================

        initialized_live = initialize_live_cache()

        # =====================================================
        # Summary
        # =====================================================

        summary = {
            "status": "success",
            "nearest_expiry": options_cache.get("nearest_expiry"),
            "total_contracts": options_cache.get("total_contracts"),
            "history_contracts": len(options_history_cache),
            "ema_contracts": len(indicator_cache),
            "live_contracts": initialized_live,
            "token_updated_at": (token_state.updated_at),
        }

        logger.info(f"Market Refresh Completed Successfully: " f"{summary}")

        logger.info("=" * 80)

        return summary

    except Exception as ex:

        logger.exception(f"Market Refresh Failed: {ex}")

        return {
            "status": "error",
            "message": str(ex),
        }
