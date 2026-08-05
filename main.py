import asyncio
import json
import uvicorn
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core import config
from core.logger import get_logger
from services.option_service import get_options_contracts, options_cache
from services.token_service import token_service
from services.upstox_websocket import upstox_streamer
from services.telegram_service import telegram_service
from services.history_service import fetch_historical_candles_for_all_subscribed

from api.home_routes import router as home_router
from api.health_routes import router as health_router
from api.debug_routes import router as debug_router
from api.refresh_routes import router as refresh_router
from api.history_routes import router as history_router
from ws_feed.websocket_routes import router as websocket_router

logger = get_logger(__file__)


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

    This runs after:
    1. Token refresh
    2. Option contract load
    3. options_cache subscribed_keys update

    Historical API does not require access token.
    Raw candles are not saved.
    EMA crossover results are saved only when TEST_FLAG=True.
    Live EMA service is initialized from historical EMA summary.
    """

    logger.info("================ STARTUP HISTORICAL EMA CROSSOVER LOAD STARTED ================")

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
            f"live_ema_initialized={history_summary.get('live_ema_initialized')}"
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
                f"Live EMA Initialized: {history_summary.get('live_ema_initialized')}"
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
        logger.info("================ STARTUP HISTORICAL EMA CROSSOVER LOAD COMPLETED ================")


def fetch_daily_historical_candles():
    """
    Fetches historical candles and calculates EMA crossover during daily hard refresh.

    This runs after:
    1. Token refresh
    2. Latest option contract load
    3. options_cache subscribed_keys update

    This should complete before Upstox streamer restart.
    Raw candles are not saved.
    EMA crossover results are saved only when TEST_FLAG=True.
    Live EMA service is re-initialized from latest historical EMA summary.
    """

    logger.info("================ DAILY HISTORICAL EMA CROSSOVER LOAD STARTED ================")

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
            f"live_ema_initialized={history_summary.get('live_ema_initialized')}"
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
                f"Live EMA Initialized: {history_summary.get('live_ema_initialized')}"
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
        logger.info("================ DAILY HISTORICAL EMA CROSSOVER LOAD COMPLETED ================")


def run_initial_startup():
    """Initial synchronous load when the application starts up."""
    logger.info("Initializing application startup sequence...")

    telegram_service.send_startup_message(
        status="started",
        details=(
            "Initial startup sequence started. "
            "Refreshing token, loading instruments, calculating historical EMA crossover, "
            "and initializing live EMA state."
        ),
    )

    try:
        # 1. Fetch access token from DB into memory
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

        # 2. Fetch option contracts and update subscription keys
        result = load_and_subscribe_instruments()

        # 3. Fetch historical candles and calculate EMA crossover for all subscribed instruments
        history_summary = None

        if result:
            history_summary = fetch_startup_historical_candles()
        else:
            logger.warning(
                "Skipping startup historical EMA crossover fetch because instrument load failed."
            )

        # 4. Startup cache and sample log
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
            f"{history_summary.get('live_ema_initialized') if history_summary else 'not_available'}"
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

    Runs from APScheduler thread.

    Steps:
    1. Refresh token document from MongoDB.
    2. Load latest token into memory.
    3. Fetch latest NIFTY option contracts.
    4. Filter contracts by configured strike range.
    5. Update options_cache and subscribed_keys.
    6. Fetch historical candles and calculate EMA crossover for all subscribed instruments.
    7. Initialize live EMA state from historical EMA summary.
    8. Restart Upstox streamer so latest keys are actually subscribed.
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
            "6. Initialize live EMA state\n"
            "7. Restart Upstox streamer"
        ),
        level="REFRESH",
    )

    refresh_success = False

    try:
        # 1. Refresh access token document from MongoDB
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

        # 2. Fetch latest option contracts and update options_cache
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

        # 3. Fetch historical candles and calculate EMA crossover before streamer restart
        history_summary = fetch_daily_historical_candles()

        if history_summary:
            logger.info(
                f"Daily hard refresh historical EMA summary: "
                f"status={history_summary.get('status')}, "
                f"total_candles={history_summary.get('total_candles')}, "
                f"ema_result_file={history_summary.get('ema_results_file_path', 'not_saved')}, "
                f"live_ema_initialized={history_summary.get('live_ema_initialized')}"
            )
        else:
            logger.warning(
                "Daily hard refresh historical EMA crossover fetch did not return summary."
            )

        # 4. Restart Upstox streamer on its running FastAPI event loop
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

    scheduler.start()

    scheduler_message = (
        f"Scheduler active.\n\n"
        f"Token refresh interval: every {config.REFRESH_INTERVAL_MINUTES} minutes\n"
        f"Daily hard refresh: Mon-Fri at 09:00 AM "
        f"{getattr(config, 'MARKET_TIMEZONE', 'Asia/Kolkata')}"
    )

    logger.info(
        f"Scheduler active: Token refresh every {config.REFRESH_INTERVAL_MINUTES} mins | "
        f"Daily market hard refresh scheduled for Mon-Fri at 09:00 AM "
        f"{getattr(config, 'MARKET_TIMEZONE', 'Asia/Kolkata')}."
    )

    telegram_service.send_message(
        title="Scheduler Started",
        message=scheduler_message,
        level="INFO",
    )

    return scheduler


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    """Handles async lifecycle startup/shutdown events for FastAPI."""
    logger.info("Executing lifespan startup sequence...")

    scheduler = None

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, run_initial_startup)

        scheduler = start_scheduler()

        # upstox_streamer.start() creates its own background task
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

# Register HTTP and WebSocket routes
app.include_router(home_router)
app.include_router(health_router)
app.include_router(debug_router)
app.include_router(refresh_router)
app.include_router(history_router)
app.include_router(websocket_router)


if __name__ == "__main__":
    logger.info("Starting FastAPI server with WebSockets on http://0.0.0.0:8000 ...")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
    
    