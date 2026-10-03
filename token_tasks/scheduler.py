"""
token_tasks/scheduler.py
Background asyncio task that refreshes the token cache every N seconds
(default 30 minutes). Started from main.lifespan.
"""
import asyncio
from typing import Optional

from core.logger import get_logger
from token_tasks.config import token_config
from token_tasks.service import token_service

logger = get_logger(__name__)

_refresh_task: Optional[asyncio.Task] = None


async def _refresh_loop() -> None:
    interval = token_config.REFRESH_INTERVAL_SECONDS
    logger.info("Token refresh loop started | interval=%ss", interval)

    while True:
        try:
            await asyncio.sleep(interval)
            logger.info("Scheduled token refresh triggered")
            # pymongo is sync → run in threadpool to keep loop responsive
            await asyncio.to_thread(token_service.refresh_token)
        except asyncio.CancelledError:
            logger.info("Token refresh loop cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error inside token refresh loop: %s", exc)


def start_scheduler() -> None:
    """Start the background refresh task (no-op if already running)."""
    global _refresh_task
    if _refresh_task and not _refresh_task.done():
        logger.warning("Token scheduler already running — skipping start")
        return
    _refresh_task = asyncio.create_task(_refresh_loop(), name="token-refresh-loop")
    logger.info("Token scheduler started")


async def stop_scheduler() -> None:
    """Cancel the background refresh task gracefully."""
    global _refresh_task
    if _refresh_task is None:
        logger.info("Token scheduler not running")
        return

    logger.info("Stopping token scheduler...")
    _refresh_task.cancel()
    try:
        await _refresh_task
    except asyncio.CancelledError:
        pass
    finally:
        _refresh_task = None
    logger.info("Token scheduler stopped")


def is_running() -> bool:
    return _refresh_task is not None and not _refresh_task.done()