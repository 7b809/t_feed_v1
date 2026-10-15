"""Singleton wrapper around the APScheduler BackgroundScheduler.

Used by project startup, daily refresh, and hard refresh so all three
trigger points can call ``start()`` without creating duplicate
schedulers.

Jobs registered here:

    * token_lifecycle_check  — interval, token validity monitoring
    * daily_refresh          — cron, Mon–Fri at DAILY_REFRESH_HOUR:MINUTE
    * opening_range_calc     — cron, Mon–Fri at 09:16:10 IST (configurable)

    start()     -> idempotent; creates + starts the scheduler once.
    shutdown()  -> stops the scheduler if running.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from core import config

logger = logging.getLogger(__name__)


class SchedulerService:
    def __init__(self) -> None:
        self._scheduler: BackgroundScheduler | None = None

    @property
    def scheduler(self) -> BackgroundScheduler | None:
        return self._scheduler

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    def start(self) -> BackgroundScheduler:
        """Start the scheduler. Idempotent — safe to call repeatedly."""
        # Imports live inside so we never form an import cycle with
        # admin_routes / daily_refresh / opening_range_task at module
        # import time.
        from token_tasks.daily_refresh import run_daily_refresh
        from token_tasks.opening_range_task import run_opening_range_calculation
        from token_tasks.token_monitor import check_upstox_token_validity

        if self._scheduler is not None and self._scheduler.running:
            logger.info("Scheduler already running; skipping start.")
            return self._scheduler

        scheduler = BackgroundScheduler(timezone=config.MARKET_TIMEZONE)

        # 1. Token validity monitoring
        scheduler.add_job(
            check_upstox_token_validity,
            "interval",
            minutes=config.UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES,
            id="token_lifecycle_check",
            replace_existing=True,
            kwargs={"send_success_message": False},
        )

        # 2. Daily refresh
        if config.DAILY_REFRESH_ENABLED:
            scheduler.add_job(
                run_daily_refresh,
                "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=config.DAILY_REFRESH_HOUR,
                minute=config.DAILY_REFRESH_MINUTE,
                id="daily_refresh",
                replace_existing=True,
                misfire_grace_time=300,
            )

        # 3. Opening Range calculation — fires every weekday at the
        #    configured trigger time (default 09:16:10 IST).
        scheduler.add_job(
            run_opening_range_calculation,
            "cron",
            day_of_week=config.DAILY_REFRESH_DAYS,
            hour=config.OPENING_RANGE_HOUR,
            minute=config.OPENING_RANGE_MINUTE,
            second=config.OPENING_RANGE_SECOND,
            id="opening_range_calc",
            replace_existing=True,
            misfire_grace_time=300,
        )

        scheduler.start()
        self._scheduler = scheduler
        logger.info(
            "Scheduler started (token monitor every %dm, daily refresh=%s, "
            "opening range at %02d:%02d:%02d)",
            config.UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES,
            config.DAILY_REFRESH_ENABLED,
            config.OPENING_RANGE_HOUR,
            config.OPENING_RANGE_MINUTE,
            config.OPENING_RANGE_SECOND,
        )
        return scheduler

    def shutdown(self) -> None:
        """Stop the scheduler if it is running."""
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler shutdown complete.")
        self._scheduler = None


scheduler_service = SchedulerService()