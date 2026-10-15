"""Admin endpoints — hard refresh, backup status, last cleanup status.

    POST /admin/hard-refresh          -> full refresh incl. folder cleanup
    GET  /admin/backup-status         -> last backup extraction summary
    GET  /admin/cleanup-status        -> last folder-cleanup summary
    POST /admin/cleanup               -> run the folder cleanup standalone
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter

from services.backup_data_service import backup_data_service
from services.cleanup_service import cleanup_service
from services.instrument_registry import instrument_registry
from services.live_cross_store import live_cross_store
from services.live_feed_service import live_feed_service
from services.telegram_service import telegram_service

logger = logging.getLogger(__name__)

router = APIRouter()

def _run_hard_refresh_sync() -> dict[str, Any]:
    """Synchronous worker used by both the API and the scheduler.

    Order:
        1. Cleanup transient folders (data/, temp/, downloads/, ...).
           Reserved folders (meta_data/, logs/, .git/, ...) are always
           preserved.
        2. Download the latest backup and re-extract into data/.
        3. Reload the instrument registry.
        4. Reset today's live-cross state.
        5. Restart the live feed.
    """
    telegram_service.send_message(
        "Hard Refresh Started",
        "Cleaning transient folders, then downloading latest backup and resubscribing live feed.",
        level="STARTUP",
    )

    # ---- 1. Folder cleanup ------------------------------------------------
    try:
        cleanup = cleanup_service.run(reason="hard_refresh")
        logger.info("Hard refresh cleanup: %s", cleanup)
    except Exception as exc:
        logger.exception("Folder cleanup failed during hard refresh")
        cleanup = {
            "reason": "hard_refresh",
            "removed": [],
            "skipped": [],
            "errors": [f"{type(exc).__name__}: {exc}"],
        }

    # ---- 2. Backup + registry --------------------------------------------
    backup = backup_data_service.refresh()
    registry = instrument_registry.load()
    live_cross_store.reset_all()

    # ---- 3. Live feed restart --------------------------------------------
    feed_ok = False
    try:
        feed_ok = live_feed_service.restart()
    except Exception:
        logger.exception("Feed restart failed during hard refresh")

    result = {
        "cleanup": cleanup,
        "backup": backup,
        "registry": registry,
        "feed_restarted": feed_ok,
    }

    status = "SUCCESS" if backup.get("status") == "ok" else "WARNING"
    message = (
        f"Cleanup removed: {len(cleanup.get('removed') or [])}  "
        f"preserved: {len(cleanup.get('skipped') or [])}  "
        f"errors: {len(cleanup.get('errors') or [])}\n"
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
    """Cleanup, then download latest backup, reload registry, resubscribe feed."""
    return await asyncio.to_thread(_run_hard_refresh_sync)

@router.get("/admin/backup-status")
def backup_status() -> dict[str, Any]:
    return backup_data_service.last_status()

@router.get("/admin/cleanup-status")
def cleanup_status() -> dict[str, Any]:
    """Return the summary of the most recent folder-cleanup pass."""
    return cleanup_service.last_status()

@router.post("/admin/cleanup")
async def run_cleanup() -> dict[str, Any]:
    """Run the folder cleanup standalone (no backup / no feed restart)."""
    return await asyncio.to_thread(cleanup_service.run, "manual")