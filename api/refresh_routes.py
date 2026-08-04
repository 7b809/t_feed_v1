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

logger = get_logger(__file__)

router = APIRouter()

_manual_refresh_lock = asyncio.Lock()

_last_manual_refresh = {
    "status": "not_started",
    "timestamp": None,
    "message": "Manual refresh has not been triggered yet.",
    "subscribed_instruments": 0,
    "nearest_expiry": None,
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
    6. Restart Upstox streamer so latest keys are subscribed.
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
                "5. Restart Upstox streamer"
            ),
            level="REFRESH",
        )

        try:
            # 1. Refresh token from MongoDB
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
                }

                raise HTTPException(status_code=500, detail=error_message)

            logger.info("Manual refresh: token refreshed into memory successfully.")

            telegram_service.send_token_refresh_message(
                success=True,
                updated_at=token_doc.get("updated_at") if token_doc else "N/A",
            )

            # 2. Fetch latest option contracts and update options_cache
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

            # 3. Restart Upstox streamer so latest keys are actually subscribed
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
    }
