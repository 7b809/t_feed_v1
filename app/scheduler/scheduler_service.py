# app\scheduler\scheduler_service.py
#
# Schedules:
#   * Job 1 (existing)  : Mon-Fri 09:00 IST -> MEGA archive refresh
#   * Job 2 (new)       : Mon-Fri 09:16:10 IST -> EMA live loop start

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import Settings
from app.services.mega_archive_service import MegaArchiveService, RefreshConflict

logger = logging.getLogger(__name__)


class SchedulerService:
    def __init__(
        self,
        config: Settings,
        job_service: MegaArchiveService,
        ema_service=None,
    ):
        self.config = config
        self.job_service = job_service
        self.ema_service = ema_service
        self.scheduler: BackgroundScheduler | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ #
    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called from FastAPI lifespan so we can post async work from APScheduler."""
        self._loop = loop

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if not self.config.scheduler_enabled or self.scheduler is not None:
            return
        days = self.config.schedule_weekdays
        self.scheduler = BackgroundScheduler(timezone=self.config.schedule_timezone)

        # Job 1 — MEGA archive refresh
        self.scheduler.add_job(
            self._run,
            trigger=CronTrigger(
                day_of_week=days,
                hour=self.config.schedule_hour,
                minute=self.config.schedule_minute,
                timezone=self.config.schedule_timezone,
            ),
            id="mega-archive-weekday-refresh",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )

        # Job 2 — EMA live loop start at 09:16:10
        if self.ema_service is not None:
            self.scheduler.add_job(
                self._start_ema_live,
                trigger=CronTrigger(
                    day_of_week=days,
                    hour=9,
                    minute=16,
                    second=10,
                    timezone=self.config.schedule_timezone,
                ),
                id="ema-live-start",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )

        self.scheduler.start()
        logger.info(
            "Scheduler started weekdays=%s time=%02d:%02d timezone=%s",
            days, self.config.schedule_hour, self.config.schedule_minute,
            self.config.schedule_timezone,
        )

    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        try:
            self.job_service.refresh("scheduled")
        except RefreshConflict:
            logger.warning("Scheduled refresh skipped because another refresh is running")
        except Exception:
            logger.exception("Scheduled refresh failed")

    def _start_ema_live(self) -> None:
        if self.ema_service is None or self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.ema_service.start_live(), self._loop)
            logger.info("Scheduled EMA live start triggered at 09:16:10")
        except Exception:
            logger.exception("Failed to start EMA live loop from scheduler")

    # ------------------------------------------------------------------ #
    def shutdown(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None