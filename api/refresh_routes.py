import asyncio
from datetime import datetime, timezone, time
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from core import config
from core.logger import get_logger
from services.option_service import get_options_contracts, options_cache
from services.token_service import token_service
from services.upstox_websocket import upstox_streamer
from services.telegram_service import telegram_service
from services.history_service import fetch_historical_candles_for_all_subscribed
from services.opening_range_service import (
    calculate_opening_range_for_all_subscribed,
)

logger = get_logger(__file__)

router = APIRouter()

_manual_refresh_lock = asyncio.Lock()

_last_manual_refresh = {
    "status": "not_started",
    "timestamp": None,
    "message": "Manual refresh has not been triggered yet.",
    "subscribed_instruments": 0,
    "nearest_expiry": None,
    "historical_ema_status": None,
    "live_ema_initialized": None,
    "opening_range_status": None,
    "opening_range_executed": False,
    "opening_range_market_time": None,
    "opening_range_timezone": None,
}


# ============================================================
# MARKET TIME HELPERS
# ============================================================


def get_market_timezone() -> ZoneInfo:
    """
    Returns the configured market timezone.

    Uses MARKET_TIMEZONE from core.config.

    Expected config:
        MARKET_TIMEZONE = get_string(
            "MARKET_TIMEZONE",
            "Asia/Kolkata",
        )
    """
    timezone_name = getattr(
        config,
        "MARKET_TIMEZONE",
        "Asia/Kolkata",
    )

    try:
        return ZoneInfo(timezone_name)
    except Exception as ex:
        logger.error(
            f"Invalid MARKET_TIMEZONE={timezone_name!r}. "
            f"Falling back to Asia/Kolkata. "
            f"error={type(ex).__name__}: {ex}"
        )

        return ZoneInfo("Asia/Kolkata")


def get_current_market_datetime() -> datetime:
    """
    Returns the current datetime in the configured market timezone.
    """
    market_timezone = get_market_timezone()

    return datetime.now(market_timezone)


def is_opening_range_refresh_window() -> tuple[bool, datetime]:
    """
    Determines whether Opening Range processing should run as part
    of the manual hard refresh.

    Opening Range processing is allowed only between:

        09:15:00
        and
        15:45:00

    inclusive, using MARKET_TIMEZONE from config.

    This condition is intentionally used ONLY by the hard refresh API.
    It does not modify or control main.py's scheduled Opening Range flow.
    """
    market_now = get_current_market_datetime()

    market_time = market_now.time()

    opening_range_start = time(9, 15)
    opening_range_end = time(15, 45)

    allowed = opening_range_start <= market_time <= opening_range_end

    return allowed, market_now


# ============================================================
# MANUAL MARKET HARD REFRESH
# ============================================================


@router.post("/refresh/manual")
async def manual_market_refresh():
    """
    Manually triggers market hard refresh.

    Steps:

    1. Refresh token document from MongoDB.
    2. Load latest token into memory.
    3. Fetch latest option contracts.
    4. Filter instruments by configured strike range.
    5. Update options_cache and subscribed_keys.
    6. Validate subscribed instruments.
    7. Fetch historical candles for all subscribed instruments.
    8. Calculate historical EMA and initialize live EMA state.
    9. Restart Upstox streamer so latest keys are subscribed.
    10. Send token refresh Telegram status.
    11. Send instrument/subscription Telegram status.
    12. Send historical EMA Telegram status.
    13. Send hard refresh success/failure Telegram status.

    Additional Opening Range flow:

    Steps 14-21 are executed ONLY when the current time in
    config.MARKET_TIMEZONE is between 09:15 and 15:45 inclusive.

    14. Calculate Opening Range.
    15. Calculate R1/S1 levels.
    16. Calculate R2/S2 levels.
    17. Calculate R3/S3 levels.
    18. Scan previous candles for configured level touches.
    19. Select isolated instrument according to the existing
        Opening Range service logic.
    20. Save Opening Range results.
    21. Update Opening Range in-memory cache.

    IMPORTANT:
    - This time-window condition exists only in this hard refresh API.
    - main.py is not modified by this logic.
    - The existing Opening Range service remains responsible for
      the actual Opening Range calculations, touch processing,
      isolation, storage, and cache updates.
    """

    global _last_manual_refresh

    if _manual_refresh_lock.locked():
        raise HTTPException(
            status_code=409,
            detail=(
                "Manual refresh is already running. " "Please wait for it to complete."
            ),
        )

    async with _manual_refresh_lock:
        started_at = datetime.now(timezone.utc).isoformat()

        logger.info(
            "================ MANUAL MARKET HARD REFRESH STARTED ================"
        )

        telegram_service.send_message(
            title="Manual Market Hard Refresh Started",
            message=(
                "Manual hard refresh started from API.\n\n"
                "Core Actions:\n"
                "1. Refresh token from MongoDB\n"
                "2. Fetch latest option instruments\n"
                "3. Filter configured strike range\n"
                "4. Update subscription cache\n"
                "5. Fetch historical candles and calculate EMA\n"
                "6. Initialize live EMA state for all instruments\n"
                "7. Restart Upstox streamer\n\n"
                "Opening Range Hard Refresh Rule:\n"
                "8. Opening Range steps 14-21 run only when market time "
                "is between 09:15 and 15:45 using MARKET_TIMEZONE.\n\n"
                "Note: EMA calculation runs for all instruments. "
                "Telegram EMA alerts are sent only for the isolated "
                "Opening Range instrument."
            ),
            level="REFRESH",
        )

        history_summary = None
        opening_range_summary = None
        opening_range_executed = False
        opening_range_market_time = None
        opening_range_timezone = getattr(
            config,
            "MARKET_TIMEZONE",
            "Asia/Kolkata",
        )

        try:
            # ============================================================
            # 1. Refresh token from MongoDB
            # ============================================================

            logger.info("Manual refresh: refreshing token document from MongoDB...")

            await run_in_threadpool(token_service.refresh_tokens)

            current_token = token_service.get_access_token()
            token_doc = token_service.get_token_document()

            if not current_token:
                error_message = "Manual refresh failed: " "No access token available."

                logger.error(error_message)

                telegram_service.send_token_refresh_message(
                    success=False,
                    error=error_message,
                )

                telegram_service.send_daily_refresh_message(
                    success=False,
                    error=error_message,
                )

                _last_manual_refresh = {
                    "status": "failed",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "message": error_message,
                    "subscribed_instruments": 0,
                    "nearest_expiry": options_cache.get("nearest_expiry"),
                    "historical_ema_status": None,
                    "live_ema_initialized": None,
                    "opening_range_status": None,
                    "opening_range_executed": False,
                    "opening_range_market_time": None,
                    "opening_range_timezone": opening_range_timezone,
                }

                raise HTTPException(
                    status_code=500,
                    detail=error_message,
                )

            logger.info("Manual refresh: token refreshed into memory successfully.")

            telegram_service.send_token_refresh_message(
                success=True,
                updated_at=(token_doc.get("updated_at") if token_doc else "N/A"),
            )

            # ============================================================
            # 2. Fetch latest option contracts and update options_cache
            # ============================================================

            logger.info("Manual refresh: fetching latest option contracts...")

            result = await run_in_threadpool(
                get_options_contracts,
                save_data=True,
            )

            if not result:
                error_message = (
                    "Manual refresh failed: "
                    "Option contract fetch returned no result."
                )

                logger.error(error_message)

                telegram_service.send_instruments_fetched_message(
                    success=False,
                    error=error_message,
                )

                telegram_service.send_daily_refresh_message(
                    success=False,
                    error=error_message,
                )

                _last_manual_refresh = {
                    "status": "failed",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "message": error_message,
                    "subscribed_instruments": 0,
                    "nearest_expiry": options_cache.get("nearest_expiry"),
                    "historical_ema_status": None,
                    "live_ema_initialized": None,
                    "opening_range_status": None,
                    "opening_range_executed": False,
                    "opening_range_market_time": None,
                    "opening_range_timezone": opening_range_timezone,
                }

                raise HTTPException(
                    status_code=500,
                    detail=error_message,
                )

            subscribed_keys = options_cache.get(
                "subscribed_keys",
                [],
            )

            if not subscribed_keys:
                error_message = (
                    "Manual refresh failed: "
                    "No subscribed keys found after contract reload."
                )

                logger.error(error_message)

                telegram_service.send_daily_refresh_message(
                    success=False,
                    error=error_message,
                )

                _last_manual_refresh = {
                    "status": "failed",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "message": error_message,
                    "subscribed_instruments": 0,
                    "nearest_expiry": options_cache.get("nearest_expiry"),
                    "historical_ema_status": None,
                    "live_ema_initialized": None,
                    "opening_range_status": None,
                    "opening_range_executed": False,
                    "opening_range_market_time": None,
                    "opening_range_timezone": opening_range_timezone,
                }

                raise HTTPException(
                    status_code=500,
                    detail=error_message,
                )

            telegram_service.send_instruments_fetched_message(
                success=True,
                nearest_expiry=options_cache.get("nearest_expiry"),
                total_contracts=options_cache.get(
                    "total_contracts",
                    0,
                ),
                subscribed_keys_count=len(subscribed_keys),
                strike_from=getattr(
                    config,
                    "STRIKE_FROM",
                    "N/A",
                ),
                strike_to=getattr(
                    config,
                    "STRIKE_TO",
                    "N/A",
                ),
            )

            logger.info(
                f"Manual refresh: loaded "
                f"{len(subscribed_keys)} subscribed instruments."
            )

            # ============================================================
            # 3. Fetch historical EMA and initialize live EMA state
            # ============================================================

            logger.info(
                "Manual refresh: fetching historical candles "
                "and initializing live EMA..."
            )

            history_summary = await run_in_threadpool(
                fetch_historical_candles_for_all_subscribed,
                interval=getattr(
                    config,
                    "HISTORICAL_CANDLE_INTERVAL",
                    "1minute",
                ),
                history_days=getattr(
                    config,
                    "HISTORICAL_CANDLE_DAYS",
                    10,
                ),
                save_data=True,
                max_workers=getattr(
                    config,
                    "HISTORICAL_CANDLE_MAX_WORKERS",
                    5,
                ),
            )

            logger.info(
                f"Manual refresh historical EMA completed. "
                f"status={history_summary.get('status')}, "
                f"total_instruments="
                f"{history_summary.get('total_instruments')}, "
                f"success="
                f"{history_summary.get('success_count')}, "
                f"empty="
                f"{history_summary.get('empty_count')}, "
                f"insufficient_data="
                f"{history_summary.get('insufficient_data_count')}, "
                f"failed="
                f"{history_summary.get('failed_count')}, "
                f"total_candles="
                f"{history_summary.get('total_candles')}, "
                f"live_ema_initialized="
                f"{history_summary.get('live_ema_initialized')}"
            )

            telegram_service.send_message(
                title="Manual Historical EMA Refresh Completed",
                message=(
                    f"Status: {history_summary.get('status')}\n"
                    f"From Date: {history_summary.get('from_date')}\n"
                    f"To Date: {history_summary.get('to_date')}\n"
                    f"Interval: {history_summary.get('interval')}\n"
                    f"Total Instruments: "
                    f"{history_summary.get('total_instruments')}\n"
                    f"Success: "
                    f"{history_summary.get('success_count')}\n"
                    f"Empty: "
                    f"{history_summary.get('empty_count')}\n"
                    f"Insufficient Data: "
                    f"{history_summary.get('insufficient_data_count')}\n"
                    f"Failed: "
                    f"{history_summary.get('failed_count')}\n"
                    f"Total Candles: "
                    f"{history_summary.get('total_candles')}\n"
                    f"EMA Fast Period: "
                    f"{history_summary.get('ema_fast_period')}\n"
                    f"EMA Slow Period: "
                    f"{history_summary.get('ema_slow_period')}\n"
                    f"EMA Result File: "
                    f"{history_summary.get('ema_results_file_path', 'not_saved')}\n"
                    f"Live EMA Initialized: "
                    f"{history_summary.get('live_ema_initialized')}\n"
                    f"Telegram EMA Alert Scope: "
                    f"isolated instrument only"
                ),
                level="REFRESH",
            )

            # ============================================================
            # 4. Restart Upstox streamer so latest keys are subscribed
            # ============================================================

            logger.info("Manual refresh: restarting Upstox streamer...")

            if hasattr(upstox_streamer, "restart"):
                await upstox_streamer.restart()
            else:
                await upstox_streamer.stop()

                await asyncio.sleep(2)

                await upstox_streamer.start()

            telegram_service.send_subscription_message(
                success=True,
                subscribed_keys_count=len(subscribed_keys),
                feed_mode=getattr(
                    config,
                    "WEBSOCKET_FEED_MODE",
                    "full",
                ),
            )

            telegram_service.send_daily_refresh_message(
                success=True,
                subscribed_keys_count=len(subscribed_keys),
                nearest_expiry=options_cache.get("nearest_expiry"),
            )

            # ============================================================
            # 5. Opening Range hard-refresh time check
            # ============================================================
            #
            # IMPORTANT:
            # This logic exists ONLY inside the manual hard refresh API.
            #
            # main.py is NOT changed.
            #
            # Steps 14-21 run only between:
            #
            #     09:15 <= market time <= 15:45
            #
            # using config.MARKET_TIMEZONE.
            # ============================================================

            (
                opening_range_allowed,
                market_now,
            ) = is_opening_range_refresh_window()

            opening_range_market_time = market_now.isoformat()

            opening_range_timezone = (
                market_now.tzinfo.key
                if hasattr(
                    market_now.tzinfo,
                    "key",
                )
                else str(market_now.tzinfo)
            )

            logger.info(
                "Manual hard refresh Opening Range time check: "
                f"market_time={market_now.strftime('%Y-%m-%d %H:%M:%S %Z')}, "
                f"timezone={opening_range_timezone}, "
                f"allowed={opening_range_allowed}, "
                f"window=09:15-15:45"
            )

            if opening_range_allowed:
                # ========================================================
                # 6. Opening Range flow
                # ========================================================
                #
                # This common service performs the existing Opening Range
                # calculation flow:
                #
                # 14. Calculate Opening Range
                # 15. Calculate R1/S1
                # 16. Calculate R2/S2
                # 17. Calculate R3/S3
                # 18. Scan previous candles for touches
                # 19. Select isolated instrument
                # 20. Save Opening Range results
                # 21. Update Opening Range cache
                # ========================================================

                opening_range_executed = True

                logger.info(
                    "Manual hard refresh: current market time is "
                    "inside Opening Range execution window. "
                    "Running Opening Range steps 14-21..."
                )

                telegram_service.send_message(
                    title="Hard Refresh Opening Range Started",
                    message=(
                        "Opening Range processing enabled "
                        "for this hard refresh.\n\n"
                        f"Market Time: "
                        f"{market_now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                        f"Timezone: {opening_range_timezone}\n"
                        "Allowed Window: 09:15 - 15:45\n\n"
                        "Actions:\n"
                        "14. Calculate Opening Range\n"
                        "15. Calculate R1/S1 levels\n"
                        "16. Calculate R2/S2 levels\n"
                        "17. Calculate R3/S3 levels\n"
                        "18. Scan previous candles for level touches\n"
                        "19. Select isolated instrument\n"
                        "20. Save Opening Range results\n"
                        "21. Update Opening Range in-memory cache"
                    ),
                    level="REFRESH",
                )

                # --------------------------------------------------------
                # Steps 14-21
                # --------------------------------------------------------
                #
                # Keep the actual Opening Range business logic inside the
                # existing common service. This avoids duplicating the
                # calculation/isolation/storage/cache implementation here.
                # --------------------------------------------------------

                opening_range_summary = await run_in_threadpool(
                    calculate_opening_range_for_all_subscribed,
                    candle_count=getattr(
                        config,
                        "OPENING_RANGE_CANDLE_COUNT",
                        1,
                    ),
                    save_data=getattr(
                        config,
                        "OPENING_RANGE_SAVE_FILE",
                        True,
                    ),
                    max_workers=getattr(
                        config,
                        "OPENING_RANGE_MAX_WORKERS",
                        5,
                    ),
                )

                if not isinstance(
                    opening_range_summary,
                    dict,
                ):
                    opening_range_summary = {
                        "status": "unknown",
                        "raw_result": opening_range_summary,
                    }

                isolated_state = opening_range_summary.get("isolated_instrument") or {}

                isolated_selected = bool(isolated_state.get("selected"))

                logger.info(
                    "Manual hard refresh Opening Range completed. "
                    f"status={opening_range_summary.get('status')}, "
                    f"total_instruments="
                    f"{opening_range_summary.get('total_instruments')}, "
                    f"success="
                    f"{opening_range_summary.get('success_count')}, "
                    f"empty="
                    f"{opening_range_summary.get('empty_count')}, "
                    f"insufficient_data="
                    f"{opening_range_summary.get('insufficient_data_count')}, "
                    f"failed="
                    f"{opening_range_summary.get('failed_count')}, "
                    f"backfill_touch_events="
                    f"{opening_range_summary.get('backfill_touch_events_count', 0)}, "
                    f"latest_main_index_ltp="
                    f"{opening_range_summary.get('latest_main_index_ltp')}, "
                    f"isolated_selected="
                    f"{isolated_selected}, "
                    f"output_file="
                    f"{opening_range_summary.get('output_file_path', 'not_saved')}"
                )

                telegram_service.send_message(
                    title="Hard Refresh Opening Range Completed",
                    message=(
                        f"Status: "
                        f"{opening_range_summary.get('status')}\n"
                        f"Market Time: "
                        f"{market_now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                        f"Timezone: "
                        f"{opening_range_timezone}\n"
                        f"Opening Range Candles: "
                        f"{opening_range_summary.get('opening_range_candle_count')}\n"
                        f"Market Open Time: "
                        f"{opening_range_summary.get('market_open_time')}\n"
                        f"Opening Range End Time: "
                        f"{opening_range_summary.get('opening_range_end_time')}\n"
                        f"Total Instruments: "
                        f"{opening_range_summary.get('total_instruments')}\n"
                        f"Success: "
                        f"{opening_range_summary.get('success_count')}\n"
                        f"Empty: "
                        f"{opening_range_summary.get('empty_count')}\n"
                        f"Insufficient Data: "
                        f"{opening_range_summary.get('insufficient_data_count')}\n"
                        f"Failed: "
                        f"{opening_range_summary.get('failed_count')}\n"
                        f"Backfill Touch Events: "
                        f"{opening_range_summary.get('backfill_touch_events_count', 0)}\n"
                        f"Isolated Instrument Selected: "
                        f"{isolated_selected}\n"
                        f"Output File: "
                        f"{opening_range_summary.get('output_file_path', 'not_saved')}"
                    ),
                    level="REFRESH",
                )

            else:
                # ========================================================
                # Outside Opening Range window
                # ========================================================

                opening_range_executed = False

                logger.info(
                    "Manual hard refresh: Opening Range steps 14-21 "
                    "skipped because current market time is outside "
                    "09:15-15:45."
                )

                telegram_service.send_message(
                    title="Hard Refresh Opening Range Skipped",
                    message=(
                        "Opening Range steps 14-21 were skipped.\n\n"
                        f"Market Time: "
                        f"{market_now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                        f"Timezone: {opening_range_timezone}\n"
                        "Allowed Window: 09:15 - 15:45\n\n"
                        "Core hard refresh steps 1-13 completed normally.\n"
                        "Opening Range was not recalculated because "
                        "the hard refresh was outside the configured "
                        "market-time window."
                    ),
                    level="REFRESH",
                )

            # ============================================================
            # 7. Final hard refresh status
            # ============================================================

            completed_at = datetime.now(timezone.utc).isoformat()

            _last_manual_refresh = {
                "status": "success",
                "timestamp": completed_at,
                "message": ("Manual market hard refresh " "completed successfully."),
                "subscribed_instruments": len(subscribed_keys),
                "nearest_expiry": options_cache.get("nearest_expiry"),
                "historical_ema_status": (
                    history_summary.get("status") if history_summary else None
                ),
                "live_ema_initialized": (
                    history_summary.get("live_ema_initialized")
                    if history_summary
                    else None
                ),
                "opening_range_status": (
                    opening_range_summary.get("status")
                    if opening_range_summary
                    else (
                        "skipped_outside_time_window"
                        if not opening_range_executed
                        else None
                    )
                ),
                "opening_range_executed": (opening_range_executed),
                "opening_range_market_time": (opening_range_market_time),
                "opening_range_timezone": (opening_range_timezone),
            }

            logger.info(
                "================ MANUAL MARKET HARD REFRESH COMPLETED ================"
            )

            return {
                "status": "success",
                "message": ("Manual market hard refresh " "completed successfully."),
                "started_at": started_at,
                "completed_at": completed_at,
                "nearest_expiry": options_cache.get("nearest_expiry"),
                "total_contracts": options_cache.get(
                    "total_contracts",
                    0,
                ),
                "subscribed_instruments": len(subscribed_keys),
                "feed_mode": getattr(
                    config,
                    "WEBSOCKET_FEED_MODE",
                    "full",
                ),
                # ========================================================
                # CORE HARD REFRESH STEPS 1-13
                # ========================================================
                "hard_refresh_flow": {
                    "token_refreshed": True,
                    "token_loaded_into_memory": True,
                    "option_contracts_fetched": True,
                    "strike_range_applied": True,
                    "options_cache_updated": True,
                    "subscribed_instruments_validated": True,
                    "historical_candles_fetched": True,
                    "historical_ema_initialized": True,
                    "live_ema_initialized": (
                        history_summary.get("live_ema_initialized")
                        if history_summary
                        else None
                    ),
                    "upstox_streamer_restarted": True,
                },
                # ========================================================
                # HISTORICAL EMA
                # ========================================================
                "historical_ema": {
                    "status": (
                        history_summary.get("status") if history_summary else None
                    ),
                    "from_date": (
                        history_summary.get("from_date") if history_summary else None
                    ),
                    "to_date": (
                        history_summary.get("to_date") if history_summary else None
                    ),
                    "interval": (
                        history_summary.get("interval") if history_summary else None
                    ),
                    "history_days": (
                        history_summary.get("history_days") if history_summary else None
                    ),
                    "total_instruments": (
                        history_summary.get("total_instruments")
                        if history_summary
                        else None
                    ),
                    "success_count": (
                        history_summary.get("success_count")
                        if history_summary
                        else None
                    ),
                    "empty_count": (
                        history_summary.get("empty_count") if history_summary else None
                    ),
                    "insufficient_data_count": (
                        history_summary.get("insufficient_data_count")
                        if history_summary
                        else None
                    ),
                    "failed_count": (
                        history_summary.get("failed_count") if history_summary else None
                    ),
                    "total_candles": (
                        history_summary.get("total_candles")
                        if history_summary
                        else None
                    ),
                    "ema_fast_period": (
                        history_summary.get("ema_fast_period")
                        if history_summary
                        else None
                    ),
                    "ema_slow_period": (
                        history_summary.get("ema_slow_period")
                        if history_summary
                        else None
                    ),
                    "live_ema_initialized": (
                        history_summary.get("live_ema_initialized")
                        if history_summary
                        else None
                    ),
                    "ema_results_file_path": (
                        history_summary.get(
                            "ema_results_file_path",
                            "not_saved",
                        )
                        if history_summary
                        else "not_saved"
                    ),
                },
                # ========================================================
                # OPENING RANGE STEPS 14-21
                # ========================================================
                "opening_range_flow": {
                    "time_window_enabled": True,
                    "market_timezone": (opening_range_timezone),
                    "market_time": (opening_range_market_time),
                    "allowed_window": ("09:15:00 - 15:45:00"),
                    "executed": (opening_range_executed),
                    "step_14_calculate_opening_range": (opening_range_executed),
                    "step_15_calculate_r1_s1": (opening_range_executed),
                    "step_16_calculate_r2_s2": (opening_range_executed),
                    "step_17_calculate_r3_s3": (opening_range_executed),
                    "step_18_scan_level_touches": (opening_range_executed),
                    "step_19_select_isolated_instrument": (opening_range_executed),
                    "step_20_save_opening_range_results": (opening_range_executed),
                    "step_21_update_opening_range_cache": (opening_range_executed),
                    "status": (
                        opening_range_summary.get("status")
                        if opening_range_summary
                        else (
                            "skipped_outside_time_window"
                            if not opening_range_executed
                            else None
                        )
                    ),
                    "summary": (
                        opening_range_summary if opening_range_summary else None
                    ),
                },
                # ========================================================
                # EXISTING ISOLATED INSTRUMENT FLOW
                # ========================================================
                "isolated_instrument_flow": {
                    "enabled": getattr(
                        config,
                        "OPENING_RANGE_ISOLATED_INSTRUMENT_ENABLED",
                        True,
                    ),
                    "ema_telegram_alerts_enabled": getattr(
                        config,
                        "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED",
                        True,
                    ),
                    "opening_range_processed_in_this_refresh": (opening_range_executed),
                    "message": (
                        "Manual hard refresh reloads instruments "
                        "and EMA state. Opening Range steps 14-21 "
                        "are additionally executed only when the "
                        "current market time in MARKET_TIMEZONE is "
                        "between 09:15 and 15:45 inclusive."
                    ),
                },
            }

        except HTTPException:
            raise

        except Exception as ex:
            error_message = f"{type(ex).__name__}: {ex}"

            logger.error("Manual market hard refresh failed: " f"{error_message}")

            telegram_service.send_exception_message(
                title="Manual Market Hard Refresh Failed",
                exception=ex,
                context="manual_market_refresh",
            )

            telegram_service.send_daily_refresh_message(
                success=False,
                error=error_message,
            )

            _last_manual_refresh = {
                "status": "failed",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": error_message,
                "subscribed_instruments": len(
                    options_cache.get(
                        "subscribed_keys",
                        [],
                    )
                ),
                "nearest_expiry": options_cache.get("nearest_expiry"),
                "historical_ema_status": (
                    history_summary.get("status") if history_summary else None
                ),
                "live_ema_initialized": (
                    history_summary.get("live_ema_initialized")
                    if history_summary
                    else None
                ),
                "opening_range_status": (
                    opening_range_summary.get("status")
                    if opening_range_summary
                    else None
                ),
                "opening_range_executed": (opening_range_executed),
                "opening_range_market_time": (opening_range_market_time),
                "opening_range_timezone": (opening_range_timezone),
            }

            raise HTTPException(
                status_code=500,
                detail=("Manual market hard refresh failed: " f"{error_message}"),
            )

        finally:
            logger.info(
                "================ MANUAL MARKET HARD REFRESH EXITED ================"
            )


# ============================================================
# MANUAL REFRESH STATUS
# ============================================================


@router.get("/refresh/status")
async def get_manual_refresh_status():
    """
    Returns latest manual refresh status.
    """

    return {
        "manual_refresh_running": (_manual_refresh_lock.locked()),
        "last_manual_refresh": (_last_manual_refresh),
        "current_cache": {
            "nearest_expiry": (options_cache.get("nearest_expiry")),
            "total_contracts": (options_cache.get("total_contracts")),
            "subscribed_keys_count": len(
                options_cache.get(
                    "subscribed_keys",
                    [],
                )
            ),
        },
        "current_flow": {
            "historical_ema_refresh_in_manual_refresh": True,
            "live_ema_runs_for_all_instruments": getattr(
                config,
                "LIVE_EMA_ENABLED",
                True,
            ),
            "opening_range_isolated_instrument_enabled": getattr(
                config,
                "OPENING_RANGE_ISOLATED_INSTRUMENT_ENABLED",
                True,
            ),
            "isolated_ema_telegram_alerts_enabled": getattr(
                config,
                "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED",
                True,
            ),
            "ema_websocket_opening_range_enrichment": getattr(
                config,
                "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
                True,
            ),
            "opening_range_hard_refresh_time_window": {
                "enabled": True,
                "timezone": getattr(
                    config,
                    "MARKET_TIMEZONE",
                    "Asia/Kolkata",
                ),
                "start": "09:15:00",
                "end": "15:45:00",
                "inclusive": True,
                "description": (
                    "Opening Range steps 14-21 are executed "
                    "during hard refresh only when current "
                    "market time is inside this window."
                ),
            },
        },
    }
