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
    """APScheduler wrapper for SR watchdog and daily MEGA data archive."""

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
        if self._scheduler is not None and self._scheduler.running:
            logger.info("scheduler already running; skipping start")
            return

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
                    day_of_week=self._config.daily_data_backup_days,
                    hour=self._config.daily_data_backup_hour,
                    minute=self._config.daily_data_backup_minute,
                    timezone="Asia/Kolkata",
                    id="daily_orderflow_mega_backup",
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=3600,
                    replace_existing=True,
                )
                logger.info(
                    "daily MEGA backup scheduled for %02d:%02d IST (%s)",
                    self._config.daily_data_backup_hour,
                    self._config.daily_data_backup_minute,
                    self._config.daily_data_backup_days,
                )
            except Exception:
                logger.exception("could not register daily MEGA backup; other jobs continue")

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
        """Backup is isolated; a failure never interrupts the SR watchdog."""
        try:
            if self._daily_backup_service is None:
                return
            await self._daily_backup_service.run_daily_backup(reason="scheduled")
        except Exception:
            logger.exception("daily MEGA backup scheduler job failed; continuing")
