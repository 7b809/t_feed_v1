# File: services/scheduler_service.py
"""Singleton APScheduler wrapper.

Jobs:
    * token_lifecycle_check         — interval
    * daily_refresh                 — cron, 08:30 IST Mon-Fri

Readiness / preflight jobs:
    * readiness_digest              — cron, 08:45 IST
    * preflight_recheck             — cron, 09:05 IST

Daily backup:
    * daily_backup                  — cron, 16:00 IST

Strategy jobs (Opening Range calculation and its watchdog) are
deliberately **not** registered here. They were removed along with
the strategy pipeline — the scheduler now only drives infrastructure,
token lifecycle, readiness, and backup tasks.

Diagnostics
-----------
This module uses ``get_logger(__file__)`` so every line reaches the
file handlers in ``logs/`` and ``logs_text/``. The scheduler logs:

    * a compact one-line summary of every cron it registered, and
    * a post-``shutdown()`` confirmation so a restart in the same
      process shows the "was running" -> "now stopped" transition
      in two adjacent log lines.
"""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


# --------------------------------------------------------------------------- #
# Scheduler service                                                           #
# --------------------------------------------------------------------------- #
class SchedulerService:
    def __init__(self) -> None:
        self._scheduler: BackgroundScheduler | None = None

    @property
    def scheduler(self) -> BackgroundScheduler | None:
        return self._scheduler

    def start(self) -> BackgroundScheduler:
        from token_tasks.daily_backup_task import run_daily_backup
        from token_tasks.daily_refresh import run_daily_refresh
        from token_tasks.readiness_digest_task import (
            run_preflight_recheck,
            run_readiness_digest,
        )
        from token_tasks.token_monitor import check_upstox_token_validity

        if self._scheduler is not None and self._scheduler.running:
            logger.info("Scheduler already running; skipping start.")
            return self._scheduler

        scheduler = BackgroundScheduler(timezone=config.MARKET_TIMEZONE)

        # -- token lifecycle -------------------------------------------- #
        scheduler.add_job(
            check_upstox_token_validity, "interval",
            minutes=config.UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES,
            id="token_lifecycle_check",
            replace_existing=True,
            kwargs={"send_success_message": False},
        )

        # -- daily refresh ---------------------------------------------- #
        if config.DAILY_REFRESH_ENABLED:
            scheduler.add_job(
                run_daily_refresh, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=config.DAILY_REFRESH_HOUR,
                minute=config.DAILY_REFRESH_MINUTE,
                id="daily_refresh",
                replace_existing=True, misfire_grace_time=300,
            )

        # -- Readiness digest + pre-flight ------------------------------ #
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

        # -- Daily backup ----------------------------------------------- #
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

        # ---- Compact summary line ------------------------------------- #
        logger.info(
            "Scheduler started — daily=%02d:%02d, digest=%02d:%02d, "
            "preflight=%02d:%02d, DailyBackup=%02d:%02d",
            config.DAILY_REFRESH_HOUR, config.DAILY_REFRESH_MINUTE,
            config.READINESS_DIGEST_HOUR, config.READINESS_DIGEST_MINUTE,
            config.READINESS_PREFLIGHT_HOUR, config.READINESS_PREFLIGHT_MINUTE,
            config.DAILY_BACKUP_HOUR, config.DAILY_BACKUP_MINUTE,
        )

        return scheduler

    def shutdown(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            try:
                job_count = len(self._scheduler.get_jobs())
            except Exception:
                job_count = -1
            try:
                self._scheduler.shutdown(wait=False)
                logger.info(
                    "Scheduler shutdown complete — %d job(s) stopped.",
                    job_count,
                )
            except Exception:
                logger.exception(
                    "Scheduler shutdown raised; forcing _scheduler = None."
                )
        self._scheduler = None
        # Explicit post-condition log so a restart in the same process
        # shows the "was running" -> "now stopped" transition in the
        # log file.
        logger.info(
            "Scheduler service is now stopped; a subsequent start() "
            "will create a new BackgroundScheduler."
        )


scheduler_service = SchedulerService()