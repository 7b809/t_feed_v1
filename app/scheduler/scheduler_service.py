# app/scheduler/scheduler_service.py
from __future__ import annotations

import logging
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..config import Settings
from ..services.isolation_store import IsolationStore
from ..services.isolation_subscriber import IsolationSubscriber

logger = logging.getLogger(__name__)


class SchedulerService:
    """APScheduler wrapper — one job: sr_watchdog every 30 s."""

    def __init__(
        self,
        config: Settings,
        subscriber: IsolationSubscriber,
        isolation_store: IsolationStore,
        daily_backup_service=None,
    ) -> None:
        self._config = config
        self._subscriber = subscriber
        self._isolation_store = isolation_store
        self._daily_backup_service = daily_backup_service
        self._scheduler: Optional[AsyncIOScheduler] = None

    def start(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
        self._scheduler.add_job(
            self._sr_watchdog,
            "interval",
            seconds=30,
            id="sr_watchdog",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        if self._config.daily_data_backup_enabled and self._daily_backup_service is not None:
            try:
                self._scheduler.add_job(
                    self._daily_backup,
                    "cron",
                    hour=16,
                    minute=0,
                    timezone="Asia/Kolkata",
                    id="daily_project_data_backup",
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=3600,
                    replace_existing=True,
                )
                logger.info("daily project backup scheduled for 16:00 IST")
            except Exception:
                logger.exception("could not register daily project backup; other jobs continue")
        self._scheduler.start()
        logger.info("scheduler started (sr_watchdog every 30s)")

    def shutdown(self) -> None:
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                logger.exception("scheduler shutdown error")
            self._scheduler = None
            logger.info("scheduler stopped")

    async def _sr_watchdog(self) -> None:
        try:
            await self._subscriber.restart_dead_clients()
        except Exception:
            logger.exception("sr_watchdog error")
    async def _daily_backup(self) -> None:
        """Isolated scheduled task: backup errors must never affect watchdog."""
        try:
            if self._daily_backup_service is None:
                return
            await self._daily_backup_service.run_daily_backup()
        except Exception:
            logger.exception("daily project backup scheduler job failed; continuing")
