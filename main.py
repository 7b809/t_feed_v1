import asyncio
import json
import uvicorn
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from api.logs_api import router as logs_router
from core import config
from core.logger import get_logger
from services.option_service import get_options_contracts, options_cache
from services.token_service import token_service
from services.upstox_websocket import upstox_streamer
from services.telegram_service import telegram_service
from services.history_service import fetch_historical_candles_for_all_subscribed
from services.opening_range_service import calculate_opening_range_for_all_subscribed

from token_tasks.token_monitor import (
    check_upstox_token_validity,
    get_token_monitor_interval_minutes,
)
from token_tasks.telegram_token_bot import telegram_token_bot

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


def get_ema_order_side_rule_text() -> str:
    """
    Returns readable EMA Telegram order-side rule.

    Current rule:
        bullish_cross -> same side as isolated instrument
        bearish_cross -> opposite side of isolated instrument
    """

    return (
        "EMA Order Side Rule: bullish_cross uses the same option side as the "
        "isolated instrument; bearish_cross uses the opposite option side of "
        "the isolated instrument."
    )


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
    - Suggested Telegram order side is dynamic:
        bullish_cross -> same side as isolated instrument
        bearish_cross -> opposite side of isolated instrument
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
            f"live_ema_calculation_mode={get_live_ema_calculation_mode_text()}, "
            f"ema_order_side_rule=dynamic_isolated_instrument_side"
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
                f"Telegram EMA Alert Scope: isolated instrument only\n"
                f"{get_ema_order_side_rule_text()}"
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
    - Suggested Telegram order side is dynamic:
        bullish_cross -> same side as isolated instrument
        bearish_cross -> opposite side of isolated instrument
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
            f"live_ema_calculation_mode={get_live_ema_calculation_mode_text()}, "
            f"ema_order_side_rule=dynamic_isolated_instrument_side"
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
                f"Telegram EMA Alert Scope: isolated instrument only\n"
                f"{get_ema_order_side_rule_text()}"
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
    - Suggested Telegram order side is dynamic:
        bullish_cross -> same side as isolated instrument
        bearish_cross -> opposite side of isolated instrument
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
            "- EMA Telegram alerts only for isolated instrument\n"
            "- bullish_cross uses same side as isolated instrument\n"
            "- bearish_cross uses opposite side of isolated instrument\n\n"
            f"Live EMA Calculation Mode: {get_live_ema_calculation_mode_text()}\n"
            f"Live EMA Mode Description: {get_live_ema_calculation_mode_description()}\n"
            f"{get_ema_order_side_rule_text()}"
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
            f"ema_order_side_rule=dynamic_isolated_instrument_side, "
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
                f"{get_ema_order_side_rule_text()}\n"
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


def _get_market_now() -> datetime:
    """
    Returns current datetime in the configured market timezone.
    Falls back to Asia/Kolkata if the configured timezone is invalid.
    """
    timezone_name = getattr(config, "MARKET_TIMEZONE", "Asia/Kolkata")

    try:
        timezone = ZoneInfo(timezone_name)
    except Exception:
        timezone = ZoneInfo("Asia/Kolkata")

    return datetime.now(timezone)


def _is_weekday_market_day() -> bool:
    """Returns True when the current market date is Monday-Friday."""
    return _get_market_now().weekday() < 5


def _is_opening_range_fetch_time_passed() -> bool:
    """
    Returns True when today's configured Opening Range scheduled fetch time
    has already passed in the configured market timezone.
    """
    now_market = _get_market_now()

    scheduled_hour = int(getattr(config, "OPENING_RANGE_FETCH_HOUR", 9))
    scheduled_minute = int(getattr(config, "OPENING_RANGE_FETCH_MINUTE", 18))

    scheduled_time = now_market.replace(
        hour=scheduled_hour,
        minute=scheduled_minute,
        second=0,
        microsecond=0,
    )

    return now_market >= scheduled_time


def _is_todays_opening_range_already_saved() -> bool:
    """
    Checks whether today's Opening Range result is already saved successfully.

    The Opening Range service saves the calculation summary to the configured
    OPENING_RANGE_OUTPUT_FILE. The summary contains the calculation date and
    status, so this prevents a normal application restart later in the day
    from unnecessarily recalculating today's Opening Range.
    """
    output_file = Path(
        getattr(
            config,
            "OPENING_RANGE_OUTPUT_FILE",
            "data/opening_range_results.json",
        )
    )

    if not output_file.exists():
        return False

    try:
        with output_file.open("r", encoding="utf-8") as file:
            saved_summary = json.load(file)

        if not isinstance(saved_summary, dict):
            return False

        today = _get_market_now().date().isoformat()
        saved_date = str(saved_summary.get("date") or "")
        saved_status = str(saved_summary.get("status") or "").lower()

        success_count = int(saved_summary.get("success_count") or 0)
        total_instruments = int(saved_summary.get("total_instruments") or 0)

        return (
            saved_date == today
            and saved_status == "success"
            and total_instruments > 0
            and success_count > 0
        )

    except Exception as ex:
        logger.warning(
            "Could not verify today's saved Opening Range result. "
            "Startup catch-up will continue. error=%s: %s",
            type(ex).__name__,
            ex,
        )
        return False


def run_startup_opening_range_catchup() -> dict | None:
    """
    Performs the Opening Range startup catch-up when the application starts
    after today's scheduled Opening Range job has already passed.

    Normal behavior:
        - Before 09:18 AM: do nothing and wait for the scheduled job.
        - At/after 09:18 AM: check today's saved result.
        - If today's result already exists successfully: do nothing.
        - Otherwise: immediately run today's Opening Range calculation.

    This is intentionally separate from the scheduled Opening Range function
    so the same calculation workflow is used by both the scheduler and the
    startup catch-up.
    """
    now_market = _get_market_now()

    logger.info(
        "Startup Opening Range catch-up check. " "market_datetime=%s",
        now_market.isoformat(),
    )

    if not _is_weekday_market_day():
        logger.info(
            "Startup Opening Range catch-up skipped. "
            "Today is not a Monday-Friday market day."
        )
        return None

    if not _is_opening_range_fetch_time_passed():
        logger.info(
            "Startup Opening Range catch-up not required. "
            "Configured Opening Range fetch time has not passed yet."
        )
        return None

    if _is_todays_opening_range_already_saved():
        logger.info(
            "Startup Opening Range catch-up skipped. "
            "Today's Opening Range result is already saved successfully."
        )
        return None

    logger.info(
        "Startup Opening Range catch-up required. "
        "Today's scheduled Opening Range job has already passed and "
        "today's successful Opening Range result was not found."
    )

    telegram_service.send_message(
        title="Startup Opening Range Catch-up Started",
        message=(
            "Application started after today's scheduled Opening Range fetch time.\n\n"
            "Startup catch-up is now running today's Opening Range calculation "
            "instead of waiting for the next trading day's 09:18 AM job.\n\n"
            f"Market Date: {now_market.date().isoformat()}\n"
            f"Current Market Time: {now_market.strftime('%H:%M:%S')}\n"
            f"Scheduled Fetch Time: "
            f"{getattr(config, 'OPENING_RANGE_FETCH_HOUR', 9):02d}:"
            f"{getattr(config, 'OPENING_RANGE_FETCH_MINUTE', 18):02d}\n"
            "Duplicate protection: enabled"
        ),
        level="REFRESH",
    )

    try:
        summary = run_daily_opening_range_fetch()

        if summary:
            logger.info(
                "Startup Opening Range catch-up completed. "
                "status=%s, date=%s, success=%s, failed=%s, "
                "isolated_selected=%s",
                summary.get("status"),
                summary.get("date"),
                summary.get("success_count"),
                summary.get("failed_count"),
                bool((summary.get("isolated_instrument") or {}).get("selected")),
            )

        return summary

    except Exception as ex:
        logger.error(
            "Startup Opening Range catch-up failed: %s: %s",
            type(ex).__name__,
            ex,
        )

        telegram_service.send_exception_message(
            title="Startup Opening Range Catch-up Failed",
            exception=ex,
            context="run_startup_opening_range_catchup",
        )

        return None


def run_initial_startup():
    """
    Initial synchronous load when the application starts up.

    Startup lifecycle:
    1. Refresh Upstox token from MongoDB.
    2. Fetch latest option contracts.
    3. Apply configured strike range and rebuild subscriptions.
    4. Fetch historical candles for all subscribed instruments.
    5. Calculate historical EMA crossover state.
    6. Initialize live EMA state.
    7. Check whether today's Opening Range scheduled job has already passed.
    8. If it has passed and today's successful Opening Range result is missing,
       immediately perform the Opening Range catch-up.
    9. If today's Opening Range already exists, do not duplicate the calculation.
    10. Return control to application startup so scheduler and live streaming
        can continue normally.
    """

    logger.info("Initializing application startup sequence...")

    telegram_service.send_startup_message(
        status="started",
        details=(
            "Initial startup sequence started. "
            "Refreshing token, loading instruments, calculating historical EMA crossover, "
            "and initializing live EMA state for all subscribed instruments. "
            "Opening Range isolated instrument selection will run after Opening Range fetch.\n\n"
            f"Live EMA Calculation Mode: {get_live_ema_calculation_mode_text()}\n"
            f"Live EMA Mode Description: {get_live_ema_calculation_mode_description()}\n"
            f"{get_ema_order_side_rule_text()}"
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
        opening_range_catchup_summary = None

        if result:
            history_summary = fetch_startup_historical_candles()

            # ========================================================
            # STARTUP OPENING RANGE CATCH-UP
            # ========================================================
            # The normal Opening Range job is scheduled for 09:18 AM.
            # If the application starts/restarts after 09:18 AM, APScheduler
            # does not guarantee that the already-missed job will execute.
            #
            # Therefore:
            # - Before 09:18 AM -> wait for the normal scheduled job.
            # - After 09:18 AM -> check today's saved Opening Range result.
            # - If today's result is missing -> fetch Opening Range immediately.
            # - If today's result already exists successfully -> do nothing.
            #
            # This prevents a startup after 09:18 AM from waiting until the
            # next trading day while also preventing unnecessary duplicate
            # Opening Range calculations during normal restarts.
            # ========================================================
            opening_range_catchup_summary = run_startup_opening_range_catchup()
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
            f"Nearest Expiry: {options_cache.get('nearest_expiry')}\n"
            f"Total Contracts: {options_cache.get('total_contracts')}\n"
            f"Subscribed Instruments: {len(subscribed_keys)}\n"
            f"Historical EMA: "
            f"{history_summary.get('status') if history_summary else 'not_available'}\n"
            f"Historical Candles: "
            f"{history_summary.get('total_candles') if history_summary else 0}\n"
            f"Startup Opening Range Catch-up: "
            f"{'completed' if opening_range_catchup_summary else 'not_required'}\n"
            f"Live EMA Initialized: "
            f"{history_summary.get('live_ema_initialized') if history_summary else 'not_available'}\n"
            f"EMA Mode: {get_live_ema_calculation_mode_text()}\n"
            f"EMA WebSocket: {websocket_mode}\n"
            f"Dashboard: /isolated-dashboard"
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
    - Suggested Telegram order side is dynamic:
        bullish_cross -> same side as isolated instrument
        bearish_cross -> opposite side of isolated instrument
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
            f"Live EMA Mode Description: {get_live_ema_calculation_mode_description()}\n"
            f"{get_ema_order_side_rule_text()}"
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
                f"live_ema_calculation_mode={get_live_ema_calculation_mode_text()}, "
                f"ema_order_side_rule=dynamic_isolated_instrument_side"
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
    """
    Configures and starts background cron/interval tasks.

    Scheduled daily lifecycle:
    - 09:00 AM IST, Monday-Friday:
      daily market hard refresh.
    - 09:18 AM IST, Monday-Friday:
      Opening Range fetch.
    - Application startup after 09:18 AM:
      startup catch-up checks for today's Opening Range before the scheduler
      starts waiting for future scheduled executions.
    """

    scheduler = BackgroundScheduler()

    scheduler.add_job(
        func=token_service.refresh_tokens,
        trigger="interval",
        minutes=config.REFRESH_INTERVAL_MINUTES,
        id="token_refresh_job",
        replace_existing=True,
    )

    scheduler.add_job(
        func=check_upstox_token_validity,
        trigger="interval",
        minutes=get_token_monitor_interval_minutes(),
        id="upstox_token_validity_check_job",
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
        "Scheduler active.\n\n"
        f"Token refresh: Every {config.REFRESH_INTERVAL_MINUTES} minutes\n"
        f"Token check: Every {get_token_monitor_interval_minutes()} minutes\n"
        f"Telegram commands: "
        f"{getattr(config, 'TELEGRAM_TOKEN_BOT_ENABLED', True)}\n"
        f"Daily refresh: 09:00 AM "
        f"{getattr(config, 'MARKET_TIMEZONE', 'Asia/Kolkata')}\n"
        f"Opening Range: "
        f"{getattr(config, 'OPENING_RANGE_FETCH_HOUR', 9):02d}:"
        f"{getattr(config, 'OPENING_RANGE_FETCH_MINUTE', 18):02d}\n"
        f"EMA Mode: {get_live_ema_calculation_mode_text()}\n"
        f"Dashboard: /isolated_dashboard"
    )
    logger.info(
        f"Scheduler active: Token refresh every {config.REFRESH_INTERVAL_MINUTES} mins | "
        f"Upstox token validity check every {get_token_monitor_interval_minutes()} mins | "
        f"Telegram token commands="
        f"{getattr(config, 'TELEGRAM_TOKEN_BOT_ENABLED', True)} | "
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
        f"Live EMA mode={get_live_ema_calculation_mode_text()} | "
        f"EMA order side rule=dynamic_isolated_instrument_side."
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

        telegram_token_bot.start()

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
            telegram_token_bot.stop()

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
app.include_router(logs_router)

# ============================================================
# APPLICATION STARTUP / DAILY MARKET INITIALIZATION
# ============================================================
#
# This comment block documents the expected lifecycle for testing,
# debugging, maintenance, and future feature work.
#
# APPLICATION STARTUP:
#
# 1. Refresh Upstox token from MongoDB.
#
# 2. Fetch latest option contracts.
#
# 3. Apply configured strike range and rebuild subscriptions.
#
# 4. Fetch historical candles for all subscribed instruments.
#
# 5. Calculate historical EMA crossover state.
#
# 6. Initialize live EMA state.
#
# 7. Check the current market date/time.
#
# 8. Check whether today's configured Opening Range fetch time has passed.
#
# 9. If the application starts BEFORE the Opening Range scheduled time:
#    - Do not fetch Opening Range immediately.
#    - Wait for the normal scheduled 09:18 AM job.
#
# 10. If the application starts AFTER the Opening Range scheduled time:
#     - Check the saved Opening Range result file.
#     - Verify that today's date is present.
#     - Verify that the saved result status is successful.
#
# 11. If today's Opening Range result is NOT available:
#     - Automatically run the Opening Range fetch during startup.
#     - Do not wait for the next trading day's 09:18 AM job.
#
# 12. If today's Opening Range result IS already available:
#     - Skip the startup catch-up.
#     - Avoid unnecessary duplicate Opening Range calculations.
#
# DAILY 09:00 HARD REFRESH:
#
# 13. Refresh token from MongoDB.
#
# 14. Fetch latest instruments.
#
# 15. Filter the configured strike range.
#
# 16. Rebuild the subscription cache.
#
# 17. Fetch historical candles.
#
# 18. Recalculate EMA crossover state.
#
# 19. Initialize live EMA state.
#
# 20. Restart the Upstox streamer with refreshed subscriptions.
#
# DAILY 09:18 OPENING RANGE:
#
# 21. Read subscribed instruments.
#
# 22. Fetch today's intraday candles.
#
# 23. Select the configured Opening Range candles.
#
# 24. Calculate Opening Range OHLC and average.
#
# 25. Calculate R1/S1, R2/S2, R3/S3 and thresholds.
#
# 26. Backfill-scan R2/R3/S2/S3 touches that occurred before the
#     Opening Range fetch.
#
# 27. Evaluate isolated instrument selection.
#
# 28. Save Opening Range results.
#
# 29. Update the in-memory Opening Range cache.
#
# 30. Make Opening Range levels available for EMA WebSocket enrichment.
#
# LIVE PROCESSING:
#
# 31. Continue live Upstox tick processing.
#
# 32. Continue live EMA calculation for all subscribed instruments.
#
# 33. Continue live Opening Range touch monitoring after OR levels exist.
#
# 34. Keep isolated-instrument EMA Telegram alert rules active.
#
# STARTUP CATCH-UP TEST CASES:
#
# 35. Start application before 09:18 AM:
#     Expected -> no startup Opening Range fetch.
#
# 36. Start application after 09:18 AM with no today's OR result:
#     Expected -> startup Opening Range fetch runs automatically.
#
# 37. Restart application after 09:18 AM with today's OR result already saved:
#     Expected -> startup catch-up is skipped.
#
# 38. Start application on Saturday/Sunday:
#     Expected -> startup Opening Range catch-up is skipped.
#
# 39. Start application after 09:18 AM when OR result file is invalid:
#     Expected -> startup catch-up attempts a fresh Opening Range fetch.
#
# 40. If startup catch-up fails:
#     Expected -> application reports the failure through logs/Telegram
#     and the normal scheduled job remains registered for future execution.
#
# ============================================================

# if __name__ == "__main__":
#     logger.info("Starting FastAPI server with WebSockets on http://0.0.0.0:8000 ...")
#     uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
