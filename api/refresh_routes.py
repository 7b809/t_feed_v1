import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from core import config
from core.logger import get_logger
from services.option_service import get_options_contracts, options_cache
from services.token_service import token_service
from services.upstox_websocket import upstox_streamer
from services.telegram_service import telegram_service
from services.history_service import fetch_historical_candles_for_all_subscribed

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
}


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
    6. Fetch historical candles for all subscribed instruments.
    7. Calculate historical EMA and initialize live EMA state.
    8. Restart Upstox streamer so latest keys are subscribed.

    Current flow:
    - Live EMA runs for all subscribed instruments.
    - Opening Range later isolates one instrument based on R2/R3/S2/S3 touch logic.
    - Telegram EMA alerts are sent only for the isolated Opening Range instrument.
    - WebSocket EMA events can still be broadcast for all instruments.
    """

    global _last_manual_refresh

    if _manual_refresh_lock.locked():
        raise HTTPException(
            status_code=409,
            detail="Manual refresh is already running. Please wait for it to complete.",
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
                "Actions:\n"
                "1. Refresh token from MongoDB\n"
                "2. Fetch latest option instruments\n"
                "3. Filter configured strike range\n"
                "4. Update subscription cache\n"
                "5. Fetch historical candles and calculate EMA\n"
                "6. Initialize live EMA state for all instruments\n"
                "7. Restart Upstox streamer\n\n"
                "Note: EMA calculation runs for all instruments. "
                "Telegram EMA alerts are sent only for the isolated Opening Range instrument."
            ),
            level="REFRESH",
        )

        history_summary = None

        try:
            # ============================================================
            # 1. Refresh token from MongoDB
            # ============================================================

            logger.info("Manual refresh: refreshing token document from MongoDB...")
            await run_in_threadpool(token_service.refresh_tokens)

            current_token = token_service.get_access_token()
            token_doc = token_service.get_token_document()

            if not current_token:
                error_message = "Manual refresh failed: No access token available."

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
                }

                raise HTTPException(status_code=500, detail=error_message)

            logger.info("Manual refresh: token refreshed into memory successfully.")

            telegram_service.send_token_refresh_message(
                success=True,
                updated_at=token_doc.get("updated_at") if token_doc else "N/A",
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
                    "Manual refresh failed: Option contract fetch returned no result."
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
                }

                raise HTTPException(status_code=500, detail=error_message)

            subscribed_keys = options_cache.get("subscribed_keys", [])

            if not subscribed_keys:
                error_message = "Manual refresh failed: No subscribed keys found after contract reload."

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
                }

                raise HTTPException(status_code=500, detail=error_message)

            telegram_service.send_instruments_fetched_message(
                success=True,
                nearest_expiry=options_cache.get("nearest_expiry"),
                total_contracts=options_cache.get("total_contracts", 0),
                subscribed_keys_count=len(subscribed_keys),
                strike_from=getattr(config, "STRIKE_FROM", "N/A"),
                strike_to=getattr(config, "STRIKE_TO", "N/A"),
            )

            logger.info(
                f"Manual refresh: loaded {len(subscribed_keys)} subscribed instruments."
            )

            # ============================================================
            # 3. Fetch historical EMA and initialize live EMA state
            # ============================================================

            logger.info(
                "Manual refresh: fetching historical candles and initializing live EMA..."
            )

            history_summary = await run_in_threadpool(
                fetch_historical_candles_for_all_subscribed,
                interval=getattr(config, "HISTORICAL_CANDLE_INTERVAL", "1minute"),
                history_days=getattr(config, "HISTORICAL_CANDLE_DAYS", 10),
                save_data=True,
                max_workers=getattr(config, "HISTORICAL_CANDLE_MAX_WORKERS", 5),
            )

            logger.info(
                f"Manual refresh historical EMA completed. "
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
                title="Manual Historical EMA Refresh Completed",
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
                    f"Telegram EMA Alert Scope: isolated instrument only"
                ),
                level="REFRESH",
            )

            # ============================================================
            # 4. Restart Upstox streamer so latest keys are actually subscribed
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
                feed_mode=getattr(config, "WEBSOCKET_FEED_MODE", "full"),
            )

            telegram_service.send_daily_refresh_message(
                success=True,
                subscribed_keys_count=len(subscribed_keys),
                nearest_expiry=options_cache.get("nearest_expiry"),
            )

            completed_at = datetime.now(timezone.utc).isoformat()

            _last_manual_refresh = {
                "status": "success",
                "timestamp": completed_at,
                "message": "Manual market hard refresh completed successfully.",
                "subscribed_instruments": len(subscribed_keys),
                "nearest_expiry": options_cache.get("nearest_expiry"),
                "historical_ema_status": history_summary.get("status"),
                "live_ema_initialized": history_summary.get("live_ema_initialized"),
            }

            logger.info(
                "================ MANUAL MARKET HARD REFRESH COMPLETED ================"
            )

            return {
                "status": "success",
                "message": "Manual market hard refresh completed successfully.",
                "started_at": started_at,
                "completed_at": completed_at,
                "nearest_expiry": options_cache.get("nearest_expiry"),
                "total_contracts": options_cache.get("total_contracts", 0),
                "subscribed_instruments": len(subscribed_keys),
                "feed_mode": getattr(config, "WEBSOCKET_FEED_MODE", "full"),
                "historical_ema": {
                    "status": history_summary.get("status"),
                    "from_date": history_summary.get("from_date"),
                    "to_date": history_summary.get("to_date"),
                    "interval": history_summary.get("interval"),
                    "history_days": history_summary.get("history_days"),
                    "total_instruments": history_summary.get("total_instruments"),
                    "success_count": history_summary.get("success_count"),
                    "empty_count": history_summary.get("empty_count"),
                    "insufficient_data_count": history_summary.get(
                        "insufficient_data_count"
                    ),
                    "failed_count": history_summary.get("failed_count"),
                    "total_candles": history_summary.get("total_candles"),
                    "ema_fast_period": history_summary.get("ema_fast_period"),
                    "ema_slow_period": history_summary.get("ema_slow_period"),
                    "live_ema_initialized": history_summary.get("live_ema_initialized"),
                    "ema_results_file_path": history_summary.get(
                        "ema_results_file_path",
                        "not_saved",
                    ),
                },
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
                    "message": (
                        "Manual refresh reloads instruments and EMA state. "
                        "Opening Range isolated instrument selection is evaluated "
                        "after Opening Range fetch or live R2/R3/S2/S3 touches."
                    ),
                },
            }

        except HTTPException:
            raise

        except Exception as ex:
            error_message = f"{type(ex).__name__}: {ex}"

            logger.error(f"Manual market hard refresh failed: {error_message}")

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
                "subscribed_instruments": len(options_cache.get("subscribed_keys", [])),
                "nearest_expiry": options_cache.get("nearest_expiry"),
                "historical_ema_status": (
                    history_summary.get("status") if history_summary else None
                ),
                "live_ema_initialized": (
                    history_summary.get("live_ema_initialized")
                    if history_summary
                    else None
                ),
            }

            raise HTTPException(
                status_code=500,
                detail=f"Manual market hard refresh failed: {error_message}",
            )

        finally:
            logger.info(
                "================ MANUAL MARKET HARD REFRESH EXITED ================"
            )


@router.get("/refresh/status")
async def get_manual_refresh_status():
    """Returns latest manual refresh status."""

    return {
        "manual_refresh_running": _manual_refresh_lock.locked(),
        "last_manual_refresh": _last_manual_refresh,
        "current_cache": {
            "nearest_expiry": options_cache.get("nearest_expiry"),
            "total_contracts": options_cache.get("total_contracts"),
            "subscribed_keys_count": len(options_cache.get("subscribed_keys", [])),
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
        },
    }
