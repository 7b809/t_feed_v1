# services/scheduler_service.py
"""Singleton APScheduler wrapper.

Jobs:
    * token_lifecycle_check         — interval
    * daily_refresh                 — cron, 08:30 IST Mon-Fri
    * first_candle_calc             — cron, 09:16:10 IST Mon-Fri
    * readiness_digest              — cron, 08:45 IST
    * preflight_recheck             — cron, 09:05 IST
    * level_isolation_recheck       — cron, every 5 min, 09:16–15:30 IST Mon-Fri
    * daily_backup                  — cron, 16:00 IST
    * live_sr_refresh_from_api      — interval, every N minutes
    * dummy_order_tracker           — interval, every DUMMY_ORDER_TRACK_INTERVAL_SECONDS
                                      (NEW 2026-10-27: live-tracks dummy orders)
"""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

class SchedulerService:
    def __init__(self) -> None:
        self._scheduler: BackgroundScheduler | None = None

    @property
    def scheduler(self) -> BackgroundScheduler | None:
        return self._scheduler

    def start(self) -> BackgroundScheduler:
        from token_tasks.daily_backup_task import run_daily_backup
        from token_tasks.daily_refresh import run_daily_refresh
        from token_tasks.first_candle_task import run_first_candle_calculation
        from token_tasks.level_isolated_instrument_task import (
            run_level_isolation_recheck,
        )
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

        # -- first candle (09:16:10) ----------------------------------- #
        if getattr(config, "FIRST_CANDLE_ENABLED", True):
            scheduler.add_job(
                run_first_candle_calculation, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=getattr(config, "FIRST_CANDLE_HOUR", 9),
                minute=getattr(config, "FIRST_CANDLE_MINUTE", 16),
                second=getattr(config, "FIRST_CANDLE_SECOND", 10),
                id="first_candle_calc",
                replace_existing=True, misfire_grace_time=60,
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

        # -- Level isolation recheck (every 5 min during market hours) -- #
        if getattr(config, "LEVEL_ISOLATION_ENABLED", True):
            scheduler.add_job(
                run_level_isolation_recheck, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour="9-15",
                minute="*/5",
                id="level_isolation_recheck",
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

        # -- Live SR refresh from Upstox API --------------------------- #
        if getattr(config, "LIVE_SR_REFRESH_FROM_API_ENABLED", True):
            def _refresh_live_sr() -> None:
                try:
                    from services.sr_backfill_service import sr_backfill_service
                    summary = sr_backfill_service.run(
                        reason="periodic_refresh",
                        send_telegram=False,
                    )
                    logger.info(
                        "[scheduler] live_sr_refresh_from_api — "
                        "status=%s instruments=%s seeded=%s errors=%s",
                        summary.get("status"),
                        summary.get("instruments_total"),
                        summary.get("instruments_seeded"),
                        summary.get("errors"),
                    )
                except Exception:
                    logger.exception(
                        "[scheduler] live_sr_refresh_from_api failed"
                    )

            scheduler.add_job(
                _refresh_live_sr, "interval",
                minutes=max(1, int(getattr(
                    config, "LIVE_SR_REFRESH_FROM_API_MINUTES", 2,
                ))),
                id="live_sr_refresh_from_api",
                replace_existing=True, misfire_grace_time=60,
            )

        # -- Dummy order live tracker ----------------------------------- #
        # Every DUMMY_ORDER_TRACK_INTERVAL_SECONDS (default 30 s) the
        # service polls the entry signal, creates any missing dummy
        # orders for armed candidates, updates open orders from live
        # LTP + periodic bar reconcile, and auto-closes at EOD.
        # The service is a no-op outside the DUMMY_ORDER window, so it
        # is safe to schedule all day.
        if getattr(config, "DUMMY_ORDER_ENABLED", True):
            def _dummy_order_tick() -> None:
                try:
                    from services.dummy_order_service import (
                        dummy_order_service,
                    )
                    summary = dummy_order_service.run_once(
                        reason="scheduled",
                    )
                    if summary.get("status") not in {"skipped"}:
                        logger.info(
                            "[scheduler] dummy_order_tracker — "
                            "status=%s created=%s updated=%s closed=%s "
                            "open=%s total=%s",
                            summary.get("status"),
                            summary.get("created"),
                            summary.get("updated"),
                            summary.get("closed"),
                            summary.get("open_count"),
                            summary.get("total_today"),
                        )
                except Exception:
                    logger.exception(
                        "[scheduler] dummy_order_tracker failed"
                    )

            scheduler.add_job(
                _dummy_order_tick, "interval",
                seconds=max(5, int(getattr(
                    config, "DUMMY_ORDER_TRACK_INTERVAL_SECONDS", 30,
                ))),
                id="dummy_order_tracker",
                replace_existing=True, misfire_grace_time=30,
            )

        scheduler.start()
        self._scheduler = scheduler

        logger.info(
            "Scheduler started — daily=%02d:%02d, first_candle=%02d:%02d:%02d, "
            "digest=%02d:%02d, preflight=%02d:%02d, "
            "level_isolation_recheck=*/5 (9-15), DailyBackup=%02d:%02d, "
            "live_sr_refresh=%dm (enabled=%s), "
            "dummy_order_tracker=%ds (enabled=%s)",
            config.DAILY_REFRESH_HOUR, config.DAILY_REFRESH_MINUTE,
            getattr(config, "FIRST_CANDLE_HOUR", 9),
            getattr(config, "FIRST_CANDLE_MINUTE", 16),
            getattr(config, "FIRST_CANDLE_SECOND", 10),
            config.READINESS_DIGEST_HOUR, config.READINESS_DIGEST_MINUTE,
            config.READINESS_PREFLIGHT_HOUR, config.READINESS_PREFLIGHT_MINUTE,
            config.DAILY_BACKUP_HOUR, config.DAILY_BACKUP_MINUTE,
            int(getattr(config, "LIVE_SR_REFRESH_FROM_API_MINUTES", 2)),
            bool(getattr(config, "LIVE_SR_REFRESH_FROM_API_ENABLED", True)),
            int(getattr(config, "DUMMY_ORDER_TRACK_INTERVAL_SECONDS", 30)),
            bool(getattr(config, "DUMMY_ORDER_ENABLED", True)),
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
        logger.info(
            "Scheduler service is now stopped; a subsequent start() "
            "will create a new BackgroundScheduler."
        )

scheduler_service = SchedulerService()