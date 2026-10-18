"""Singleton APScheduler wrapper.

Jobs (Strategy 1):
    * token_lifecycle_check         — interval
    * daily_refresh                 — cron, Mon–Fri
    * opening_range_calc            — cron, 09:16:10 IST
    * strategy_selection_calc       — cron, 09:16:30 IST (Stage 1)
    * strategy_isolation_calc       — cron, 09:17:00 IST (Stage 2)
    * strategy_order_calc           — cron, 09:18:00 IST (Stage 3+4)

Jobs (Strategy 2):
    * strategy_2_stage3_isolation   — cron, 09:18:00 IST (initial pass)
    * strategy_2_stage4_order       — cron, 15:25:00 IST (final catch-up)
    * strategy_2_stage3_recheck     — interval, every 5 min (09:15–15:30)

Jobs (Daily backup):
    * daily_backup                  — cron, 16:00 IST every day (default)
        Zips the folders listed in DAILY_BACKUP_FOLDERS and saves the
        archive inline to MongoDB when it fits under
        DAILY_BACKUP_MAX_BLOB_MB, otherwise saves metadata-only with a
        clear "skipped_size_limit" reason. Gated by DAILY_BACKUP_ENABLED.
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
        from token_tasks.daily_backup_task import run_daily_backup
        from token_tasks.daily_refresh import run_daily_refresh
        from token_tasks.opening_range_task import run_opening_range_calculation
        from token_tasks.strategy_order_task import run_strategy_order
        from token_tasks.strategy_selection_task import run_strategy_selection
        from token_tasks.strategy_isolation_task import run_strategy_isolation
        from token_tasks.strategy_2_isolation_task import run_strategy_2_isolation
        from token_tasks.strategy_2_order_task import run_strategy_2_order
        from token_tasks.token_monitor import check_upstox_token_validity

        if self._scheduler is not None and self._scheduler.running:
            logger.info("Scheduler already running; skipping start.")
            return self._scheduler

        scheduler = BackgroundScheduler(timezone=config.MARKET_TIMEZONE)

        # -- token / daily / OR -----------------------------------------
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

        # -- Strategy 1 --------------------------------------------------
        base_sec = (
            config.OPENING_RANGE_HOUR * 3600
            + config.OPENING_RANGE_MINUTE * 60
            + config.OPENING_RANGE_SECOND
        )

        def _cron_for(offset_sec: int) -> tuple[int, int, int]:
            total = (base_sec + offset_sec) % 86400
            return total // 3600, (total % 3600) // 60, total % 60

        s1_h, s1_m, s1_s = _cron_for(20)
        s2_h, s2_m, s2_s = _cron_for(50)

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

        # -- Strategy 2 --------------------------------------------------
        if config.STRATEGY_2_ENABLED:
            # Initial Stage 3 pass right after Strategy 1 Stage 4 (09:18).
            scheduler.add_job(
                run_strategy_2_isolation, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=config.STRATEGY_ORDER_HOUR,
                minute=config.STRATEGY_ORDER_MINUTE,
                second=15,
                id="strategy_2_stage3_isolation",
                replace_existing=True, misfire_grace_time=300,
            )

            # Periodic Stage 3 re-check every 5 min during the trading day.
            scheduler.add_job(
                run_strategy_2_isolation, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour="9-15",
                minute="*/5",
                id="strategy_2_stage3_recheck",
                replace_existing=True, misfire_grace_time=120,
            )

            # Final Stage 4 pass 5 min before cash-market close.
            scheduler.add_job(
                run_strategy_2_order, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=15, minute=25, second=0,
                id="strategy_2_stage4_order",
                replace_existing=True, misfire_grace_time=600,
            )

        # -- Daily backup (zip folders -> MongoDB) ----------------------- #
        # Every day at DAILY_BACKUP_HOUR:DAILY_BACKUP_MINUTE (default
        # 16:00 IST). The task zips DAILY_BACKUP_FOLDERS and, when the
        # archive fits under DAILY_BACKUP_MAX_BLOB_MB, saves it inline
        # to <DAILY_BACKUP_MONGO_DB>.backup_collection. Otherwise it
        # saves metadata-only with status = "skipped_size_limit".
        if config.DAILY_BACKUP_ENABLED:
            scheduler.add_job(
                run_daily_backup, "cron",
                day_of_week=config.DAILY_BACKUP_DAYS,
                hour=config.DAILY_BACKUP_HOUR,
                minute=config.DAILY_BACKUP_MINUTE,
                id="daily_backup",
                replace_existing=True, misfire_grace_time=1800,
            )

        scheduler.start()
        self._scheduler = scheduler

        daily_backup_line = (
            f"{config.DAILY_BACKUP_HOUR:02d}:"
            f"{config.DAILY_BACKUP_MINUTE:02d}"
            if config.DAILY_BACKUP_ENABLED
            else "disabled"
        )
        logger.info(
            "Scheduler started — OR=%02d:%02d:%02d, S1.stage1=%02d:%02d:%02d, "
            "S1.stage2=%02d:%02d:%02d, S1.stage4=%02d:%02d:00, "
            "S2.enabled=%s, DailyBackup=%s",
            config.OPENING_RANGE_HOUR, config.OPENING_RANGE_MINUTE,
            config.OPENING_RANGE_SECOND,
            s1_h, s1_m, s1_s, s2_h, s2_m, s2_s,
            config.STRATEGY_ORDER_HOUR, config.STRATEGY_ORDER_MINUTE,
            config.STRATEGY_2_ENABLED,
            daily_backup_line,
        )
        return scheduler

    def shutdown(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler shutdown complete.")
        self._scheduler = None


scheduler_service = SchedulerService()