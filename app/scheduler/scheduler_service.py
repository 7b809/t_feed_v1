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
    ) -> None:
        self._config = config
        self._subscriber = subscriber
        self._isolation_store = isolation_store
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