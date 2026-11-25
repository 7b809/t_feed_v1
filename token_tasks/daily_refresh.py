# token_tasks/daily_refresh.py
"""Daily refresh job — runs Mon–Fri at the configured IST time.

Delegates to the unified refresh routine in ``routes.admin_routes``
so the daily refresh executes exactly the same task list as project
startup and manual hard refresh.

After completion, the job marks ``meta_data/last_daily_refresh.json``
so a later startup can detect "already ran today" and skip the
backfill.

Note on ``loop=None``
---------------------
``_run_full_refresh_sync(reason, loop)`` accepts an optional asyncio
loop used to (re-)wire ``live_broadcast`` and ``live_feed_service``
for thread-safe WebSocket publishing. This job runs from the
APScheduler background thread, which has no asyncio loop of its own,
so it passes ``loop=None``. That is correct and safe:

  * If the app is up, startup already wired the loop, and the
    ``LiveFeedService._loop`` / ``live_broadcast._loop`` members
    persist across feed restarts — so no re-wiring is needed.
  * If the app restarted before this job fired, startup re-wired it.

Passing ``loop=None`` therefore means "keep the existing wiring",
not "drop it".
"""

from __future__ import annotations

import logging

from core import config
from services.telegram_service import telegram_service

logger = logging.getLogger(__name__)


def run_daily_refresh() -> None:
    # Imported here to avoid any import cycle at module load time.
    from routes.admin_routes import _run_full_refresh_sync
    from services.backfill_guard import mark_ran

    logger.info(
        "Daily refresh starting (scheduled %02d:%02d %s)",
        config.DAILY_REFRESH_HOUR,
        config.DAILY_REFRESH_MINUTE,
        config.MARKET_TIMEZONE,
    )
    telegram_service.send_message(
        "Daily Refresh Triggered",
        (
            f"Scheduled refresh at {config.DAILY_REFRESH_HOUR:02d}:"
            f"{config.DAILY_REFRESH_MINUTE:02d} {config.MARKET_TIMEZONE}."
        ),
        level="STARTUP",
    )
    try:
        # loop=None is intentional: the scheduler runs this from a
        # background thread with no asyncio loop. The loop used for
        # WebSocket publishing was wired at startup and persists
        # across feed restarts, so no re-wiring is required here.
        result = _run_full_refresh_sync(reason="daily")
        logger.info("Daily refresh pipeline result: %s", result)

        # The unified routine reports several failures in its result
        # instead of raising. Treat token, provider validation, backup,
        # and feed startup as critical gates; do not write the success
        # marker when any of these stages failed.
        critical_failures = []
        if not result.get("token_refresh_ok"):
            critical_failures.append("mongodb_token_fetch")
        if not result.get("token_valid"):
            critical_failures.append("upstox_token_validation")
        if (result.get("backup") or {}).get("status") != "ok":
            critical_failures.append("backup_refresh")
        if not result.get("feed_restarted"):
            critical_failures.append("live_feed_restart")

        if critical_failures:
            message = (
                "08:00 IST application refresh finished with critical failures.\\n"
                f"Failed stages: {', '.join(critical_failures)}\\n"
                "The daily success marker was NOT written."
            )
            logger.error(
                "[daily-refresh] critical stage(s) failed: %s",
                critical_failures,
            )
            telegram_service.send_message(
                "Daily Application Refresh · FAILED",
                message,
                level="ERROR",
            )
            return

        # Record success only after all critical stages pass, so a later
        # startup can distinguish a complete refresh from a partial run.
        mark_ran("daily_refresh", reason="scheduled", status="ok")
        logger.info(
            "[daily-refresh] completed successfully — token_valid=%s feed_restarted=%s",
            result.get("token_valid"),
            result.get("feed_restarted"),
        )
    except Exception as exc:
        logger.exception("Daily refresh failed")
        telegram_service.send_message(
            "Daily Refresh Failed",
            f"{type(exc).__name__}: {exc}",
            level="ERROR",
        )