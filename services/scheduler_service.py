# services/scheduler_service.py
"""Singleton APScheduler wrapper.

All S/R data is supplied by the external EMA V2 WebSocket service.
This scheduler deliberately contains no local pivot calculations,
Upstox S/R refresh job, or replay-series S/R seed job.

The dummy-order tracker waits for a fresh external EMA V2 15/3 snapshot
for the currently isolated instrument. Safety-critical sandbox EOD
exits are independent of S/R readiness.
"""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


def sr_backfill_ready() -> tuple[bool, str]:
    """Return whether external EMA V2 S/R is ready for active isolation."""
    try:
        from services.level_isolated_instrument_service import level_isolated_instrument_service
        from services.external_sr_websocket_service import external_sr_websocket_service

        detail = level_isolated_instrument_service.get_detail() or {}
        isolated = detail.get("isolated") or {}
        key = (
            str(isolated.get("instrument_key") or "").strip()
            if isinstance(isolated, dict)
            else ""
        )
        if not key:
            return True, "no_active_isolation"
        if not external_sr_websocket_service.is_ready(key, "15/3"):
            return False, "external_sr_15_3_not_ready"
        return True, "external_sr_ready"
    except Exception as exc:
        logger.exception("[scheduler] external SR readiness probe failed")
        return False, f"external_sr_probe_error:{type(exc).__name__}"


class SchedulerService:
    def __init__(self) -> None:
        self._scheduler: BackgroundScheduler | None = None

    @property
    def scheduler(self) -> BackgroundScheduler | None:
        return self._scheduler

    # ------------------------------------------------------------------ #
    # Compatibility accessor for any existing external scheduler ticks. #
    # The readiness result now reflects EMA V2 WebSocket S/R, not a      #
    # local backfill or replay-series latch.                              #
    # ------------------------------------------------------------------ #
    def is_sr_backfill_ready(self) -> tuple[bool, str]:
        """Return the external EMA V2 S/R readiness state.

        The legacy method name is retained for compatibility with
        existing callers.
        """
        return sr_backfill_ready()

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
                minute=getattr(config, "FIRST_CANDLE_MINUTE", 17),
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

        # -- Dummy order live tracker ----------------------------------- #
        # Every DUMMY_ORDER_TRACK_INTERVAL_SECONDS (default 30 s) the
        # service polls the entry signal, creates any missing dummy
        # orders for armed candidates, updates open orders from live
        # LTP + periodic bar reconcile, and auto-closes at EOD.
        #
        # The tick short-circuits until the external EMA V2 15/3
        # WebSocket has delivered a fresh snapshot for the active
        # isolated instrument. main.py uses the same readiness check
        # before its one-time startup catch-up.
        #
        # The service is a no-op outside the DUMMY_ORDER window, so it
        # is safe to schedule all day.
        if getattr(config, "DUMMY_ORDER_ENABLED", True):
            def _dummy_order_tick() -> None:
                # -------- latch gate (MUST be first) ------------------ #
                ready, reason = sr_backfill_ready()
                if not ready:
                    logger.info(
                        "[scheduler] dummy_order_tracker skipped — %s",
                        reason,
                    )
                    return

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

        # -- Sandbox EOD exit (Bug B fix, 2026-11-04) ------------------- #
        # The sandbox executor owns its own end-of-day exit at
        # SANDBOX_EOD_HOUR:SANDBOX_EOD_MINUTE (default 15:15 IST).
        #
        # Why this is a SEPARATE job from the dummy_order_tracker:
        #   * The dummy order engine's accounting EOD_CLOSE runs at
        #     DUMMY_ORDER_EOD_HOUR:DUMMY_ORDER_EOD_MINUTE (default
        #     15:35). That is TOO LATE for a real exit: NSE intraday
        #     F&O positions are broker-auto-squared-off around
        #     15:15–15:25, so by 15:35 the sandbox would already be
        #     flat and the executor's exit_all call would be a no-op
        #     or error.
        #   * Therefore the sandbox must own its exit at 15:15 — before
        #     both the broker's cut-off and the dummy's accounting tick.
        #   * When the dummy EOD_CLOSE transition fires at 15:35, the
        #     executor sees the position is already EXITED and logs
        #     the divergence as expected (see
        #     sandbox_execution_service._handle_eod_close).
        #
        # Why no sr_backfill_ready() gate here:
        #   * Safety first. An un-exitable position is strictly worse
        #     than an exit taken against a partially-built SR series.
        #     Blocking this job on the readiness latch would risk
        #     leaving the sandbox long into the broker's auto-square-off
        #     window. force_eod_exit() itself is idempotent and a no-op
        #     when no position is open, so an accidental early fire
        #     (e.g. operator restarts at 15:14) is harmless.
        #
        # Gating on SANDBOX_EXECUTION_ENABLED:
        #   * Default is false. On boxes that have not opted into live
        #     sandbox execution, no job is registered and the scheduler
        #     logs remain unchanged from pre-2026-11-04.
        if getattr(config, "SANDBOX_EXECUTION_ENABLED", False):
            _sandbox_eod_hour = int(getattr(config, "SANDBOX_EOD_HOUR", 15))
            _sandbox_eod_minute = int(getattr(config, "SANDBOX_EOD_MINUTE", 15))

            def _sandbox_eod_tick() -> None:
                try:
                    from services.sandbox_execution_service import (
                        sandbox_execution_service,
                    )
                    result = sandbox_execution_service.force_eod_exit()
                    logger.info(
                        "[scheduler] sandbox_eod_exit — status=%s open=%s",
                        (result or {}).get("status"),
                        (result or {}).get("open"),
                    )
                except Exception:
                    logger.exception(
                        "[scheduler] sandbox_eod_exit failed; the "
                        "executor's exit-all retries and the later "
                        "dummy 15:35 EOD_CLOSE no-op path will be "
                        "the fallback exit"
                    )

            scheduler.add_job(
                _sandbox_eod_tick, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=_sandbox_eod_hour,
                minute=_sandbox_eod_minute,
                id="sandbox_eod_exit",
                replace_existing=True,
                # Generous grace window: we would rather fire late
                # (still inside the broker auto-square-off window)
                # than not at all.
                misfire_grace_time=600,
            )

        # ------------------------------------------------------------------ #
        # DEFENSIVE CONTRACT — any additional scheduler job that invokes   #
        # dummy_order_service MUST call sr_backfill_ready() first. This    #
        # checks external EMA V2 15/3 freshness for the active instrument. #
        # The sandbox_eod_exit is intentionally exempt: it is a safety-    #
        # critical position exit and must run independently of S/R state.  #
        # ------------------------------------------------------------------ #

        scheduler.start()
        self._scheduler = scheduler

        logger.info(
            "Scheduler started — jobs=%s, dummy_order_tracker=%s "
            "(interval=%ss, gate=external_ema_v2_15/3), "
            "sandbox_eod_exit=%s (%02d:%02d)",
            [job.id for job in scheduler.get_jobs()],
            bool(getattr(config, "DUMMY_ORDER_ENABLED", True)),
            int(getattr(config, "DUMMY_ORDER_TRACK_INTERVAL_SECONDS", 30)),
            bool(getattr(config, "SANDBOX_EXECUTION_ENABLED", False)),
            int(getattr(config, "SANDBOX_EOD_HOUR", 15)),
            int(getattr(config, "SANDBOX_EOD_MINUTE", 15)),
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