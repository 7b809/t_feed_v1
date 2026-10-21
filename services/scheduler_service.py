# services/scheduler_service.py
"""Singleton APScheduler wrapper.

Jobs:
    * token_lifecycle_check         — interval
    * daily_refresh                 — cron, 08:30 IST Mon-Fri
    * opening_range_calc            — cron, 09:16:10 IST
    * strategy_selection_calc       — cron, 09:16:30 IST (Stage 1)
    * strategy_isolation_calc       — cron, 09:17:00 IST (Stage 2)
    * strategy_tracker_catchup      — cron, 09:17:30 IST (Stage-3 catch-up)
    * strategy_order_calc           — cron, 09:18:00 IST (Stage 3+4)

Readiness / watchdog jobs (NEW):
    * readiness_digest              — cron, 08:45 IST
    * preflight_recheck             — cron, 09:05 IST
    * or_watchdog                   — cron, 09:17:10 IST
    * stage1_watchdog               — cron, 09:17:30 IST

Strategy 2 jobs:
    * strategy_2_stage3_isolation   — cron, 09:18:00 IST
    * strategy_2_stage4_order       — cron, 15:25:00 IST
    * strategy_2_stage3_recheck     — cron, every 5 min 09-15

Note on ``strategy_tracker_catchup``
-----------------------------------
This job runs 30 seconds before the Stage-4 order cron (default
09:17:30) and calls ``strategy_live_tracker_service.run_catchup()`` so
the S1 tracker:

    1. reloads the Stage-2 contracts that the isolation cron just wrote
       to disk, and
    2. re-scans the 09:15–09:20 window for per-contract R2 touches.

It is a belt-and-braces guard on top of the post-Stage-2 hook inside
``token_tasks/strategy_isolation_task.py``. It protects against:

    * an unusually slow Stage 2 that finishes past its scheduled cron
      time, or
    * any future refactor that breaks the post-Stage-2 hook.

The job is harmless when the post-Stage-2 hook already ran: the
tracker reloads from disk and the per-contract scan is idempotent
(already-activated contracts keep their existing record).
"""

from __future__ import annotations

import logging
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from core import config

logger = logging.getLogger(__name__)


def _stage2_tree_has_contracts() -> bool:
    try:
        root = Path(config.STRATEGY_BASE_DIR) / config.STRATEGY_STAGE_2_NAME
        if not root.exists() or not root.is_dir():
            return False
        for ticker_dir in root.iterdir():
            if not ticker_dir.is_dir():
                continue
            for index_dir in ticker_dir.iterdir():
                if not index_dir.is_dir():
                    continue
                for strike_dir in index_dir.iterdir():
                    if strike_dir.is_dir():
                        return True
        return False
    except Exception:
        logger.exception(
            "Strategy 2 recheck: stage-2 tree scan failed; treating as empty"
        )
        return False


def _strategy2_recheck() -> None:
    if not _stage2_tree_has_contracts():
        logger.info(
            "Strategy 2 recheck: stage-2 tree is empty — skipping "
            "(no contracts to watch)"
        )
        return
    from token_tasks.strategy_2_isolation_task import run_strategy_2_isolation
    run_strategy_2_isolation()


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
        from token_tasks.readiness_digest_task import (
            run_preflight_recheck,
            run_readiness_digest,
        )
        from token_tasks.strategy_order_task import run_strategy_order
        from token_tasks.strategy_selection_task import run_strategy_selection
        from token_tasks.strategy_isolation_task import run_strategy_isolation
        from token_tasks.strategy_tracker_catchup_task import (
            run_strategy_tracker_catchup,
        )
        from token_tasks.strategy_2_isolation_task import run_strategy_2_isolation
        from token_tasks.strategy_2_order_task import run_strategy_2_order
        from token_tasks.token_monitor import check_upstox_token_validity
        from token_tasks.watchdog_task import (
            run_or_watchdog,
            run_stage1_watchdog,
        )

        if self._scheduler is not None and self._scheduler.running:
            logger.info("Scheduler already running; skipping start.")
            return self._scheduler

        scheduler = BackgroundScheduler(timezone=config.MARKET_TIMEZONE)

        # -- token / daily / OR ----------------------------------------- #
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

        # -- Readiness digest + pre-flight (NEW) ------------------------ #
        if config.READINESS_DIGEST_ENABLED:
            scheduler.add_job(
                run_readiness_digest, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=config.READINESS_DIGEST_HOUR,
                minute=config.READINESS_DIGEST_MINUTE,
                id="readiness_digest",
                replace_existing=True, misfire_grace_time=120,
            )

        if config.READINESS_PREFLIGHT_ENABLED:
            scheduler.add_job(
                run_preflight_recheck, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=config.READINESS_PREFLIGHT_HOUR,
                minute=config.READINESS_PREFLIGHT_MINUTE,
                id="preflight_recheck",
                replace_existing=True, misfire_grace_time=120,
            )

        # -- OR / Stage-1 watchdogs (NEW) ------------------------------- #
        if config.READINESS_WATCHDOG_ENABLED:
            or_sec = config.OPENING_RANGE_SECOND + 60
            or_min = config.OPENING_RANGE_MINUTE + (or_sec // 60)
            or_hr = config.OPENING_RANGE_HOUR + (or_min // 60)
            scheduler.add_job(
                run_or_watchdog, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=or_hr % 24,
                minute=or_min % 60,
                second=or_sec % 60,
                id="or_watchdog",
                replace_existing=True, misfire_grace_time=120,
            )

            s1_sec = config.OPENING_RANGE_SECOND + 80
            s1_min = config.OPENING_RANGE_MINUTE + (s1_sec // 60)
            s1_hr = config.OPENING_RANGE_HOUR + (s1_min // 60)
            scheduler.add_job(
                run_stage1_watchdog, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=s1_hr % 24,
                minute=s1_min % 60,
                second=s1_sec % 60,
                id="stage1_watchdog",
                replace_existing=True, misfire_grace_time=120,
            )

        # -- Strategy 1 ------------------------------------------------- #
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

        # -- Strategy 1 · Stage-3 tracker catch-up (safety net) --------- #
        #
        # Runs 30 s before the Stage-4 order cron. Purpose: give the
        # tracker one more chance to reload the freshly-written Stage-2
        # tree and re-scan the 09:15–09:20 window before Stage 4 reads
        # ``stage_3_summary.json``.
        #
        # Belt-and-braces on top of the post-Stage-2 hook in
        # ``token_tasks/strategy_isolation_task.py`` — protects against
        # an unusually slow Stage 2 or a future refactor that breaks the
        # post-Stage-2 hook.
        catchup_h = catchup_m = catchup_s = 0
        if config.STRATEGY_TRACKER_ENABLED:
            catchup_sec = (
                config.STRATEGY_ORDER_HOUR * 3600
                + config.STRATEGY_ORDER_MINUTE * 60
                - 30
            ) % 86400
            catchup_h = catchup_sec // 3600
            catchup_m = (catchup_sec % 3600) // 60
            catchup_s = catchup_sec % 60
            scheduler.add_job(
                run_strategy_tracker_catchup, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=catchup_h,
                minute=catchup_m,
                second=catchup_s,
                id="strategy_tracker_catchup",
                replace_existing=True, misfire_grace_time=60,
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

        # -- Strategy 2 -------------------------------------------------- #
        if config.STRATEGY_2_ENABLED:
            scheduler.add_job(
                run_strategy_2_isolation, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=config.STRATEGY_ORDER_HOUR,
                minute=config.STRATEGY_ORDER_MINUTE,
                second=15,
                id="strategy_2_stage3_isolation",
                replace_existing=True, misfire_grace_time=300,
            )
            scheduler.add_job(
                _strategy2_recheck, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour="9-15",
                minute="*/5",
                id="strategy_2_stage3_recheck",
                replace_existing=True, misfire_grace_time=120,
            )
            scheduler.add_job(
                run_strategy_2_order, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=15, minute=25, second=0,
                id="strategy_2_stage4_order",
                replace_existing=True, misfire_grace_time=600,
            )

        # -- Daily backup ------------------------------------------------ #
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

        logger.info(
            "Scheduler started — daily=%02d:%02d, digest=%02d:%02d, "
            "preflight=%02d:%02d, OR=%02d:%02d:%02d, S1.stage1=%02d:%02d:%02d, "
            "S1.stage2=%02d:%02d:%02d, S1.stage3_catchup=%02d:%02d:%02d, "
            "S1.stage4=%02d:%02d:00, S2.enabled=%s, DailyBackup=%02d:%02d",
            config.DAILY_REFRESH_HOUR, config.DAILY_REFRESH_MINUTE,
            config.READINESS_DIGEST_HOUR, config.READINESS_DIGEST_MINUTE,
            config.READINESS_PREFLIGHT_HOUR, config.READINESS_PREFLIGHT_MINUTE,
            config.OPENING_RANGE_HOUR, config.OPENING_RANGE_MINUTE,
            config.OPENING_RANGE_SECOND,
            s1_h, s1_m, s1_s, s2_h, s2_m, s2_s,
            catchup_h, catchup_m, catchup_s,
            config.STRATEGY_ORDER_HOUR, config.STRATEGY_ORDER_MINUTE,
            config.STRATEGY_2_ENABLED,
            config.DAILY_BACKUP_HOUR, config.DAILY_BACKUP_MINUTE,
        )
        return scheduler

    def shutdown(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler shutdown complete.")
        self._scheduler = None


scheduler_service = SchedulerService()