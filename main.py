import asyncio
import json
import uvicorn
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core import config
from core.logger import get_logger
from services.option_service import get_options_contracts, options_cache
from services.token_service import token_service
from services.upstox_websocket import upstox_streamer
from services.telegram_service import telegram_service
from services.history_service import fetch_historical_candles_for_all_subscribed
from services.opening_range_service import calculate_opening_range_for_all_subscribed

from api.home_routes import router as home_router
from api.health_routes import router as health_router
from api.debug_routes import router as debug_router
from api.refresh_routes import router as refresh_router
from api.history_routes import router as history_router
from api.opening_range_routes import router as opening_range_router
from api.ws_docs_routes import router as ws_docs_router

from ws_feed.websocket_routes import router as websocket_router

logger = get_logger(__file__)

DASHBOARD_TEMPLATE_PATH = Path("templates/isolated_ema_dashboard.html")


# ============================================================
# Live EMA Mode Helper
# ============================================================


def get_live_ema_calculation_mode_text() -> str:
    """
    Returns configured live EMA calculation mode.

    LIVE_EMA_CALCULATION_MODE = False
        completed candle close based EMA calculation.

    LIVE_EMA_CALCULATION_MODE = True
        live tick/LTP based EMA calculation.
    """

    return (
        "tick_ltp"
        if bool(getattr(config, "LIVE_EMA_CALCULATION_MODE", False))
        else "candle_close"
    )


def get_live_ema_calculation_mode_description() -> str:
    """Returns readable live EMA mode description."""

    if bool(getattr(config, "LIVE_EMA_CALCULATION_MODE", False)):
        return "live tick/LTP based EMA calculation"

    return "completed candle close based EMA calculation"


def load_and_subscribe_instruments():
    """Fetches option contracts, updates memory cache, and logs subscription details."""
    logger.info("Executing contract load and subscription workflow...")

    result = get_options_contracts(save_data=True)

    if not result:
        logger.error("Failed to load option contracts for subscription.")

        telegram_service.send_instruments_fetched_message(
            success=False,
            error="Failed to load option contracts for subscription.",
        )

        return None

    subscribed_keys = options_cache.get("subscribed_keys", [])

    logger.info("================ SUBSCRIPTION SUMMARY ================")
    logger.info(
        f"Main Security: {getattr(config, 'MAIN_NIFTY_SECURITY', 'NSE_INDEX|Nifty 50')}"
    )
    logger.info(
        f"Strike Price Range: {getattr(config, 'STRIKE_FROM', 'N/A')} "
        f"to {getattr(config, 'STRIKE_TO', 'N/A')}"
    )
    logger.info(
        f"Total Subscribed Instruments (Index + Options): {len(subscribed_keys)}"
    )
    logger.info("=======================================================")

    telegram_service.send_instruments_fetched_message(
        success=True,
        nearest_expiry=options_cache.get("nearest_expiry"),
        total_contracts=options_cache.get("total_contracts", 0),
        subscribed_keys_count=len(subscribed_keys),
        strike_from=getattr(config, "STRIKE_FROM", "N/A"),
        strike_to=getattr(config, "STRIKE_TO", "N/A"),
    )

    return result


def fetch_startup_historical_candles():
    """
    Fetches historical candles and calculates EMA crossover during startup.

    Current flow:
    - Live EMA runs for all subscribed instruments.
    - EMA calculation mode is controlled by LIVE_EMA_CALCULATION_MODE.
    - Every live EMA crossover can be broadcast through WebSocket.
    - Telegram EMA alerts are restricted to the isolated Opening Range instrument only.
    """

    logger.info(
        "================ STARTUP HISTORICAL EMA CROSSOVER LOAD STARTED ================"
    )

    try:
        subscribed_keys = options_cache.get("subscribed_keys", [])

        if not subscribed_keys:
            warning_message = (
                "Startup historical EMA crossover fetch skipped. "
                "No subscribed instruments found."
            )

            logger.warning(warning_message)

            telegram_service.send_message(
                title="Startup Historical EMA Fetch Skipped",
                message=warning_message,
                level="WARNING",
            )

            return None

        history_summary = fetch_historical_candles_for_all_subscribed(
            interval=getattr(config, "HISTORICAL_CANDLE_INTERVAL", "1minute"),
            history_days=getattr(config, "HISTORICAL_CANDLE_DAYS", 10),
            save_data=True,
            max_workers=getattr(config, "HISTORICAL_CANDLE_MAX_WORKERS", 5),
        )

        logger.info(
            f"Startup historical EMA crossover fetch completed. "
            f"status={history_summary.get('status')}, "
            f"total_instruments={history_summary.get('total_instruments')}, "
            f"success={history_summary.get('success_count')}, "
            f"empty={history_summary.get('empty_count')}, "
            f"insufficient_data={history_summary.get('insufficient_data_count')}, "
            f"failed={history_summary.get('failed_count')}, "
            f"total_candles={history_summary.get('total_candles')}, "
            f"live_ema_initialized={history_summary.get('live_ema_initialized')}, "
            f"live_ema_calculation_mode={get_live_ema_calculation_mode_text()}"
        )

        telegram_service.send_message(
            title="Startup Historical EMA Crossover Fetch Completed",
            message=(
                f"Status: {history_summary.get('status')}\n"
                f"From Date: {history_summary.get('from_date')}\n"
                f"To Date: {history_summary.get('to_date')}\n"
                f"Interval: {history_summary.get('interval')}\n"
                f"Total Instruments: {history_summary.get('total_instruments')}\n"
                f"Success: {history_summary.get('success_count')}\n"
                f"Empty: {history_summary.get('empty_count')}\n"
                f"Insufficient Data: {history_summary.get('insufficient_data_count')}\n"
                f"Failed: {history_summary.get('failed_count')}\n"
                f"Total Candles: {history_summary.get('total_candles')}\n"
                f"EMA Fast Period: {history_summary.get('ema_fast_period')}\n"
                f"EMA Slow Period: {history_summary.get('ema_slow_period')}\n"
                f"EMA Result File: {history_summary.get('ema_results_file_path', 'not_saved')}\n"
                f"Live EMA Initialized: {history_summary.get('live_ema_initialized')}\n"
                f"Live EMA Calculation Mode: {get_live_ema_calculation_mode_text()}\n"
                f"Live EMA Mode Description: {get_live_ema_calculation_mode_description()}\n"
                f"Telegram EMA Alert Scope: isolated instrument only"
            ),
            level="INFO",
        )

        return history_summary

    except Exception as ex:
        logger.error(
            f"Startup historical EMA crossover fetch failed: {type(ex).__name__}: {ex}"
        )

        telegram_service.send_exception_message(
            title="Startup Historical EMA Crossover Fetch Failed",
            exception=ex,
            context="fetch_startup_historical_candles",
        )

        return None

    finally:
        logger.info(
            "================ STARTUP HISTORICAL EMA CROSSOVER LOAD COMPLETED ================"
        )


def fetch_daily_historical_candles():
    """
    Fetches historical candles and calculates EMA crossover during daily hard refresh.

    Current flow:
    - Live EMA state is maintained for all subscribed instruments.
    - EMA calculation mode is controlled by LIVE_EMA_CALCULATION_MODE.
    - EMA crossover WebSocket events are broadcast for all instruments.
    - Telegram EMA alerts are sent only for the isolated Opening Range instrument.
    """

    logger.info(
        "================ DAILY HISTORICAL EMA CROSSOVER LOAD STARTED ================"
    )

    try:
        subscribed_keys = options_cache.get("subscribed_keys", [])

        if not subscribed_keys:
            warning_message = (
                "Daily historical EMA crossover fetch skipped. "
                "No subscribed instruments found."
            )

            logger.warning(warning_message)

            telegram_service.send_message(
                title="Daily Historical EMA Fetch Skipped",
                message=warning_message,
                level="WARNING",
            )

            return None

        history_summary = fetch_historical_candles_for_all_subscribed(
            interval=getattr(config, "HISTORICAL_CANDLE_INTERVAL", "1minute"),
            history_days=getattr(config, "HISTORICAL_CANDLE_DAYS", 10),
            save_data=True,
            max_workers=getattr(config, "HISTORICAL_CANDLE_MAX_WORKERS", 5),
        )

        logger.info(
            f"Daily historical EMA crossover fetch completed. "
            f"status={history_summary.get('status')}, "
            f"total_instruments={history_summary.get('total_instruments')}, "
            f"success={history_summary.get('success_count')}, "
            f"empty={history_summary.get('empty_count')}, "
            f"insufficient_data={history_summary.get('insufficient_data_count')}, "
            f"failed={history_summary.get('failed_count')}, "
            f"total_candles={history_summary.get('total_candles')}, "
            f"live_ema_initialized={history_summary.get('live_ema_initialized')}, "
            f"live_ema_calculation_mode={get_live_ema_calculation_mode_text()}"
        )

        telegram_service.send_message(
            title="Daily Historical EMA Crossover Fetch Completed",
            message=(
                f"Status: {history_summary.get('status')}\n"
                f"From Date: {history_summary.get('from_date')}\n"
                f"To Date: {history_summary.get('to_date')}\n"
                f"Interval: {history_summary.get('interval')}\n"
                f"Total Instruments: {history_summary.get('total_instruments')}\n"
                f"Success: {history_summary.get('success_count')}\n"
                f"Empty: {history_summary.get('empty_count')}\n"
                f"Insufficient Data: {history_summary.get('insufficient_data_count')}\n"
                f"Failed: {history_summary.get('failed_count')}\n"
                f"Total Candles: {history_summary.get('total_candles')}\n"
                f"EMA Fast Period: {history_summary.get('ema_fast_period')}\n"
                f"EMA Slow Period: {history_summary.get('ema_slow_period')}\n"
                f"EMA Result File: {history_summary.get('ema_results_file_path', 'not_saved')}\n"
                f"Live EMA Initialized: {history_summary.get('live_ema_initialized')}\n"
                f"Live EMA Calculation Mode: {get_live_ema_calculation_mode_text()}\n"
                f"Live EMA Mode Description: {get_live_ema_calculation_mode_description()}\n"
                f"Telegram EMA Alert Scope: isolated instrument only"
            ),
            level="REFRESH",
        )

        return history_summary

    except Exception as ex:
        logger.error(
            f"Daily historical EMA crossover fetch failed: {type(ex).__name__}: {ex}"
        )

        telegram_service.send_exception_message(
            title="Daily Historical EMA Crossover Fetch Failed",
            exception=ex,
            context="fetch_daily_historical_candles",
        )

        return None

    finally:
        logger.info(
            "================ DAILY HISTORICAL EMA CROSSOVER LOAD COMPLETED ================"
        )


def run_daily_opening_range_fetch():
    """
    Scheduled opening range workflow.

    Current flow:
    - Opening Range levels are maintained for every subscribed instrument.
    - Live EMA continues for all instruments.
    - EMA calculation mode is controlled by LIVE_EMA_CALCULATION_MODE.
    - One instrument can be isolated based on R3/S3 priority, then R2/S2,
      nearest to Opening Range average.
    - Telegram EMA alerts are sent only for the isolated instrument.
    """

    logger.info("================ DAILY OPENING RANGE FETCH STARTED ================")

    telegram_service.send_message(
        title="Daily Opening Range Fetch Started",
        message=(
            "Opening range fetch started.\n\n"
            "Actions:\n"
            "1. Read subscribed instruments\n"
            "2. Fetch today's intraday candles\n"
            "3. Select opening range candles from market open\n"
            "4. Calculate open, high, low, close, average\n"
            "5. Calculate R1/S1, R2/S2, R3/S3 and thresholds\n"
            "6. Backfill scan for R2/R3/S2/S3 touches before fetch time\n"
            "7. Isolate one instrument if eligible touch exists\n"
            "8. Save opening range results locally\n"
            "9. Update in-memory opening range cache for EMA WebSocket enrichment\n\n"
            "Isolation Rule:\n"
            "- Use Opening Range average +/- configured window\n"
            "- Clamp inside configured strike range\n"
            "- R3/S3 priority before R2/S2\n"
            "- If multiple qualify, choose nearest strike to average\n"
            "- EMA Telegram alerts only for isolated instrument\n\n"
            f"Live EMA Calculation Mode: {get_live_ema_calculation_mode_text()}\n"
            f"Live EMA Mode Description: {get_live_ema_calculation_mode_description()}"
        ),
        level="REFRESH",
    )

    try:
        subscribed_keys = options_cache.get("subscribed_keys", [])

        if not subscribed_keys:
            warning_message = (
                "Opening range fetch skipped. "
                "No subscribed instruments found in options_cache."
            )

            logger.warning(warning_message)

            telegram_service.send_message(
                title="Opening Range Fetch Skipped",
                message=warning_message,
                level="WARNING",
            )

            return None

        opening_range_summary = calculate_opening_range_for_all_subscribed(
            candle_count=getattr(config, "OPENING_RANGE_CANDLE_COUNT", 1),
            save_data=getattr(config, "OPENING_RANGE_SAVE_FILE", True),
            max_workers=getattr(config, "OPENING_RANGE_MAX_WORKERS", 5),
        )

        isolated_state = opening_range_summary.get("isolated_instrument") or {}
        isolated_selected = bool(isolated_state.get("selected"))

        logger.info(
            f"Daily opening range fetch completed. "
            f"status={opening_range_summary.get('status')}, "
            f"total_instruments={opening_range_summary.get('total_instruments')}, "
            f"success={opening_range_summary.get('success_count')}, "
            f"empty={opening_range_summary.get('empty_count')}, "
            f"insufficient_data={opening_range_summary.get('insufficient_data_count')}, "
            f"failed={opening_range_summary.get('failed_count')}, "
            f"backfill_touch_events={opening_range_summary.get('backfill_touch_events_count', 0)}, "
            f"latest_main_index_ltp={opening_range_summary.get('latest_main_index_ltp')}, "
            f"isolated_selected={isolated_selected}, "
            f"isolated_instrument_key={isolated_state.get('instrument_key')}, "
            f"isolated_level={isolated_state.get('selected_level')}, "
            f"live_ema_calculation_mode={get_live_ema_calculation_mode_text()}, "
            f"output_file={opening_range_summary.get('output_file_path', 'not_saved')}"
        )

        telegram_service.send_message(
            title="Daily Opening Range Fetch Completed",
            message=(
                f"Status: {opening_range_summary.get('status')}\n"
                f"Date: {opening_range_summary.get('date')}\n"
                f"Source: {opening_range_summary.get('source')}\n"
                f"Interval: {opening_range_summary.get('interval')}\n"
                f"Intraday Unit: {opening_range_summary.get('unit')}\n"
                f"Intraday Interval: {opening_range_summary.get('intraday_interval')}\n"
                f"Opening Range Candles: {opening_range_summary.get('opening_range_candle_count')}\n"
                f"Market Open Time: {opening_range_summary.get('market_open_time')}\n"
                f"Opening Range End Time: {opening_range_summary.get('opening_range_end_time')}\n"
                f"Scheduled Fetch Time: {opening_range_summary.get('scheduled_fetch_time')}\n"
                f"Total Instruments: {opening_range_summary.get('total_instruments')}\n"
                f"Success: {opening_range_summary.get('success_count')}\n"
                f"Empty: {opening_range_summary.get('empty_count')}\n"
                f"Insufficient Data: {opening_range_summary.get('insufficient_data_count')}\n"
                f"Failed: {opening_range_summary.get('failed_count')}\n"
                f"Backfill Touch Events: {opening_range_summary.get('backfill_touch_events_count', 0)}\n"
                f"Latest NIFTY LTP: {opening_range_summary.get('latest_main_index_ltp')}\n"
                f"Isolated Instrument Selected: {isolated_selected}\n"
                f"Isolated Instrument Key: {isolated_state.get('instrument_key') if isolated_selected else 'not_selected'}\n"
                f"Isolated Level: {isolated_state.get('selected_level') if isolated_selected else 'not_available'}\n"
                f"Isolated EMA Alerts Count: {opening_range_summary.get('isolated_ema_alerts_count', 0)}\n"
                f"Live EMA Calculation Mode: {get_live_ema_calculation_mode_text()}\n"
                f"Live EMA Mode Description: {get_live_ema_calculation_mode_description()}\n"
                f"Output File: {opening_range_summary.get('output_file_path', 'not_saved')}"
            ),
            level="REFRESH",
        )

        return opening_range_summary

    except Exception as ex:
        logger.error(f"Daily opening range fetch failed: {type(ex).__name__}: {ex}")

        telegram_service.send_exception_message(
            title="Daily Opening Range Fetch Failed",
            exception=ex,
            context="run_daily_opening_range_fetch",
        )

        return None

    finally:
        logger.info(
            "================ DAILY OPENING RANGE FETCH COMPLETED ================"
        )


def run_initial_startup():
    """Initial synchronous load when the application starts up."""
    logger.info("Initializing application startup sequence...")

    telegram_service.send_startup_message(
        status="started",
        details=(
            "Initial startup sequence started. "
            "Refreshing token, loading instruments, calculating historical EMA crossover, "
            "and initializing live EMA state for all subscribed instruments. "
            "Opening Range isolated instrument selection will run after Opening Range fetch.\n\n"
            f"Live EMA Calculation Mode: {get_live_ema_calculation_mode_text()}\n"
            f"Live EMA Mode Description: {get_live_ema_calculation_mode_description()}"
        ),
    )

    try:
        token_service.refresh_tokens()

        current_token = token_service.get_access_token()
        doc = token_service.get_token_document()

        if current_token:
            telegram_service.send_token_refresh_message(
                success=True,
                updated_at=doc.get("updated_at") if doc else "N/A",
            )
        else:
            telegram_service.send_token_refresh_message(
                success=False,
                error="No access token found after MongoDB token refresh.",
            )

        result = load_and_subscribe_instruments()

        history_summary = None

        if result:
            history_summary = fetch_startup_historical_candles()
        else:
            logger.warning(
                "Skipping startup historical EMA crossover fetch because instrument load failed."
            )

        cached_data = options_cache.get("data", [])
        sample_contract = cached_data[0] if cached_data else None

        logger.info("=== Memory Cache State Summary ===")
        logger.info(
            f"Access Token: {current_token[:15]}..." if current_token else "No Token"
        )
        logger.info(f"Token Updated At: {doc.get('updated_at') if doc else 'N/A'}")
        logger.info(f"Nearest Expiry in Cache: {options_cache.get('nearest_expiry')}")
        logger.info(f"Total Cached Contracts: {options_cache.get('total_contracts')}")

        if sample_contract:
            logger.info(f"Sample Contract:\n{json.dumps(sample_contract, indent=2)}")
        else:
            logger.info("Sample Contract: None (Cache Empty)")

        subscribed_keys = options_cache.get("subscribed_keys", [])

        websocket_mode = (
            "EMA WebSocket events will include Opening Range levels when available."
            if getattr(config, "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS", True)
            else "EMA WebSocket events will be sent without Opening Range level enrichment."
        )

        isolated_flow = (
            "Enabled. After Opening Range levels are ready, the system monitors "
            "eligible R2/R3/S2/S3 touches and isolates one instrument for EMA Telegram alerts."
            if getattr(config, "OPENING_RANGE_ISOLATED_INSTRUMENT_ENABLED", True)
            else "Disabled."
        )

        startup_details = (
            f"Token Available: {'Yes' if current_token else 'No'}\n"
            f"Token Updated At: {doc.get('updated_at') if doc else 'N/A'}\n"
            f"Nearest Expiry: {options_cache.get('nearest_expiry')}\n"
            f"Total Contracts: {options_cache.get('total_contracts')}\n"
            f"Subscribed Instruments: {len(subscribed_keys)}\n"
            f"Historical EMA Status: "
            f"{history_summary.get('status') if history_summary else 'not_available'}\n"
            f"Historical Total Candles: "
            f"{history_summary.get('total_candles') if history_summary else 0}\n"
            f"EMA Result File: "
            f"{history_summary.get('ema_results_file_path', 'not_saved') if history_summary else 'not_available'}\n"
            f"Live EMA Initialized: "
            f"{history_summary.get('live_ema_initialized') if history_summary else 'not_available'}\n"
            f"Live EMA Calculation Mode: {get_live_ema_calculation_mode_text()}\n"
            f"Live EMA Mode Description: {get_live_ema_calculation_mode_description()}\n"
            f"EMA WebSocket Mode: {websocket_mode}\n"
            f"Opening Range Isolated Instrument Flow: {isolated_flow}\n"
            f"EMA Telegram Alert Scope: isolated instrument only\n"
            f"Isolated EMA Dashboard: /isolated-dashboard"
        )

        if result and current_token and subscribed_keys:
            telegram_service.send_startup_message(
                status="completed successfully",
                details=startup_details,
            )
        else:
            telegram_service.send_startup_message(
                status="completed with warnings",
                details=startup_details,
            )

    except Exception as ex:
        logger.error(f"Initial startup sequence failed: {type(ex).__name__}: {ex}")

        telegram_service.send_exception_message(
            title="Initial Startup Failed",
            exception=ex,
            context="run_initial_startup",
        )

        raise


def run_daily_market_hard_refresh():
    """
    Daily hard refresh workflow.

    Current flow:
    - EMA crossover events are produced for all subscribed instruments.
    - EMA calculation mode is controlled by LIVE_EMA_CALCULATION_MODE.
    - Opening Range later isolates one instrument based on R2/R3/S2/S3 touch logic.
    - Telegram EMA alerts are sent only for that isolated instrument.
    """

    logger.info("================ DAILY MARKET HARD REFRESH STARTED ================")

    telegram_service.send_message(
        title="Daily Market Hard Refresh Started",
        message=(
            "Daily hard refresh started.\n\n"
            "Actions:\n"
            "1. Refresh token from MongoDB\n"
            "2. Fetch latest instruments\n"
            "3. Filter configured strike range\n"
            "4. Update subscription cache\n"
            "5. Fetch historical candles and calculate EMA crossover\n"
            "6. Initialize live EMA state for all instruments\n"
            "7. Restart Upstox streamer\n\n"
            "Note: EMA calculation runs for all instruments. "
            "Telegram EMA alerts are restricted to the isolated Opening Range instrument.\n\n"
            f"Live EMA Calculation Mode: {get_live_ema_calculation_mode_text()}\n"
            f"Live EMA Mode Description: {get_live_ema_calculation_mode_description()}"
        ),
        level="REFRESH",
    )

    refresh_success = False

    try:
        logger.info("Refreshing token document from MongoDB...")
        token_service.refresh_tokens()

        current_token = token_service.get_access_token()
        doc = token_service.get_token_document()

        if not current_token:
            error_message = "Daily hard refresh failed: No access token available."
            logger.error(error_message)

            telegram_service.send_token_refresh_message(
                success=False,
                error=error_message,
            )

            telegram_service.send_daily_refresh_message(
                success=False,
                error=error_message,
            )

            return

        logger.info("Token refreshed into memory successfully.")

        telegram_service.send_token_refresh_message(
            success=True,
            updated_at=doc.get("updated_at") if doc else "N/A",
        )

        logger.info(
            "Fetching latest option contracts and rebuilding subscription keys..."
        )

        result = load_and_subscribe_instruments()

        if not result:
            error_message = (
                "Daily hard refresh failed: Instrument fetch returned no result."
            )
            logger.error(error_message)

            telegram_service.send_daily_refresh_message(
                success=False,
                error=error_message,
            )

            return

        subscribed_keys = options_cache.get("subscribed_keys", [])

        if not subscribed_keys:
            error_message = (
                "Daily hard refresh failed: "
                "No subscribed keys found after contract reload."
            )
            logger.error(error_message)

            telegram_service.send_daily_refresh_message(
                success=False,
                error=error_message,
            )

            return

        logger.info(
            f"Daily hard refresh loaded {len(subscribed_keys)} subscribed instruments."
        )

        history_summary = fetch_daily_historical_candles()

        if history_summary:
            logger.info(
                f"Daily hard refresh historical EMA summary: "
                f"status={history_summary.get('status')}, "
                f"total_candles={history_summary.get('total_candles')}, "
                f"ema_result_file={history_summary.get('ema_results_file_path', 'not_saved')}, "
                f"live_ema_initialized={history_summary.get('live_ema_initialized')}, "
                f"live_ema_calculation_mode={get_live_ema_calculation_mode_text()}"
            )
        else:
            logger.warning(
                "Daily hard refresh historical EMA crossover fetch did not return summary."
            )

        loop = getattr(upstox_streamer, "loop", None)

        if loop and loop.is_running():
            logger.info("Scheduling Upstox streamer restart on FastAPI event loop...")

            future = asyncio.run_coroutine_threadsafe(
                upstox_streamer.restart(),
                loop,
            )

            def restart_done_callback(done_future):
                try:
                    done_future.result()

                    logger.info("Daily hard refresh streamer restart completed.")

                    telegram_service.send_subscription_message(
                        success=True,
                        subscribed_keys_count=len(
                            options_cache.get("subscribed_keys", [])
                        ),
                        feed_mode=getattr(config, "WEBSOCKET_FEED_MODE", "full"),
                    )

                    telegram_service.send_daily_refresh_message(
                        success=True,
                        subscribed_keys_count=len(
                            options_cache.get("subscribed_keys", [])
                        ),
                        nearest_expiry=options_cache.get("nearest_expiry"),
                    )

                except Exception as ex:
                    logger.error(
                        f"Daily hard refresh streamer restart failed: "
                        f"{type(ex).__name__}: {ex}"
                    )

                    telegram_service.send_exception_message(
                        title="Daily Hard Refresh Streamer Restart Failed",
                        exception=ex,
                        context="restart_done_callback",
                    )

                    telegram_service.send_daily_refresh_message(
                        success=False,
                        error=f"Streamer restart failed: {type(ex).__name__}: {ex}",
                    )

            future.add_done_callback(restart_done_callback)
            refresh_success = True

        else:
            warning_message = (
                "Upstox streamer event loop is not available. "
                "Token, subscription cache, historical EMA crossover, and live EMA state "
                "were refreshed, but streamer restart was skipped."
            )

            logger.warning(warning_message)

            telegram_service.send_message(
                title="Daily Refresh Warning",
                message=warning_message,
                level="WARNING",
            )

            telegram_service.send_daily_refresh_message(
                success=False,
                error=warning_message,
            )

    except Exception as ex:
        logger.error(f"Daily market hard refresh failed: {type(ex).__name__}: {ex}")

        telegram_service.send_exception_message(
            title="Daily Market Hard Refresh Failed",
            exception=ex,
            context="run_daily_market_hard_refresh",
        )

        telegram_service.send_daily_refresh_message(
            success=False,
            error=f"{type(ex).__name__}: {ex}",
        )

    finally:
        if refresh_success:
            logger.info(
                "Daily hard refresh flow scheduled streamer restart successfully."
            )

        logger.info(
            "================ DAILY MARKET HARD REFRESH COMPLETED ================"
        )


def start_scheduler() -> BackgroundScheduler:
    """Configures and starts background cron/interval tasks."""
    scheduler = BackgroundScheduler()

    scheduler.add_job(
        func=token_service.refresh_tokens,
        trigger="interval",
        minutes=config.REFRESH_INTERVAL_MINUTES,
        id="token_refresh_job",
        replace_existing=True,
    )

    scheduler.add_job(
        func=run_daily_market_hard_refresh,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=9,
            minute=0,
            timezone=getattr(config, "MARKET_TIMEZONE", "Asia/Kolkata"),
        ),
        id="daily_market_hard_refresh_job",
        replace_existing=True,
    )

    scheduler.add_job(
        func=run_daily_opening_range_fetch,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=getattr(config, "OPENING_RANGE_FETCH_HOUR", 9),
            minute=getattr(config, "OPENING_RANGE_FETCH_MINUTE", 18),
            timezone=getattr(config, "MARKET_TIMEZONE", "Asia/Kolkata"),
        ),
        id="daily_opening_range_fetch_job",
        replace_existing=True,
    )

    scheduler.start()

    scheduler_message = (
        f"Scheduler active.\n\n"
        f"Token refresh interval: every {config.REFRESH_INTERVAL_MINUTES} minutes\n"
        f"Daily hard refresh: Mon-Fri at 09:00 AM "
        f"{getattr(config, 'MARKET_TIMEZONE', 'Asia/Kolkata')}\n"
        f"Opening range fetch: Mon-Fri at "
        f"{getattr(config, 'OPENING_RANGE_FETCH_HOUR', 9):02d}:"
        f"{getattr(config, 'OPENING_RANGE_FETCH_MINUTE', 18):02d} "
        f"{getattr(config, 'MARKET_TIMEZONE', 'Asia/Kolkata')}\n"
        f"EMA WebSocket OR enrichment: "
        f"{getattr(config, 'EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS', True)}\n"
        f"Opening Range isolated instrument flow: "
        f"{getattr(config, 'OPENING_RANGE_ISOLATED_INSTRUMENT_ENABLED', True)}\n"
        f"Isolated EMA Telegram alerts: "
        f"{getattr(config, 'EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED', True)}\n"
        f"Live EMA Calculation Mode: {get_live_ema_calculation_mode_text()}\n"
        f"Live EMA Mode Description: {get_live_ema_calculation_mode_description()}\n"
        f"Isolated EMA Dashboard: /isolated-dashboard"
    )

    logger.info(
        f"Scheduler active: Token refresh every {config.REFRESH_INTERVAL_MINUTES} mins | "
        f"Daily market hard refresh scheduled for Mon-Fri at 09:00 AM "
        f"{getattr(config, 'MARKET_TIMEZONE', 'Asia/Kolkata')} | "
        f"Opening range fetch scheduled for Mon-Fri at "
        f"{getattr(config, 'OPENING_RANGE_FETCH_HOUR', 9):02d}:"
        f"{getattr(config, 'OPENING_RANGE_FETCH_MINUTE', 18):02d} "
        f"{getattr(config, 'MARKET_TIMEZONE', 'Asia/Kolkata')} | "
        f"EMA WebSocket OR enrichment="
        f"{getattr(config, 'EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS', True)} | "
        f"Opening Range isolation="
        f"{getattr(config, 'OPENING_RANGE_ISOLATED_INSTRUMENT_ENABLED', True)} | "
        f"Isolated EMA Telegram="
        f"{getattr(config, 'EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED', True)} | "
        f"Live EMA mode={get_live_ema_calculation_mode_text()}."
    )

    telegram_service.send_message(
        title="Scheduler Started",
        message=scheduler_message,
        level="INFO",
    )

    return scheduler


def log_registered_routes(fastapi_app: FastAPI):
    """Logs all registered HTTP and WebSocket routes in the application."""

    logger.info("=== Registered Application Routes ===")

    for route in fastapi_app.routes:
        path = getattr(route, "path", None)

        if not path:
            continue

        methods = getattr(route, "methods", None)

        if methods:
            methods_text = ",".join(sorted(methods))
            logger.info(f"  HTTP {methods_text} -> {path}")
        else:
            logger.info(f"  WS -> {path}")

    logger.info("=====================================")


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    """Handles async lifecycle startup/shutdown events for FastAPI."""
    logger.info("Executing lifespan startup sequence...")

    log_registered_routes(app)

    scheduler = None

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, run_initial_startup)

        scheduler = start_scheduler()

        await upstox_streamer.start()

        subscribed_keys = options_cache.get("subscribed_keys", [])

        telegram_service.send_subscription_message(
            success=True,
            subscribed_keys_count=len(subscribed_keys),
            feed_mode=getattr(config, "WEBSOCKET_FEED_MODE", "full"),
        )

        logger.info("Application startup completed successfully.")

        yield

    except Exception as ex:
        logger.error(f"Application startup/runtime failure: {type(ex).__name__}: {ex}")

        telegram_service.send_exception_message(
            title="Application Startup Runtime Failure",
            exception=ex,
            context="app_lifespan",
        )

        raise

    finally:
        logger.info("Executing lifespan shutdown sequence...")

        telegram_service.send_shutdown_message(
            details="Application shutdown sequence started."
        )

        try:
            await upstox_streamer.stop()

            if scheduler and scheduler.running:
                logger.info("Shutting down background scheduler...")
                scheduler.shutdown()

            telegram_service.send_shutdown_message(
                details="Application shutdown completed successfully."
            )

        except Exception as ex:
            logger.error(f"Application shutdown error: {type(ex).__name__}: {ex}")

            telegram_service.send_exception_message(
                title="Application Shutdown Error",
                exception=ex,
                context="app_lifespan shutdown",
            )


app = FastAPI(title="Option Feed Engine", lifespan=app_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# HTML Dashboard Routes
# ============================================================


@app.get("/isolated-dashboard", include_in_schema=False)
async def isolated_dashboard():
    """
    Serves the isolated EMA dashboard HTML page.

    File:
        templates/isolated_ema_dashboard.html
    """

    if not DASHBOARD_TEMPLATE_PATH.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                "Dashboard template not found. Expected file: "
                "templates/isolated_ema_dashboard.html"
            ),
        )

    return FileResponse(
        path=DASHBOARD_TEMPLATE_PATH,
        media_type="text/html",
    )


@app.get("/isolated-ema-dashboard", include_in_schema=False)
async def isolated_ema_dashboard_alias():
    """
    Alias route for isolated EMA dashboard.
    """

    return await isolated_dashboard()


# Register HTTP and WebSocket routes.
app.include_router(home_router)
app.include_router(health_router)
app.include_router(debug_router)
app.include_router(refresh_router)
app.include_router(history_router)
app.include_router(opening_range_router)
app.include_router(websocket_router)
app.include_router(ws_docs_router)


if __name__ == "__main__":
    logger.info("Starting FastAPI server with WebSockets on http://0.0.0.0:8000 ...")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
