"""Admin endpoints + the unified refresh routine.

    POST /admin/hard-refresh          -> full refresh (unified routine)
    GET  /admin/backup-status         -> last backup extraction summary
    GET  /admin/cleanup-status        -> last folder-cleanup summary
    POST /admin/cleanup               -> run the folder cleanup standalone

The unified refresh routine ``_run_full_refresh_sync`` is the **single
source of truth** for every task that must run in all three states:

    Project startup  ->  app_lifespan calls it with reason="startup"
    Daily refresh    ->  token_tasks.daily_refresh calls it with reason="daily"
    Hard refresh     ->  POST /admin/hard-refresh calls it with reason="manual"

Because it is idempotent, calling it repeatedly from any trigger point
is safe: the scheduler is a singleton, the Telegram bot returns True if
already running, live-feed restart short-circuits when not running, and
the cross handler wiring simply replaces the handler reference.

Task list (all three states):
    1.  Telegram start notification
    2.  Cleanup transient folders
    3.  Refresh token cache from MongoDB
    4.  Validate Upstox token
    5.  Backup extract from MongoDB
    6.  Reload instrument registry
    7.  Wire broadcast loop + cross handler
    8.  Reset today's live-cross state
    9.  EMA backfill seed (backup + intraday)
    10. Opening Range (OR) calculation
    11. Start / restart live feed
    12. Start scheduler (idempotent)
    13. Start Telegram bot (idempotent)
    14. Telegram completion notification
    15. Return structured result
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter

from services.backup_data_service import backup_data_service
from services.cleanup_service import cleanup_service
from services.instrument_registry import instrument_registry
from services.live_backfill_service import live_backfill_service
from services.live_broadcast import live_broadcast
from services.live_cross_store import live_cross_store
from services.live_ema_engine import live_ema_engine
from services.live_feed_service import live_feed_service
from services.opening_range_service import opening_range_service
from services.scheduler_service import scheduler_service
from services.telegram_service import telegram_service
from services.token_service import token_service
from token_tasks.telegram_token_bot import telegram_token_bot
from token_tasks.token_monitor import check_upstox_token_validity

logger = logging.getLogger(__name__)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Cross handler wiring                                                         #
# --------------------------------------------------------------------------- #
def _wire_cross_handler() -> None:
    """Install the cross persistence + broadcast handler on the engine.

    Idempotent — re-wiring simply replaces the handler reference.
    """

    def on_cross(entry, cross):
        try:
            live_cross_store.save(entry, cross)
        except Exception:
            logger.exception(
                "Failed persisting live cross for %s", entry["instrument_key"]
            )
        try:
            live_broadcast.publish_sync("crosses", entry, {"cross": cross})
        except Exception:
            logger.exception("Failed broadcasting live cross")

    live_ema_engine.set_cross_handler(on_cross)


# --------------------------------------------------------------------------- #
# Unified refresh                                                              #
# --------------------------------------------------------------------------- #
def _run_full_refresh_sync(
    reason: str = "manual",
    loop: "asyncio.AbstractEventLoop | None" = None,
) -> dict[str, Any]:
    """Execute every refresh task in the canonical order.

    The exact same task list runs for project startup, the scheduled
    daily refresh, and the manual hard-refresh endpoint. Each step is
    wrapped so a single failure does not abort the rest of the
    routine — the returned summary reports per-step status.
    """
    label = reason.title()
    telegram_service.send_message(
        f"{label} Refresh Started",
        (
            "Running unified refresh: cleanup → token → backup → "
            "registry → wiring → OR levels → backfill → feed → "
            "scheduler → telegram."
        ),
        level="STARTUP",
    )

    # ---- 1. Cleanup transient folders ------------------------------------
    try:
        cleanup = cleanup_service.run(reason=reason)
        logger.info("[%s] cleanup: %s", reason, cleanup)
    except Exception as exc:
        logger.exception("[%s] cleanup failed", reason)
        cleanup = {
            "reason": reason,
            "removed": [],
            "skipped": [],
            "errors": [f"{type(exc).__name__}: {exc}"],
        }

    # ---- 2. Token cache refresh ------------------------------------------
    token_refresh_ok = True
    try:
        token_service.refresh_tokens()
    except Exception:
        logger.exception("[%s] token refresh failed", reason)
        token_refresh_ok = False

    # ---- 3. Token validity check -----------------------------------------
    token_valid = False
    token_status = "not_checked"
    try:
        token_result = check_upstox_token_validity(send_success_message=False)
        token_valid = bool(token_result.get("valid"))
        token_status = str(token_result.get("status", "unknown"))
    except Exception as exc:
        logger.exception("[%s] token validation failed", reason)
        token_status = f"error:{type(exc).__name__}"

    # ---- 4. Backup extract + registry reload -----------------------------
    backup = backup_data_service.refresh()
    registry = instrument_registry.load()

    # ---- 5. Wire broadcast loop + cross handler --------------------------
    if loop is not None:
        try:
            live_broadcast.set_loop(loop)
            live_feed_service.set_loop(loop)
        except Exception:
            logger.exception("[%s] loop wiring failed", reason)
    try:
        _wire_cross_handler()
    except Exception:
        logger.exception("[%s] cross handler wiring failed", reason)

    # ---- 6. Reset today's live-cross state -------------------------------
    try:
        live_cross_store.reset_all()
    except Exception:
        logger.exception("[%s] live-cross reset failed", reason)

    # ---- 7. EMA backfill seed --------------------------------------------
    backfill_summary: dict[str, Any] = {
        "seeded": 0,
        "crosses": 0,
        "skipped": 0,
        "errors": 0,
        "total": 0,
    }
    try:
        backfill_summary = live_backfill_service.seed_all()
    except Exception:
        logger.exception("[%s] EMA backfill failed", reason)

    # ---- 8. Opening Range calculation ------------------------------------
    or_summary: dict[str, Any] = {
        "status": "not_run",
        "total": 0,
        "calculated": 0,
        "skipped": 0,
        "errors": 0,
    }
    try:
        or_summary = opening_range_service.calculate_all()
    except Exception:
        logger.exception("[%s] opening range calculation failed", reason)

    # ---- 9. Start / restart live feed ------------------------------------
    feed_ok = False
    try:
        feed_running = bool(live_feed_service.status().get("running"))
        if feed_running:
            feed_ok = live_feed_service.restart()
        else:
            feed_ok = live_feed_service.start()
    except Exception:
        logger.exception("[%s] live feed start/restart failed", reason)

    # ---- 10. Start scheduler (idempotent) --------------------------------
    scheduler_ok = True
    try:
        scheduler_service.start()
    except Exception:
        logger.exception("[%s] scheduler start failed", reason)
        scheduler_ok = False

    # ---- 11. Start Telegram token bot (idempotent) -----------------------
    telegram_ok = True
    try:
        telegram_token_bot.start()
    except Exception:
        logger.exception("[%s] telegram bot start failed", reason)
        telegram_ok = False

    result: dict[str, Any] = {
        "reason": reason,
        "cleanup": cleanup,
        "token_refresh_ok": token_refresh_ok,
        "token_valid": token_valid,
        "token_status": token_status,
        "backup": backup,
        "registry": registry,
        "backfill": backfill_summary,
        "opening_range": or_summary,
        "feed_restarted": feed_ok,
        "scheduler_started": scheduler_ok,
        "telegram_started": telegram_ok,
    }

    # ---- 12. Telegram completion notification ----------------------------
    status = "SUCCESS" if backup.get("status") == "ok" else "WARNING"
    message = (
        f"Cleanup removed: {len(cleanup.get('removed') or [])}  "
        f"preserved: {len(cleanup.get('skipped') or [])}  "
        f"errors: {len(cleanup.get('errors') or [])}\n"
        f"Token refresh: {token_refresh_ok}  valid: {token_valid}  status: {token_status}\n"
        f"Backup status: {backup.get('status')}  files: {backup.get('file_count')}\n"
        f"Instruments loaded: {registry.get('count')}\n"
        f"EMA seeded: {backfill_summary.get('seeded')}/{backfill_summary.get('total')}  "
        f"crosses: {backfill_summary.get('crosses')}\n"
        f"OR levels: {or_summary.get('calculated')}/{or_summary.get('total')}  "
        f"skipped: {or_summary.get('skipped')}  errors: {or_summary.get('errors')}\n"
        f"Live feed running: {feed_ok}\n"
        f"Scheduler: {scheduler_ok}  Telegram bot: {telegram_ok}"
    )
    telegram_service.send_message(
        f"{label} Refresh Completed" if status == "SUCCESS"
        else f"{label} Refresh Finished",
        message,
        level=status,
    )
    return result


# --------------------------------------------------------------------------- #
# HTTP routes                                                                  #
# --------------------------------------------------------------------------- #
@router.post("/admin/hard-refresh")
async def hard_refresh() -> dict[str, Any]:
    """Unified refresh triggered from the API.

    Executes the same task list as startup and daily refresh.
    """
    return await asyncio.to_thread(_run_full_refresh_sync, "manual", None)


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