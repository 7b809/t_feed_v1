"""Singleton wrapper around the APScheduler BackgroundScheduler.

Jobs registered here:

    * token_lifecycle_check    — interval, token validity monitoring
    * daily_refresh            — cron, Mon–Fri at DAILY_REFRESH_HOUR:MINUTE
    * opening_range_calc       — cron, 09:16:10 IST
    * strategy_selection_calc  — cron, 09:16:30 IST (stage 1)
    * strategy_isolation_calc  — cron, 09:17:00 IST (stage 2)

start() is idempotent.
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

    def start(self) -> BackgroundScheduler:
        from token_tasks.daily_refresh import run_daily_refresh
        from token_tasks.opening_range_task import run_opening_range_calculation
        from token_tasks.strategy_selection_task import run_strategy_selection
        from token_tasks.strategy_isolation_task import run_strategy_isolation
        from token_tasks.token_monitor import check_upstox_token_validity

        if self._scheduler is not None and self._scheduler.running:
            logger.info("Scheduler already running; skipping start.")
            return self._scheduler

        scheduler = BackgroundScheduler(timezone=config.MARKET_TIMEZONE)

        # 1. Token monitor
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

        # 3. Opening Range — 09:16:10 IST
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

        # 4. Strategy stage 1 — 20s after OR so the first candle is final.
        s1_total_sec = (
            config.OPENING_RANGE_HOUR * 3600
            + config.OPENING_RANGE_MINUTE * 60
            + config.OPENING_RANGE_SECOND
            + 20
        )
        s1_hour = (s1_total_sec // 3600) % 24
        s1_min = (s1_total_sec % 3600) // 60
        s1_sec = s1_total_sec % 60

        scheduler.add_job(
            run_strategy_selection,
            "cron",
            day_of_week=config.DAILY_REFRESH_DAYS,
            hour=s1_hour, minute=s1_min, second=s1_sec,
            id="strategy_selection_calc",
            replace_existing=True,
            misfire_grace_time=300,
        )

        # 5. Strategy stage 2 — 50s after OR so Upstox intraday has the
        #    first candle and stage 1 has written its last_day_cross files.
        s2_total_sec = (
            config.OPENING_RANGE_HOUR * 3600
            + config.OPENING_RANGE_MINUTE * 60
            + config.OPENING_RANGE_SECOND
            + 50
        )
        s2_hour = (s2_total_sec // 3600) % 24
        s2_min = (s2_total_sec % 3600) // 60
        s2_sec = s2_total_sec % 60

        scheduler.add_job(
            run_strategy_isolation,
            "cron",
            day_of_week=config.DAILY_REFRESH_DAYS,
            hour=s2_hour, minute=s2_min, second=s2_sec,
            id="strategy_isolation_calc",
            replace_existing=True,
            misfire_grace_time=300,
        )

        scheduler.start()
        self._scheduler = scheduler
        logger.info(
            "Scheduler started (token monitor every %dm, daily refresh=%s, "
            "opening range %02d:%02d:%02d, stage_1 %02d:%02d:%02d, "
            "stage_2 %02d:%02d:%02d)",
            config.UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES,
            config.DAILY_REFRESH_ENABLED,
            config.OPENING_RANGE_HOUR, config.OPENING_RANGE_MINUTE,
            config.OPENING_RANGE_SECOND,
            s1_hour, s1_min, s1_sec,
            s2_hour, s2_min, s2_sec,
        )
        return scheduler

    def shutdown(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler shutdown complete.")
        self._scheduler = None


scheduler_service = SchedulerService()