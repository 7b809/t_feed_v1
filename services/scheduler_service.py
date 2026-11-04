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
    * live_sr_refresh_from_api      — interval, every N minutes (live-buffer only)
    * replay_seed_at_open           — cron, 09:16 IST Mon-Fri
                                      (NEW 2026-11-04: deferred first-seed for
                                       pre-window startups; idempotent)
    * dummy_order_tracker           — interval, every DUMMY_ORDER_TRACK_INTERVAL_SECONDS
                                      (live-tracks dummy orders; short-circuits
                                       until SR backfill is ready)

Deterministic-replay contract (2026-11-04)
------------------------------------------
Three invariants keep the post-isolation execution pipeline reproducible:

1. ``_dummy_order_tick`` (and **any** other tick — present or future —
   that invokes ``dummy_order_service``, including a potential
   ``_strategy2_recheck``) MUST consult ``sr_backfill_ready()`` before
   calling into the dummy-order service. This is the same latch
   ``main.py`` uses at startup. Firing before the SR backfill is ready
   creates orders against a partially-built series — the
   14:16-on-2026-10-29 class of bug.

2. ``_refresh_live_sr`` (the every-2-minute API refresh) MUST NOT
   mutate the replay series used by the startup catch-up. It updates
   only the live buffer (``update_replay=False``). The replay series
   is built once per day and is then frozen; ``DUMMY_ORDER_DRIFT_GUARD``
   then guarantees that any existing order's entry/stop/target is
   never recomputed from a later snapshot.

3. ``_seed_replay_series_at_open`` is the ONLY deferred path allowed
   to write the replay series (``update_replay=True``). It exists to
   cover the case where the process was up *before* 09:15 — feed-start
   backfill correctly skips (outside window) but nothing else would
   flip ``is_ready_for_today()`` for the rest of the day. The job is
   idempotent: it returns immediately when the replay is already
   seeded. Without it, a pre-09:15 startup silently disables
   ``dummy_order_tracker`` until the next restart.

See also:
    * ``services/sr_backfill_service.py::is_ready_for_today``  (the latch)
    * ``services/dummy_order_service.py``                      (consumer)
    * ``core/config.py::DUMMY_ORDER_*``                        (flags)
"""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

# --------------------------------------------------------------------------- #
# DEFENSIVE CONTRACT — the single SR-backfill readiness latch                 #
# --------------------------------------------------------------------------- #
# Any tick that directly OR indirectly invokes ``dummy_order_service``       #
# MUST call ``sr_backfill_ready()`` first and bail on ``False``. This        #
# includes the in-file ``_dummy_order_tick`` below, and any out-of-file     #
# tick (e.g. a future ``_strategy2_recheck``) that invokes the service.     #
# The helper is intentionally module-level so external ticks can import    #
# it without going through ``scheduler_service``.                           #
# --------------------------------------------------------------------------- #
def sr_backfill_ready() -> tuple[bool, str]:
    """Return ``(ready, reason)`` for today's SR backfill latch.

    This is the **same latch** used by ``main.py`` at startup. If it
    returns ``False`` the caller MUST NOT invoke ``dummy_order_service``
    — the SR series for today has not completed its backfill and any
    order created against it would be derived from partial data.

    Failure to *query* the latch is treated as NOT ready (fail-safe).
    We would rather skip a 30-second tick than create an order against
    a series we cannot verify. The warning is loud so the operator
    notices and fixes the probe.
    """
    try:
        from services.sr_backfill_service import sr_backfill_service
    except Exception as exc:
        logger.warning(
            "[scheduler] SR backfill latch unavailable — treating as "
            "NOT ready: %s: %s",
            type(exc).__name__, exc,
        )
        return False, f"import_error:{type(exc).__name__}"

    probe = getattr(sr_backfill_service, "is_ready_for_today", None)
    if probe is None:
        logger.warning(
            "[scheduler] sr_backfill_service has no is_ready_for_today() "
            "probe — treating as NOT ready. Update the service to expose "
            "the latch consumed by main.py.",
        )
        return False, "missing_probe"

    try:
        ready = bool(probe())
    except Exception as exc:
        logger.exception("[scheduler] SR backfill readiness probe raised")
        return False, f"probe_error:{type(exc).__name__}"

    return ready, "ready" if ready else "sr_backfill_not_ready_today"


# --------------------------------------------------------------------------- #
# Replay-series immutability flag for the periodic live-buffer refresh        #
# --------------------------------------------------------------------------- #
# Option (a) from the 2026-11-04 brief: the every-2-minute refresh updates
# only the live buffer. The replay series used by the startup catch-up is
# built once at startup and then frozen for the day, so that
# DUMMY_ORDER_DRIFT_GUARD can guarantee existing orders are never
# recomputed against a shifting snapshot. Changing this to True would
# re-enable option (b) and require dummy_order_service to tag every new
# order with its snapshot_epoch; that is NOT the current design.
_LIVE_SR_REFRESH_UPDATE_REPLAY = False


# --------------------------------------------------------------------------- #
# Replay-seed-at-open constants                                              #
# --------------------------------------------------------------------------- #
# 09:16 IST (one minute after the market opens). Rationale for the offset:
#   * 09:15:00 — the intraday API may not yet serve the opening candle;
#     seeding zero candles would still flip is_ready_for_today()=True,
#     which is a worse outcome than skipping.
#   * 09:16:00 — the opening 1-min candle is reliably available from
#     Upstox, so the seed populates a usable replay.
#   * misfire_grace_time=600 — if the scheduler is briefly paused around
#     09:16, the job still fires (up to 09:26). Beyond that, the operator
#     can trigger the seed manually via POST /admin/hard-refresh.
_REPLAY_SEED_HOUR = 9
_REPLAY_SEED_MINUTE = 16
_REPLAY_SEED_MISFIRE_GRACE_SECONDS = 600


class SchedulerService:
    def __init__(self) -> None:
        self._scheduler: BackgroundScheduler | None = None

    @property
    def scheduler(self) -> BackgroundScheduler | None:
        return self._scheduler

    # ------------------------------------------------------------------ #
    # Public latch accessor — used by external ticks (e.g. a future      #
    # _strategy2_recheck) that need to honour the same SR-backfill      #
    # gate as the in-file dummy_order_tracker.                          #
    # ------------------------------------------------------------------ #
    def is_sr_backfill_ready(self) -> tuple[bool, str]:
        """Public accessor mirroring the module-level ``sr_backfill_ready``.

        Exposed on the singleton so out-of-file ticks can call
        ``scheduler_service.is_sr_backfill_ready()`` without importing
        the module-level helper directly.
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

        # -- Live SR refresh from Upstox API (LIVE BUFFER ONLY) -------- #
        # 2026-11-04 change: this tick MUST NOT touch the replay series
        # consumed by the startup catch-up. Otherwise
        # DUMMY_ORDER_DRIFT_GUARD would have to recompute entry/stop/
        # target for any pre-existing order whenever the snapshot_epoch
        # advanced — which is exactly the behaviour we removed. We now
        # pass ``update_replay=False``; the replay series is frozen
        # after the startup backfill and remains stable for the day.
        if getattr(config, "LIVE_SR_REFRESH_FROM_API_ENABLED", True):
            def _refresh_live_sr() -> None:
                try:
                    from services.sr_backfill_service import sr_backfill_service
                    try:
                        summary = sr_backfill_service.run(
                            reason="periodic_refresh",
                            send_telegram=False,
                            update_replay=_LIVE_SR_REFRESH_UPDATE_REPLAY,
                        )
                    except TypeError:
                        # The service has not yet been updated to accept
                        # ``update_replay``. Fall back to the legacy
                        # signature — which WILL reseed the replay
                        # series — and log loudly so the operator knows
                        # the safe path is not yet available.
                        logger.warning(
                            "[scheduler] sr_backfill_service.run() does not "
                            "accept update_replay=; falling back to the "
                            "legacy signature. The replay series WILL be "
                            "refreshed by this tick — DUMMY_ORDER_DRIFT_GUARD "
                            "must remain enabled until the service exposes "
                            "update_replay=False.",
                        )
                        summary = sr_backfill_service.run(
                            reason="periodic_refresh",
                            send_telegram=False,
                        )
                    logger.info(
                        "[scheduler] live_sr_refresh_from_api — "
                        "status=%s instruments=%s seeded=%s errors=%s "
                        "update_replay=%s",
                        summary.get("status"),
                        summary.get("instruments_total"),
                        summary.get("instruments_seeded"),
                        summary.get("errors"),
                        _LIVE_SR_REFRESH_UPDATE_REPLAY,
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

        # -- Replay-series seed at market open (2026-11-04) ------------- #
        # Why this job exists
        # -------------------
        # Feed-start (live_feed_service._run_sr_backfill) is the primary
        # trigger for the first replay-series seed. It runs synchronously
        # inside live_feed_service.start(), which the startup pipeline
        # invokes BEFORE scheduler_service.start() — so on any normal
        # startup the replay is already seeded by the time
        # _dummy_order_tick is first eligible to fire.
        #
        # BUT: feed-start's backfill is gated on
        # sr_backfill_service.should_run(), which is window-gated to
        # [09:15, 23:59] IST on weekdays. If the process comes up
        # *before* 09:15 (e.g. 00:33 or 08:55), feed-start correctly
        # skips, and without this job NOTHING flips
        # is_ready_for_today() for the rest of the day —
        # dummy_order_tracker would skip every 30-second tick forever.
        #
        # This job is the missing retry:
        #   * Idempotent — returns immediately if is_ready_for_today()
        #     already True (normal startup path — feed-start already
        #     seeded).
        #   * update_replay=True — this is the ONLY deferred path
        #     allowed to touch the replay series. The periodic
        #     _refresh_live_sr tick keeps update_replay=False.
        #   * Window-gated by should_run() internally, so it never
        #     fires outside the trading window even if config drifts.
        #   * Runs one minute after open so the 09:15 candle is
        #     reliably available from the intraday API.
        if getattr(config, "SR_BACKFILL_ENABLED", True):
            def _seed_replay_series_at_open() -> None:
                try:
                    from services.sr_backfill_service import sr_backfill_service
                except Exception:
                    logger.exception(
                        "[scheduler] replay_seed_at_open: "
                        "sr_backfill_service import failed",
                    )
                    return

                # Idempotent fast-path: already seeded today.
                probe = getattr(
                    sr_backfill_service, "is_ready_for_today", None,
                )
                if probe is not None:
                    try:
                        if bool(probe()):
                            logger.info(
                                "[scheduler] replay_seed_at_open — "
                                "already seeded, skip",
                            )
                            return
                    except Exception:
                        logger.exception(
                            "[scheduler] replay_seed_at_open: "
                            "is_ready_for_today() probe raised; "
                            "proceeding to seed attempt",
                        )

                # Respect the trading-window gate (never fires outside it).
                should_run_fn = getattr(
                    sr_backfill_service, "should_run", None,
                )
                if should_run_fn is not None:
                    try:
                        should_run, skip_reason = should_run_fn()
                    except Exception:
                        logger.exception(
                            "[scheduler] replay_seed_at_open: "
                            "should_run() probe raised; proceeding to seed "
                            "attempt",
                        )
                        should_run, skip_reason = True, "probe_error"
                    if not should_run:
                        logger.info(
                            "[scheduler] replay_seed_at_open — "
                            "skipped: %s",
                            skip_reason,
                        )
                        return

                # Seed the replay series. This is the ONLY deferred path
                # allowed to pass update_replay=True.
                try:
                    summary = sr_backfill_service.run(
                        reason="market_open_backfill",
                        update_replay=True,
                    )
                    logger.info(
                        "[scheduler] replay_seed_at_open — "
                        "status=%s instruments=%s seeded=%s errors=%s "
                        "update_replay=True",
                        summary.get("status"),
                        summary.get("instruments_total"),
                        summary.get("instruments_seeded"),
                        summary.get("errors"),
                    )
                except Exception:
                    logger.exception(
                        "[scheduler] replay_seed_at_open failed; "
                        "dummy_order_tracker will continue to skip until "
                        "the seed succeeds (retry via POST /admin/hard-refresh "
                        "or wait for the next scheduled retry)",
                    )

            scheduler.add_job(
                _seed_replay_series_at_open, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=_REPLAY_SEED_HOUR,
                minute=_REPLAY_SEED_MINUTE,
                id="replay_seed_at_open",
                replace_existing=True,
                misfire_grace_time=_REPLAY_SEED_MISFIRE_GRACE_SECONDS,
            )

        # -- Dummy order live tracker ----------------------------------- #
        # Every DUMMY_ORDER_TRACK_INTERVAL_SECONDS (default 30 s) the
        # service polls the entry signal, creates any missing dummy
        # orders for armed candidates, updates open orders from live
        # LTP + periodic bar reconcile, and auto-closes at EOD.
        #
        # 2026-11-04: this tick now SHORT-CIRCUITS while the SR backfill
        # latch for today is not ready. The same latch is consulted by
        # main.py; here it prevents a mid-day restart (e.g. 14:16 on
        # 2026-10-29) from creating an order against a partially-built
        # SR series during the backfill window. It also gates against
        # the pre-09:15-startup gap covered by replay_seed_at_open
        # above — the tracker simply skips until that seed completes.
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

        # ------------------------------------------------------------------ #
        # DEFENSIVE CONTRACT — out-of-file ticks that invoke                 #
        # dummy_order_service (e.g. a future ``_strategy2_recheck``) MUST   #
        # call ``sr_backfill_ready()`` (module-level helper above) or the  #
        # singleton method ``scheduler_service.is_sr_backfill_ready()``    #
        # before calling into the service. Skipping this guard reopens the #
        # 14:16-on-2026-10-29 class of bug.                                #
        #                                                                   #
        # If you add a new job that indirectly reaches dummy_order_service,#
        # wrap it as:                                                       #
        #                                                                   #
        #     def _my_tick() -> None:                                       #
        #         ready, reason = sr_backfill_ready()                       #
        #         if not ready:                                             #
        #             logger.info("[scheduler] %s skipped — %s",            #
        #                         "_my_tick", reason)                       #
        #             return                                                #
        #         ...   # safe to invoke dummy_order_service here           #
        #                                                                   #
        # The corresponding test lives at tests/test_scheduler_service.py  #
        # (or equivalent) and asserts that no tick can reach                #
        # dummy_order_service when sr_backfill_ready() returns False.      #
        # ------------------------------------------------------------------ #

        scheduler.start()
        self._scheduler = scheduler

        logger.info(
            "Scheduler started — daily=%02d:%02d, first_candle=%02d:%02d:%02d, "
            "digest=%02d:%02d, preflight=%02d:%02d, "
            "level_isolation_recheck=*/5 (9-15), DailyBackup=%02d:%02d, "
            "live_sr_refresh=%dm (enabled=%s, update_replay=%s), "
            "replay_seed_at_open=%02d:%02d (enabled=%s, gated_by=is_ready_for_today), "
            "dummy_order_tracker=%ds (enabled=%s, gated_by=sr_backfill_ready)",
            config.DAILY_REFRESH_HOUR, config.DAILY_REFRESH_MINUTE,
            getattr(config, "FIRST_CANDLE_HOUR", 9),
            getattr(config, "FIRST_CANDLE_MINUTE", 16),
            getattr(config, "FIRST_CANDLE_SECOND", 10),
            config.READINESS_DIGEST_HOUR, config.READINESS_DIGEST_MINUTE,
            config.READINESS_PREFLIGHT_HOUR, config.READINESS_PREFLIGHT_MINUTE,
            config.DAILY_BACKUP_HOUR, config.DAILY_BACKUP_MINUTE,
            int(getattr(config, "LIVE_SR_REFRESH_FROM_API_MINUTES", 2)),
            bool(getattr(config, "LIVE_SR_REFRESH_FROM_API_ENABLED", True)),
            _LIVE_SR_REFRESH_UPDATE_REPLAY,
            _REPLAY_SEED_HOUR,
            _REPLAY_SEED_MINUTE,
            bool(getattr(config, "SR_BACKFILL_ENABLED", True)),
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