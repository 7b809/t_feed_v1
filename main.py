import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi.middleware.cors import CORSMiddleware
from api.chart_routes import router as chart_router
from api.instrument_routes import router as instrument_router
from api.algo_app_routes import router as algo_app_router
from api.debug_routes import router as debug_router
from api.health_routes import router as health_router
from api.history_routes import router as history_router
from api.home_routes import router as home_router
from api.logs_api import router as logs_router
from api.opening_range_routes import router as opening_range_router
from api.refresh_routes import router as refresh_router
from api.ws_docs_routes import router as ws_docs_router
from core import config
from core.logger import get_logger
from services.history_service import fetch_historical_candles_for_all_subscribed
from services.opening_range_service import calculate_opening_range_for_all_subscribed
from services.option_service import get_options_contracts, options_cache
from services.telegram_service import telegram_service
from services.token_service import token_service
from services.upstox_websocket import upstox_streamer
from token_tasks.telegram_token_bot import telegram_token_bot
from token_tasks.token_monitor import check_upstox_token_validity, get_token_monitor_interval_minutes
from services.runtime_config_service import (
    close_runtime_config,
    get_budget_max_instruments,
    get_budget_max_price,
    get_budget_min_price,
    get_budget_range_enabled,
    get_isolation_window_points,
    initialize_runtime_config,
    runtime_config_service,
)
from ws_feed.websocket_routes import router as websocket_router
from local_tele_algo_app.local_main import app as local_app

logger = get_logger(__file__)


# ============================================================
# EMA Configuration Helpers
# ============================================================
def get_live_ema_calculation_mode_text() -> str:
    return "tick_ltp" if bool(config.LIVE_EMA_CALCULATION_MODE) else "candle_close"


def get_live_ema_calculation_mode_description() -> str:
    return "live tick/LTP based EMA calculation" if bool(config.LIVE_EMA_CALCULATION_MODE) else "completed candle close based EMA calculation"


def get_ema_order_side_rule_text() -> str:
    return "EMA Order Side Rule: bullish_cross uses the same option side as the isolated instrument; bearish_cross uses the opposite option side of the isolated instrument."


def get_algo_app_status_text() -> str:
    return f"Algo App Enabled: {config.ALGO_APP_ENABLED}\nAlgo App URL Configured: {bool(config.ALGO_APP_URL)}\nAlgo App Auth Type: {config.ALGO_APP_AUTH_TYPE}\nAlgo App Background Delivery: {config.ALGO_APP_SEND_IN_BACKGROUND}\nAlgo App Schema Version: {config.ALGO_APP_PAYLOAD_SCHEMA_VERSION}"


def get_budget_range_status_text() -> str:
    budget_enabled = get_budget_range_enabled()
    budget_minimum = get_budget_min_price()
    budget_maximum = get_budget_max_price()
    maximum_instruments = get_budget_max_instruments()
    return f"Budget Range Enabled: {budget_enabled}\nBudget Range: {budget_minimum:.2f} to {budget_maximum:.2f}\nBudget Maximum Instruments: {maximum_instruments}\nBudget Sort Mode: {config.EMA_ALERT_BUDGET_SORT_MODE}"


def get_runtime_config_status_text() -> str:
    try:
        status = runtime_config_service.get_status()
        return f"Runtime Config Enabled: {status.get('enabled')}\nRuntime Config Initialized: {status.get('initialized')}\nRuntime Config MongoDB Connected: {status.get('database_connected')}\nRuntime Config Overrides: {status.get('override_count', 0)}\nIsolation Window Points: {get_isolation_window_points()}"
    except Exception as ex:
        return f"Runtime Config Status: unavailable\nRuntime Config Error: {type(ex).__name__}: {ex}"


# ============================================================
# Instrument Loading
# ============================================================


def load_and_subscribe_instruments():
    logger.info("Executing contract load and subscription workflow...")
    result = get_options_contracts(save_data=True)
    if not result:
        logger.error("Failed to load option contracts for subscription.")
        telegram_service.send_instruments_fetched_message(success=False, error="Failed to load option contracts for subscription.")
        return None
    subscribed_keys = options_cache.get("subscribed_keys", [])
    logger.info("================ SUBSCRIPTION SUMMARY ================")
    logger.info("Main Security: %s", config.MAIN_NIFTY_SECURITY)
    logger.info("Strike Price Range: %s to %s", config.STRIKE_FROM, config.STRIKE_TO)
    logger.info("Total Subscribed Instruments: %s", len(subscribed_keys))
    logger.info("=======================================================")
    telegram_service.send_instruments_fetched_message(success=True, nearest_expiry=options_cache.get("nearest_expiry"), total_contracts=options_cache.get("total_contracts", 0), subscribed_keys_count=len(subscribed_keys), strike_from=config.STRIKE_FROM, strike_to=config.STRIKE_TO)
    return result


# ============================================================
# Historical EMA Startup
# ============================================================


def fetch_startup_historical_candles():
    logger.info("================ STARTUP HISTORICAL EMA CROSSOVER LOAD STARTED ================")
    try:
        subscribed_keys = options_cache.get("subscribed_keys", [])
        if not subscribed_keys:
            warning_message = "Startup historical EMA crossover fetch skipped. No subscribed instruments found."
            logger.warning(warning_message)
            telegram_service.send_message(title="Startup Historical EMA Fetch Skipped", message=warning_message, level="WARNING")
            return None
        history_summary = fetch_historical_candles_for_all_subscribed(interval=config.HISTORICAL_CANDLE_INTERVAL, history_days=config.HISTORICAL_CANDLE_DAYS, save_data=True, max_workers=config.HISTORICAL_CANDLE_MAX_WORKERS)
        logger.info("Startup historical EMA crossover fetch completed. status=%s, total_instruments=%s, success=%s, empty=%s, insufficient_data=%s, failed=%s, total_candles=%s, live_ema_initialized=%s, live_ema_calculation_mode=%s", history_summary.get("status"), history_summary.get("total_instruments"), history_summary.get("success_count"), history_summary.get("empty_count"), history_summary.get("insufficient_data_count"), history_summary.get("failed_count"), history_summary.get("total_candles"), history_summary.get("live_ema_initialized"), get_live_ema_calculation_mode_text())
        telegram_service.send_message(title="Startup Historical EMA Crossover Fetch Completed", message=f"Status: {history_summary.get('status')}\nFrom Date: {history_summary.get('from_date')}\nTo Date: {history_summary.get('to_date')}\nInterval: {history_summary.get('interval')}\nTotal Instruments: {history_summary.get('total_instruments')}\nSuccess: {history_summary.get('success_count')}\nEmpty: {history_summary.get('empty_count')}\nInsufficient Data: {history_summary.get('insufficient_data_count')}\nFailed: {history_summary.get('failed_count')}\nTotal Candles: {history_summary.get('total_candles')}\nEMA Fast Period: {history_summary.get('ema_fast_period')}\nEMA Slow Period: {history_summary.get('ema_slow_period')}\nEMA Result File: {history_summary.get('ema_results_file_path', 'not_saved')}\nLive EMA Initialized: {history_summary.get('live_ema_initialized')}\nLive EMA Calculation Mode: {get_live_ema_calculation_mode_text()}\nLive EMA Mode Description: {get_live_ema_calculation_mode_description()}\nTelegram EMA Alert Scope: isolated instrument only\n{get_ema_order_side_rule_text()}\n\n{get_budget_range_status_text()}\n\n{get_runtime_config_status_text()}\n\n{get_algo_app_status_text()}", level="INFO")
        return history_summary
    except Exception as ex:
        logger.error("Startup historical EMA crossover fetch failed: %s: %s", type(ex).__name__, ex)
        telegram_service.send_exception_message(title="Startup Historical EMA Crossover Fetch Failed", exception=ex, context="fetch_startup_historical_candles")
        return None
    finally:
        logger.info("================ STARTUP HISTORICAL EMA CROSSOVER LOAD COMPLETED ================")


# ============================================================
# Historical EMA Daily Refresh
# ============================================================


def fetch_daily_historical_candles():
    logger.info("================ DAILY HISTORICAL EMA CROSSOVER LOAD STARTED ================")
    try:
        subscribed_keys = options_cache.get("subscribed_keys", [])
        if not subscribed_keys:
            warning_message = "Daily historical EMA crossover fetch skipped. No subscribed instruments found."
            logger.warning(warning_message)
            telegram_service.send_message(title="Daily Historical EMA Fetch Skipped", message=warning_message, level="WARNING")
            return None
        history_summary = fetch_historical_candles_for_all_subscribed(interval=config.HISTORICAL_CANDLE_INTERVAL, history_days=config.HISTORICAL_CANDLE_DAYS, save_data=True, max_workers=config.HISTORICAL_CANDLE_MAX_WORKERS)
        logger.info("Daily historical EMA crossover fetch completed. status=%s, total_instruments=%s, success=%s, empty=%s, insufficient_data=%s, failed=%s, total_candles=%s, live_ema_initialized=%s, live_ema_calculation_mode=%s", history_summary.get("status"), history_summary.get("total_instruments"), history_summary.get("success_count"), history_summary.get("empty_count"), history_summary.get("insufficient_data_count"), history_summary.get("failed_count"), history_summary.get("total_candles"), history_summary.get("live_ema_initialized"), get_live_ema_calculation_mode_text())
        telegram_service.send_message(title="Daily Historical EMA Crossover Fetch Completed", message=f"Status: {history_summary.get('status')}\nFrom Date: {history_summary.get('from_date')}\nTo Date: {history_summary.get('to_date')}\nInterval: {history_summary.get('interval')}\nTotal Instruments: {history_summary.get('total_instruments')}\nSuccess: {history_summary.get('success_count')}\nEmpty: {history_summary.get('empty_count')}\nInsufficient Data: {history_summary.get('insufficient_data_count')}\nFailed: {history_summary.get('failed_count')}\nTotal Candles: {history_summary.get('total_candles')}\nEMA Fast Period: {history_summary.get('ema_fast_period')}\nEMA Slow Period: {history_summary.get('ema_slow_period')}\nEMA Result File: {history_summary.get('ema_results_file_path', 'not_saved')}\nLive EMA Initialized: {history_summary.get('live_ema_initialized')}\nLive EMA Calculation Mode: {get_live_ema_calculation_mode_text()}\nTelegram EMA Alert Scope: isolated instrument only\n{get_ema_order_side_rule_text()}", level="REFRESH")
        return history_summary
    except Exception as ex:
        logger.error("Daily historical EMA crossover fetch failed: %s: %s", type(ex).__name__, ex)
        telegram_service.send_exception_message(title="Daily Historical EMA Crossover Fetch Failed", exception=ex, context="fetch_daily_historical_candles")
        return None
    finally:
        logger.info("================ DAILY HISTORICAL EMA CROSSOVER LOAD COMPLETED ================")


# ============================================================
# Opening Range Daily Fetch
# ============================================================


def run_daily_opening_range_fetch():
    logger.info("================ DAILY OPENING RANGE FETCH STARTED ================")
    telegram_service.send_message(title="Daily Opening Range Fetch Started", message=f"Opening range fetch started.\n\nActions:\n1. Read subscribed instruments\n2. Fetch today's intraday candles\n3. Select opening range candles\n4. Calculate Opening Range values\n5. Calculate R1/S1, R2/S2 and R3/S3\n6. Scan previous candles for touches\n7. Evaluate isolated instrument\n8. Save Opening Range results\n9. Update in-memory Opening Range cache\n\nLive EMA Mode: {get_live_ema_calculation_mode_text()}\nIsolation Window: ±{get_isolation_window_points()} points\n{get_ema_order_side_rule_text()}\n\n{get_budget_range_status_text()}\n\n{get_algo_app_status_text()}", level="REFRESH")
    try:
        subscribed_keys = options_cache.get("subscribed_keys", [])
        if not subscribed_keys:
            warning_message = "Opening range fetch skipped. No subscribed instruments found in options_cache."
            logger.warning(warning_message)
            telegram_service.send_message(title="Opening Range Fetch Skipped", message=warning_message, level="WARNING")
            return None
        opening_range_summary = calculate_opening_range_for_all_subscribed(candle_count=config.OPENING_RANGE_CANDLE_COUNT, save_data=config.OPENING_RANGE_SAVE_FILE, max_workers=config.OPENING_RANGE_MAX_WORKERS)
        isolated_state = opening_range_summary.get("isolated_instrument") or {}
        isolated_selected = bool(isolated_state.get("selected"))
        logger.info("Daily opening range fetch completed. status=%s, total_instruments=%s, success=%s, empty=%s, insufficient_data=%s, failed=%s, backfill_touch_events=%s, latest_main_index_ltp=%s, isolated_selected=%s, isolated_instrument_key=%s, isolated_level=%s, output_file=%s", opening_range_summary.get("status"), opening_range_summary.get("total_instruments"), opening_range_summary.get("success_count"), opening_range_summary.get("empty_count"), opening_range_summary.get("insufficient_data_count"), opening_range_summary.get("failed_count"), opening_range_summary.get("backfill_touch_events_count", 0), opening_range_summary.get("latest_main_index_ltp"), isolated_selected, isolated_state.get("instrument_key"), isolated_state.get("selected_level"), opening_range_summary.get("output_file_path", "not_saved"))
        telegram_service.send_message(title="Daily Opening Range Fetch Completed", message=f"Status: {opening_range_summary.get('status')}\nDate: {opening_range_summary.get('date')}\nSource: {opening_range_summary.get('source')}\nInterval: {opening_range_summary.get('interval')}\nOpening Range Candles: {opening_range_summary.get('opening_range_candle_count')}\nMarket Open Time: {opening_range_summary.get('market_open_time')}\nOpening Range End Time: {opening_range_summary.get('opening_range_end_time')}\nTotal Instruments: {opening_range_summary.get('total_instruments')}\nSuccess: {opening_range_summary.get('success_count')}\nEmpty: {opening_range_summary.get('empty_count')}\nInsufficient Data: {opening_range_summary.get('insufficient_data_count')}\nFailed: {opening_range_summary.get('failed_count')}\nBackfill Touch Events: {opening_range_summary.get('backfill_touch_events_count', 0)}\nLatest NIFTY LTP: {opening_range_summary.get('latest_main_index_ltp')}\nIsolated Instrument Selected: {isolated_selected}\nIsolated Instrument Key: {isolated_state.get('instrument_key') if isolated_selected else 'not_selected'}\nIsolated Level: {isolated_state.get('selected_level') if isolated_selected else 'not_available'}\nIsolated EMA Alerts Count: {opening_range_summary.get('isolated_ema_alerts_count', 0)}\nLive EMA Mode: {get_live_ema_calculation_mode_text()}\nOutput File: {opening_range_summary.get('output_file_path', 'not_saved')}", level="REFRESH")
        return opening_range_summary
    except Exception as ex:
        logger.error("Daily opening range fetch failed: %s: %s", type(ex).__name__, ex)
        telegram_service.send_exception_message(title="Daily Opening Range Fetch Failed", exception=ex, context="run_daily_opening_range_fetch")
        return None
    finally:
        logger.info("================ DAILY OPENING RANGE FETCH COMPLETED ================")


# ============================================================
# Market Time Helpers
# ============================================================
def _get_market_now() -> datetime:
    try:
        market_timezone = ZoneInfo(config.MARKET_TIMEZONE)
    except Exception:
        market_timezone = ZoneInfo("Asia/Kolkata")
    return datetime.now(market_timezone)


def _is_weekday_market_day() -> bool:
    return _get_market_now().weekday() < 5


def _is_opening_range_fetch_time_passed() -> bool:
    now_market = _get_market_now()
    scheduled_time = now_market.replace(hour=config.OPENING_RANGE_FETCH_HOUR, minute=config.OPENING_RANGE_FETCH_MINUTE, second=0, microsecond=0)
    return now_market >= scheduled_time


def _is_todays_opening_range_already_saved() -> bool:
    output_file = Path(config.OPENING_RANGE_OUTPUT_FILE)
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
        return saved_date == today and saved_status == "success" and total_instruments > 0 and success_count > 0
    except Exception as ex:
        logger.warning("Could not verify today's saved Opening Range result. Startup catch-up will continue. error=%s: %s", type(ex).__name__, ex)
        return False


# ============================================================
# Opening Range Startup Catch-up
# ============================================================


def run_startup_opening_range_catchup() -> dict | None:
    now_market = _get_market_now()
    logger.info("Startup Opening Range catch-up check. market_datetime=%s", now_market.isoformat())
    if not _is_weekday_market_day():
        logger.info("Startup Opening Range catch-up skipped. Today is not a Monday-Friday market day.")
        return None
    if not _is_opening_range_fetch_time_passed():
        logger.info("Startup Opening Range catch-up not required. Configured Opening Range fetch time has not passed.")
        return None
    if _is_todays_opening_range_already_saved():
        logger.info("Startup Opening Range catch-up skipped. Today's Opening Range result is already saved.")
        return None
    telegram_service.send_message(title="Startup Opening Range Catch-up Started", message=f"Application started after today's scheduled Opening Range fetch time.\n\nMarket Date: {now_market.date().isoformat()}\nCurrent Market Time: {now_market.strftime('%H:%M:%S')}\nScheduled Fetch Time: {config.OPENING_RANGE_FETCH_HOUR:02d}:{config.OPENING_RANGE_FETCH_MINUTE:02d}\nDuplicate Protection: enabled", level="REFRESH")
    try:
        summary = run_daily_opening_range_fetch()
        if summary:
            logger.info("Startup Opening Range catch-up completed. status=%s, date=%s, success=%s, failed=%s, isolated_selected=%s", summary.get("status"), summary.get("date"), summary.get("success_count"), summary.get("failed_count"), bool((summary.get("isolated_instrument") or {}).get("selected")))
        return summary
    except Exception as ex:
        logger.error("Startup Opening Range catch-up failed: %s: %s", type(ex).__name__, ex)
        telegram_service.send_exception_message(title="Startup Opening Range Catch-up Failed", exception=ex, context="run_startup_opening_range_catchup")
        return None


# ============================================================
# Initial Startup
# ============================================================


def initialize_application_runtime_config() -> dict:
    logger.info("Initializing runtime configuration service...")
    try:
        status = initialize_runtime_config()
        logger.info("Runtime configuration initialized. enabled=%s, initialized=%s, database_connected=%s, overrides=%s, error=%s", status.get("enabled"), status.get("initialized"), status.get("database_connected"), status.get("override_count"), status.get("error"))
        if status.get("error"):
            telegram_service.send_message(title="Runtime Configuration Warning", message=f"Runtime configuration started with a fallback condition.\n\nEnabled: {status.get('enabled')}\nInitialized: {status.get('initialized')}\nMongoDB Connected: {status.get('database_connected')}\nError: {status.get('error')}\n\nEnvironment/default values will be used where runtime overrides are unavailable.", level="WARNING")
        else:
            telegram_service.send_message(title="Runtime Configuration Initialized", message=f"{get_runtime_config_status_text()}\n\nTelegram runtime configuration is available through /config.", level="INFO")
        return status
    except Exception as ex:
        logger.exception("Runtime configuration initialization failed.")
        telegram_service.send_exception_message(title="Runtime Configuration Initialization Failed", exception=ex, context="initialize_application_runtime_config")
        if not bool(config.RUNTIME_CONFIG_FAIL_OPEN):
            raise
        return {"enabled": bool(config.RUNTIME_CONFIG_ENABLED), "initialized": False, "database_connected": False, "override_count": 0, "error": f"{type(ex).__name__}: {ex}"}


def run_initial_startup():
    logger.info("Initializing application startup sequence...")
    telegram_service.send_startup_message(status="started", details=f"Initial startup sequence started.\n\nLive EMA Calculation Mode: {get_live_ema_calculation_mode_text()}\nLive EMA Mode Description: {get_live_ema_calculation_mode_description()}\n{get_ema_order_side_rule_text()}\n\n{get_budget_range_status_text()}\n\n{get_runtime_config_status_text()}\n\n{get_algo_app_status_text()}")
    try:
        token_service.refresh_tokens()
        current_token = token_service.get_access_token()
        token_document = token_service.get_token_document()
        if current_token:
            telegram_service.send_token_refresh_message(success=True, updated_at=token_document.get("updated_at") if token_document else "N/A")
        else:
            telegram_service.send_token_refresh_message(success=False, error="No access token found after MongoDB token refresh.")
        result = load_and_subscribe_instruments()
        history_summary = None
        opening_range_catchup_summary = None
        if result:
            history_summary = fetch_startup_historical_candles()
            opening_range_catchup_summary = run_startup_opening_range_catchup()
        else:
            logger.warning("Skipping startup historical EMA crossover fetch because instrument load failed.")
        cached_data = options_cache.get("data", [])
        sample_contract = cached_data[0] if cached_data else None
        subscribed_keys = options_cache.get("subscribed_keys", [])
        logger.info("=== Memory Cache State Summary ===")
        logger.info("Access Token Available: %s", bool(current_token))
        logger.info("Token Updated At: %s", token_document.get("updated_at") if token_document else "N/A")
        logger.info("Nearest Expiry: %s", options_cache.get("nearest_expiry"))
        logger.info("Total Contracts: %s", options_cache.get("total_contracts"))
        logger.info("Subscribed Instruments: %s", len(subscribed_keys))
        logger.info("Algo App Enabled: %s", config.ALGO_APP_ENABLED)
        logger.info("Algo App URL Configured: %s", bool(config.ALGO_APP_URL))
        budget_enabled = get_budget_range_enabled()
        budget_minimum = get_budget_min_price()
        budget_maximum = get_budget_max_price()
        budget_maximum_instruments = get_budget_max_instruments()
        isolation_window_points = get_isolation_window_points()
        logger.info("Budget Range Enabled: %s", budget_enabled)
        logger.info("Budget Range: %.2f to %.2f", budget_minimum, budget_maximum)
        logger.info("Budget Maximum Instruments: %s", budget_maximum_instruments)
        logger.info("Isolation Window Points: %s", isolation_window_points)
        if sample_contract:
            logger.info("Sample Contract:\n%s", json.dumps(sample_contract, indent=2, default=str))
        else:
            logger.info("Sample Contract: None")
        websocket_mode = "Opening Range enrichment enabled" if config.EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS else "Opening Range enrichment disabled"
        startup_details = f"Token Available: {'Yes' if current_token else 'No'}\nNearest Expiry: {options_cache.get('nearest_expiry')}\nTotal Contracts: {options_cache.get('total_contracts')}\nSubscribed Instruments: {len(subscribed_keys)}\nHistorical EMA: {history_summary.get('status') if history_summary else 'not_available'}\nHistorical Candles: {history_summary.get('total_candles') if history_summary else 0}\nOpening Range Catch-up: {'completed' if opening_range_catchup_summary else 'not_required'}\nLive EMA Initialized: {history_summary.get('live_ema_initialized') if history_summary else 'not_available'}\nEMA Mode: {get_live_ema_calculation_mode_text()}\nEMA WebSocket: {websocket_mode}\nBudget Range Enabled: {budget_enabled}\nBudget Range: {budget_minimum:.2f} to {budget_maximum:.2f}\nBudget Maximum Instruments: {budget_maximum_instruments}\nIsolation Window Points: {isolation_window_points}\nAlgo App Enabled: {config.ALGO_APP_ENABLED}\nAlgo App URL Configured: {bool(config.ALGO_APP_URL)}\nDashboard: /isolated-dashboard"
        if result and current_token and subscribed_keys:
            telegram_service.send_startup_message(status="completed successfully", details=startup_details)
        else:
            telegram_service.send_startup_message(status="completed with warnings", details=startup_details)
    except Exception as ex:
        logger.error("Initial startup sequence failed: %s: %s", type(ex).__name__, ex)
        telegram_service.send_exception_message(title="Initial Startup Failed", exception=ex, context="run_initial_startup")
        raise


# ============================================================
# Daily Market Hard Refresh
# ============================================================


def run_daily_market_hard_refresh():
    logger.info("================ DAILY MARKET HARD REFRESH STARTED ================")
    telegram_service.send_message(title="Daily Market Hard Refresh Started", message=f"Daily hard refresh started.\n\nActions:\n1. Refresh token\n2. Fetch latest option instruments\n3. Update subscription cache\n4. Fetch historical candles\n5. Initialize live EMA state\n6. Restart Upstox streamer\n\nEMA Mode: {get_live_ema_calculation_mode_text()}\n{get_ema_order_side_rule_text()}", level="REFRESH")
    refresh_success = False
    try:
        token_service.refresh_tokens()
        current_token = token_service.get_access_token()
        token_document = token_service.get_token_document()
        if not current_token:
            error_message = "Daily hard refresh failed: No access token available."
            logger.error(error_message)
            telegram_service.send_token_refresh_message(success=False, error=error_message)
            telegram_service.send_daily_refresh_message(success=False, error=error_message)
            return
        telegram_service.send_token_refresh_message(success=True, updated_at=token_document.get("updated_at") if token_document else "N/A")
        result = load_and_subscribe_instruments()
        if not result:
            error_message = "Daily hard refresh failed: Instrument fetch returned no result."
            logger.error(error_message)
            telegram_service.send_daily_refresh_message(success=False, error=error_message)
            return
        subscribed_keys = options_cache.get("subscribed_keys", [])
        if not subscribed_keys:
            error_message = "Daily hard refresh failed: No subscribed keys after contract reload."
            logger.error(error_message)
            telegram_service.send_daily_refresh_message(success=False, error=error_message)
            return
        history_summary = fetch_daily_historical_candles()
        if history_summary:
            logger.info("Daily hard refresh historical EMA summary. status=%s, total_candles=%s, live_ema_initialized=%s", history_summary.get("status"), history_summary.get("total_candles"), history_summary.get("live_ema_initialized"))
        loop = getattr(upstox_streamer, "loop", None)
        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(upstox_streamer.restart(), loop)

            def restart_done_callback(done_future):
                try:
                    done_future.result()
                    logger.info("Daily hard refresh streamer restart completed.")
                    current_subscribed_keys = options_cache.get("subscribed_keys", [])
                    telegram_service.send_subscription_message(success=True, subscribed_keys_count=len(current_subscribed_keys), feed_mode=config.WEBSOCKET_FEED_MODE)
                    telegram_service.send_daily_refresh_message(success=True, subscribed_keys_count=len(current_subscribed_keys), nearest_expiry=options_cache.get("nearest_expiry"))
                except Exception as ex:
                    logger.error("Daily hard refresh streamer restart failed: %s: %s", type(ex).__name__, ex)
                    telegram_service.send_exception_message(title="Daily Hard Refresh Streamer Restart Failed", exception=ex, context="restart_done_callback")
                    telegram_service.send_daily_refresh_message(success=False, error=f"Streamer restart failed: {type(ex).__name__}: {ex}")
            future.add_done_callback(restart_done_callback)
            refresh_success = True
        else:
            warning_message = "Upstox streamer event loop is unavailable. Cache and EMA state were refreshed, but streamer restart was skipped."
            logger.warning(warning_message)
            telegram_service.send_message(title="Daily Refresh Warning", message=warning_message, level="WARNING")
            telegram_service.send_daily_refresh_message(success=False, error=warning_message)
    except Exception as ex:
        logger.error("Daily market hard refresh failed: %s: %s", type(ex).__name__, ex)
        telegram_service.send_exception_message(title="Daily Market Hard Refresh Failed", exception=ex, context="run_daily_market_hard_refresh")
        telegram_service.send_daily_refresh_message(success=False, error=f"{type(ex).__name__}: {ex}")
    finally:
        if refresh_success:
            logger.info("Daily hard refresh scheduled streamer restart successfully.")
        logger.info("================ DAILY MARKET HARD REFRESH COMPLETED ================")


# ============================================================
# Scheduler
# ============================================================
def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=config.MARKET_TIMEZONE)
    scheduler.add_job(func=token_service.refresh_tokens, trigger="interval", minutes=config.REFRESH_INTERVAL_MINUTES, id="token_refresh_job", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(func=check_upstox_token_validity, trigger="interval", minutes=get_token_monitor_interval_minutes(), id="upstox_token_validity_check_job", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(func=run_daily_market_hard_refresh, trigger=CronTrigger(day_of_week="mon-fri", hour=9, minute=0, timezone=config.MARKET_TIMEZONE), id="daily_market_hard_refresh_job", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(func=run_daily_opening_range_fetch, trigger=CronTrigger(day_of_week="mon-fri", hour=config.OPENING_RANGE_FETCH_HOUR, minute=config.OPENING_RANGE_FETCH_MINUTE, timezone=config.MARKET_TIMEZONE), id="daily_opening_range_fetch_job", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.start()
    budget_minimum = get_budget_min_price()
    budget_maximum = get_budget_max_price()
    scheduler_message = f"Scheduler active.\n\nToken Refresh: Every {config.REFRESH_INTERVAL_MINUTES} minutes\nToken Check: Every {get_token_monitor_interval_minutes()} minutes\nTelegram Commands: {config.TELEGRAM_TOKEN_BOT_ENABLED}\nTelegram Runtime Config: {config.TELEGRAM_RUNTIME_CONFIG_ENABLED}\nDaily Refresh: 09:00 AM {config.MARKET_TIMEZONE}\nOpening Range: {config.OPENING_RANGE_FETCH_HOUR:02d}:{config.OPENING_RANGE_FETCH_MINUTE:02d}\nIsolation Window: ±{get_isolation_window_points()} points\nEMA Mode: {get_live_ema_calculation_mode_text()}\nBudget Range: {budget_minimum:.2f} to {budget_maximum:.2f}\nAlgo App Enabled: {config.ALGO_APP_ENABLED}\nDashboard: /isolated-dashboard"
    logger.info("Scheduler active. token_refresh=%s, token_check=%s, opening_range=%02d:%02d, timezone=%s, isolation_window=%s, live_ema_mode=%s", config.REFRESH_INTERVAL_MINUTES, get_token_monitor_interval_minutes(), config.OPENING_RANGE_FETCH_HOUR, config.OPENING_RANGE_FETCH_MINUTE, config.MARKET_TIMEZONE, get_isolation_window_points(), get_live_ema_calculation_mode_text())
    telegram_service.send_message(title="Scheduler Started", message=scheduler_message, level="INFO")
    return scheduler


# ============================================================
# Route Logging
# ============================================================
def log_registered_routes(fastapi_app: FastAPI):
    logger.info("Registered application routes:")
    for route in fastapi_app.routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        methods = getattr(route, "methods", None)
        if methods:
            methods_text = ",".join(sorted(methods))
            logger.info("HTTP %s -> %s", methods_text, path)
        else:
            logger.info("WS -> %s", path)


# ============================================================
# Application Lifespan
# ============================================================
@asynccontextmanager
async def app_lifespan(app: FastAPI):
    logger.info("Executing lifespan startup sequence...")
    log_registered_routes(app)
    scheduler = None
    streamer_started = False
    telegram_bot_started = False
    try:
        loop = asyncio.get_running_loop()
        runtime_status = await loop.run_in_executor(None, initialize_application_runtime_config)
        logger.info("Runtime configuration startup status: %s", runtime_status)
        await loop.run_in_executor(None, run_initial_startup)
        scheduler = start_scheduler()
        telegram_bot_started = telegram_token_bot.start()
        await upstox_streamer.start()
        streamer_started = True
        subscribed_keys = options_cache.get("subscribed_keys", [])
        telegram_service.send_subscription_message(success=True, subscribed_keys_count=len(subscribed_keys), feed_mode=config.WEBSOCKET_FEED_MODE)
        logger.info("Application startup completed successfully. telegram_bot_started=%s, streamer_started=%s, runtime_config_overrides=%s", telegram_bot_started, streamer_started, runtime_status.get("override_count", 0))
        yield
    except Exception as ex:
        logger.exception("Application startup/runtime failure.")
        telegram_service.send_exception_message(title="Application Startup Runtime Failure", exception=ex, context="app_lifespan")
        raise
    finally:
        logger.info("Executing lifespan shutdown sequence...")
        telegram_service.send_shutdown_message(details="Application shutdown sequence started.")
        shutdown_errors = []
        try:
            if telegram_bot_started:
                telegram_token_bot.stop()
        except Exception as ex:
            shutdown_errors.append(f"Telegram bot: {type(ex).__name__}: {ex}")
            logger.exception("Telegram bot shutdown failed.")
        try:
            if streamer_started:
                await upstox_streamer.stop()
        except Exception as ex:
            shutdown_errors.append(f"Upstox streamer: {type(ex).__name__}: {ex}")
            logger.exception("Upstox streamer shutdown failed.")
        try:
            if scheduler is not None and scheduler.running:
                logger.info("Shutting down background scheduler...")
                scheduler.shutdown(wait=False)
        except Exception as ex:
            shutdown_errors.append(f"Scheduler: {type(ex).__name__}: {ex}")
            logger.exception("Scheduler shutdown failed.")
        try:
            close_runtime_config()
        except Exception as ex:
            shutdown_errors.append(f"Runtime configuration: {type(ex).__name__}: {ex}")
            logger.exception("Runtime configuration shutdown failed.")
        if shutdown_errors:
            error_text = "\n".join(shutdown_errors)
            telegram_service.send_shutdown_message(details=f"Application shutdown completed with errors.\n\n{error_text}")
        else:
            telegram_service.send_shutdown_message(details="Application shutdown completed successfully.")
        logger.info("Application shutdown sequence completed.")


# ============================================================
# FastAPI Application
# ============================================================

app = FastAPI(title="Option Feed Engine", lifespan=app_lifespan)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

app.mount("/local-app", local_app)

# ============================================================
# Router Registration
# ============================================================

app.include_router(home_router)
app.include_router(health_router)
app.include_router(debug_router)
app.include_router(refresh_router)
app.include_router(history_router)
app.include_router(opening_range_router)
app.include_router(algo_app_router)
app.include_router(chart_router)
app.include_router(websocket_router)
app.include_router(ws_docs_router)
app.include_router(logs_router)
app.include_router(instrument_router)