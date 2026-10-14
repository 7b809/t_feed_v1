"""Admin endpoints — hard refresh, backup status."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter

from services.backup_data_service import backup_data_service
from services.instrument_registry import instrument_registry
from services.live_cross_store import live_cross_store
from services.live_feed_service import live_feed_service
from services.telegram_service import telegram_service

logger = logging.getLogger(__name__)

router = APIRouter()


def _run_hard_refresh_sync() -> dict[str, Any]:
    """Synchronous worker used by both the API and the scheduler."""
    telegram_service.send_message(
        "Hard Refresh Started",
        "Downloading latest backup from MongoDB and resubscribing live feed.",
        level="STARTUP",
    )

    backup = backup_data_service.refresh()
    registry = instrument_registry.load()
    live_cross_store.reset_all()

    feed_ok = False
    try:
        feed_ok = live_feed_service.restart()
    except Exception:
        logger.exception("Feed restart failed during hard refresh")

    result = {
        "backup": backup,
        "registry": registry,
        "feed_restarted": feed_ok,
    }

    status = "SUCCESS" if backup.get("status") == "ok" else "WARNING"
    message = (
        f"Backup status: {backup.get('status')}\n"
        f"Files extracted: {backup.get('file_count')}\n"
        f"Instruments loaded: {registry.get('count')}\n"
        f"Live feed running: {feed_ok}"
    )
    telegram_service.send_message(
        "Hard Refresh Completed" if status == "SUCCESS" else "Hard Refresh Finished",
        message,
        level=status,
    )
    return result


@router.post("/admin/hard-refresh")
async def hard_refresh() -> dict[str, Any]:
    """Download latest backup, reload registry, resubscribe live feed."""
    return await asyncio.to_thread(_run_hard_refresh_sync)


@router.get("/admin/backup-status")
def backup_status() -> dict[str, Any]:
    return backup_data_service.last_status()