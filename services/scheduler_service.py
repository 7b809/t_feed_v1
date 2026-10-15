"""Singleton APScheduler wrapper.

Jobs:
    * token_lifecycle_check         — interval
    * daily_refresh                 — cron, Mon–Fri
    * opening_range_calc            — cron, 09:16:10 IST
    * strategy_selection_calc       — cron, 09:16:30 IST (stage 1)
    * strategy_isolation_calc       — cron, 09:17:00 IST (stage 2)
    * strategy_order_calc           — cron, 09:18:00 IST (stage 3+4)
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
        from token_tasks.strategy_order_task import run_strategy_order
        from token_tasks.strategy_selection_task import run_strategy_selection
        from token_tasks.strategy_isolation_task import run_strategy_isolation
        from token_tasks.token_monitor import check_upstox_token_validity

        if self._scheduler is not None and self._scheduler.running:
            logger.info("Scheduler already running; skipping start.")
            return self._scheduler

        scheduler = BackgroundScheduler(timezone=config.MARKET_TIMEZONE)

        scheduler.add_job(
            check_upstox_token_validity, "interval",
            minutes=config.UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES,
            id="token_lifecycle_check",
            replace_existing=True,
            kwargs={"send_success_message": False},
        )

        if config.DAILY_REFRESH_ENABLED:
            scheduler.add_job(
                run_daily_refresh, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=config.DAILY_REFRESH_HOUR,
                minute=config.DAILY_REFRESH_MINUTE,
                id="daily_refresh",
                replace_existing=True, misfire_grace_time=300,
            )

        scheduler.add_job(
            run_opening_range_calculation, "cron",
            day_of_week=config.DAILY_REFRESH_DAYS,
            hour=config.OPENING_RANGE_HOUR,
            minute=config.OPENING_RANGE_MINUTE,
            second=config.OPENING_RANGE_SECOND,
            id="opening_range_calc",
            replace_existing=True, misfire_grace_time=300,
        )

        base_sec = (
            config.OPENING_RANGE_HOUR * 3600
            + config.OPENING_RANGE_MINUTE * 60
            + config.OPENING_RANGE_SECOND
        )

        def _cron_for(offset_sec: int) -> tuple[int, int, int]:
            total = (base_sec + offset_sec) % 86400
            return total // 3600, (total % 3600) // 60, total % 60

        s1_h, s1_m, s1_s = _cron_for(20)   # stage 1 at 09:16:30
        s2_h, s2_m, s2_s = _cron_for(50)   # stage 2 at 09:17:00

        scheduler.add_job(
            run_strategy_selection, "cron",
            day_of_week=config.DAILY_REFRESH_DAYS,
            hour=s1_h, minute=s1_m, second=s1_s,
            id="strategy_selection_calc",
            replace_existing=True, misfire_grace_time=300,
        )
        scheduler.add_job(
            run_strategy_isolation, "cron",
            day_of_week=config.DAILY_REFRESH_DAYS,
            hour=s2_h, minute=s2_m, second=s2_s,
            id="strategy_isolation_calc",
            replace_existing=True, misfire_grace_time=300,
        )
        scheduler.add_job(
            run_strategy_order, "cron",
            day_of_week=config.DAILY_REFRESH_DAYS,
            hour=config.STRATEGY_ORDER_HOUR,
            minute=config.STRATEGY_ORDER_MINUTE,
            second=0,
            id="strategy_order_calc",
            replace_existing=True, misfire_grace_time=300,
        )

        scheduler.start()
        self._scheduler = scheduler
        logger.info(
            "Scheduler started — opening_range=%02d:%02d:%02d, "
            "stage_1=%02d:%02d:%02d, stage_2=%02d:%02d:%02d, "
            "stage_4=%02d:%02d:00",
            config.OPENING_RANGE_HOUR, config.OPENING_RANGE_MINUTE,
            config.OPENING_RANGE_SECOND,
            s1_h, s1_m, s1_s, s2_h, s2_m, s2_s,
            config.STRATEGY_ORDER_HOUR, config.STRATEGY_ORDER_MINUTE,
        )
        return scheduler

    def shutdown(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler shutdown complete.")
        self._scheduler = None


scheduler_service = SchedulerService()