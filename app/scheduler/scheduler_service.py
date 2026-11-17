# app/scheduler/scheduler_service.py
from __future__ import annotations

import logging
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..config import Settings
from ..services.isolation_store import IsolationStore
from ..services.isolation_subscriber import IsolationSubscriber
from ..services.market_session import is_market_session_open, market_session_status

logger = logging.getLogger(__name__)


class SchedulerService:
    """APScheduler wrapper for SR watchdog and daily MEGA data archive."""

    def __init__(
        self,
        config: Settings,
        subscriber: IsolationSubscriber,
        isolation_store: IsolationStore,
        daily_backup_service=None,
        telegram_service=None,
    ) -> None:
        self._config = config
        self._subscriber = subscriber
        self._isolation_store = isolation_store
        self._daily_backup_service = daily_backup_service
        self._telegram = telegram_service
        self._last_market_open: bool | None = None
        self._scheduler: Optional[AsyncIOScheduler] = None

    def start(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            logger.info("scheduler already running; skipping start")
            return

        self._scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
        self._last_market_open = is_market_session_open(self._config)
        self._scheduler.add_job(
            self._market_session_watchdog, "interval", seconds=15,
            id="market_session_watchdog", max_instances=1, coalesce=True,
            replace_existing=True,
        )
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
        logger.info("scheduler started (sr_watchdog every 30s, market_session_watchdog every 15s)")

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

    async def _market_session_watchdog(self) -> None:
        """Notify once when the configured IST market session opens or closes."""
        try:
            is_open = is_market_session_open(self._config)
            previous = self._last_market_open
            self._last_market_open = is_open
            if previous is None or previous == is_open or self._telegram is None:
                return
            status = market_session_status(self._config)
            if is_open:
                title, message, level = "Market session opened", (
                    "🟢 NSE market session is open. SR, Supertrend and shared Live Feed "
                    "connections are now eligible to connect.\n"
                    f"🕒 Session: {status['session_start']}–{status['session_end']} IST\n"
                    f"📅 Trade date: {status['trade_date_ist']}"
                ), "SUCCESS"
            else:
                title, message, level = "Market session closed", (
                    "🌙 Live WebSocket connections are now paused.\n"
                    "📂 Today's simulated orders and persisted state remain available "
                    "for recovery and review.\n"
                    f"📅 Trade date: {status['trade_date_ist']}"
                ), "INFO"
            await self._telegram.send_message(title, message, level=level)
        except Exception:
            logger.exception("market session Telegram notification failed")

    async def _daily_backup(self) -> None:
        """Backup is isolated; a failure never interrupts the SR watchdog."""
        try:
            if self._daily_backup_service is None:
                return
            await self._daily_backup_service.run_daily_backup(reason="scheduled")
        except Exception:
            logger.exception("daily MEGA backup scheduler job failed; continuing")
