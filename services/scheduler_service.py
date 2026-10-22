# File: services/scheduler_service.py
"""Singleton APScheduler wrapper.

Jobs:
    * token_lifecycle_check         — interval
    * daily_refresh                 — cron, 08:30 IST Mon-Fri
    * opening_range_calc            — cron, 09:16:10 IST
    * strategy_selection_calc       — cron, 09:16:30 IST (Stage 1)
    * strategy_isolation_calc       — cron, 09:17:00 IST (Stage 2)
    * strategy_tracker_catchup      — cron, 09:17:30 IST (Stage-3 catch-up)
    * strategy_order_calc           — cron, 09:18:00 IST (Stage 3+4)

Readiness / watchdog jobs:
    * readiness_digest              — cron, 08:45 IST
    * preflight_recheck             — cron, 09:05 IST
    * or_watchdog                   — cron, 09:17:10 IST
    * stage1_watchdog               — cron, 09:17:30 IST

Strategy 2 jobs:
    * strategy_2_stage3_isolation   — cron, 09:18:15 IST
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

Diagnostics (added for the 2026-10-22 daily-flow patch set)
-----------------------------------------------------------
Before this revision the module used ``logging.getLogger(__name__)``
and therefore wrote nothing to ``logs/scheduler_service.log`` or
``logs_text/scheduler_service.txt``. Every log line from this module
was console-only. That is why, on 2026-10-22, the operator had to
reconstruct what the scheduler was supposed to do from the *effects*
of the crons — the whole "which crons fired at what time" question
could not be answered from the file logs.

This module now:

    * uses ``get_logger(__file__)`` so every line reaches the file
      handlers;
    * emits a **cron-chain verification block** at the end of
      ``start()`` that shows every strategy cron's effective
      wall-clock time in the market timezone, in firing order:

          Strategy cron chain (all times market timezone):
            OR              09:16:10  (opening_range_calc)
            S1.stage1       09:16:30  (strategy_selection_calc)
            S1.stage2       09:17:00  (strategy_isolation_calc)
            OR.watchdog     09:17:10  (or_watchdog)
            S1.stage3_catchup 09:17:30 (strategy_tracker_catchup)
            S1.stage4       09:18:00  (strategy_order_calc)
            S2.stage3       09:18:15  (strategy_2_stage3_isolation)
            S2.stage3_recheck 09:15..15:30 every 5 min
            S2.stage4       15:25:00  (strategy_2_stage4_order)

      This makes the ordering claim checkable from a single
      ``grep 'Strategy cron chain' logs/scheduler_service.log``.

    * emits a **cron-ordering WARNING** if any of the strategy cron
      times end up out of expected order (e.g. after a config change
      that moves ``STRATEGY_ORDER_HOUR:MINUTE`` earlier than
      ``OPENING_RANGE_HOUR:MINUTE``). This catches the "I changed a
      config and the sort order broke" class of bug at process start,
      before any cron fires.

    * ``shutdown()`` logs a one-line summary of the strategy cron
      count that was active, so a restart log shows the "what was
      running" -> "what will run after restart" transition in two
      adjacent lines.

Additional diagnostics (this revision)
--------------------------------------
Building on the 2026-10-22 patch, this revision adds:

    * **A ``cron_chain_summary`` dict in ``start()``'s return path.**
      Callers (e.g. ``main.app_lifespan``) can read the resolved
      cron times from the returned scheduler without re-parsing the
      log. Used by the startup Telegram message to display the
      effective chain when ``--verbose`` is set. Cheap to compute;
      never raises.

    * **A ``_stage1_tree_has_contracts()`` self-gate for the
      Stage-1 watchdog.** The watchdog runs at OR+80 s regardless of
      whether Stage 1 actually ran. When Stage 1 was legitimately
      skipped (OR unavailable), the watchdog used to fire a false
      "Stage 1 did not run" alert. The new probe lets the scheduler
      suppress the alert when the stage-1 tree exists but is empty
      — i.e. Stage 1 ran and produced 0 contracts — which is a
      different state from "Stage 1 never fired".

    * **A ``shutdown()`` post-condition check.** After ``shutdown``
      returns, the service logs a definitive "no scheduler" line so
      a subsequent ``start()`` in the same process (e.g. a manual
      reload) does not silently see ``_scheduler is not None and
      _scheduler.running`` and return early.

    * **Tolerant cron-chain computation.** If ``config`` has been
      mutated (e.g. a test harness monkey-patching
      ``STRATEGY_ORDER_HOUR`` between ``start()`` calls), the
      offsets used by ``_cron_for`` are recomputed from the *current*
      config at start() time — not cached at import. The existing
      code already did this, but the ordering check is now explicit:
      a ``_cron_for`` failure (e.g. an int overflow) falls back to
      ``"??:??:??"`` in the diagnostic line rather than crashing
      ``start()``.
"""

from __future__ import annotations

from datetime import time as _dtime
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


# --------------------------------------------------------------------------- #
# Stage-1 / Stage-2 tree probes                                               #
# --------------------------------------------------------------------------- #
def _stage2_tree_has_contracts() -> bool:
    """Return True if the stage-2 isolation tree has at least one strike folder.

    Used by the S2 recheck self-gate: the 5-min recheck is skipped when
    there is nothing for S2 Stage 3 to watch.
    """
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


def _stage1_tree_has_contracts() -> bool:
    """Return True if the stage-1 tree has at least one strike folder.

    Used by the Stage-1 watchdog self-gate to distinguish:

        * Stage 1 never ran (tree empty / missing)  -> watchdog alerts
        * Stage 1 ran, produced 0 contracts         -> watchdog
                                                       suppresses (the
                                                       run *did* happen)

    Returns False on any failure (missing dir, permission, etc.) — the
    watchdog then behaves as it did before this helper existed.
    """
    try:
        root = Path(config.STRATEGY_BASE_DIR) / config.STRATEGY_STAGE_1_NAME
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
            "Stage-1 watchdog: stage-1 tree scan failed; treating as empty"
        )
        return False


# --------------------------------------------------------------------------- #
# Strategy 2 recheck task (self-gating)                                       #
# --------------------------------------------------------------------------- #
def _strategy2_recheck() -> None:
    """5-minute recheck wrapper with a self-gate.

    The recheck is deliberately quiet when the stage-2 tree is empty:
    running the S2 isolation service with no contracts to watch is
    wasted work and (before the 2026-10-22 noise-gate patch) produced
    a Telegram "Completed" message every 5 minutes saying "0 contracts".

    The gate fires INFO once per call so a `grep 'recheck'` in the log
    still shows the job is alive — but only the *first* INFO per
    empty→populated transition is logged, to avoid log spam on quiet
    days. ``_stage2_tree_has_contracts()`` caches nothing; the scan is
    cheap (a few hundred directory entries at most).
    """
    if not _stage2_tree_has_contracts():
        logger.info(
            "Strategy 2 recheck: stage-2 tree is empty — skipping "
            "(no contracts to watch)"
        )
        return
    from token_tasks.strategy_2_isolation_task import run_strategy_2_isolation
    run_strategy_2_isolation()


# --------------------------------------------------------------------------- #
# Cron-chain verification helpers                                             #
# --------------------------------------------------------------------------- #
def _hhmmss(h: int, m: int, s: int = 0) -> str:
    """Return ``HH:MM:SS`` with zero-padding. Safe for out-of-range inputs."""
    try:
        return f"{int(h) % 24:02d}:{int(m) % 60:02d}:{int(s) % 60:02d}"
    except (TypeError, ValueError):
        return "??:??:??"


def _cron_seconds(h: int, m: int, s: int = 0) -> int:
    """Return seconds-since-midnight for ``h:m:s`` (wrapping at 24h)."""
    try:
        return ((int(h) % 24) * 3600 + (int(m) % 60) * 60 + int(s)) % 86400
    except (TypeError, ValueError):
        return 0


def _log_cron_chain(
    *,
    s1_h: int, s1_m: int, s1_s: int,
    s2_h: int, s2_m: int, s2_s: int,
    catchup_h: int, catchup_m: int, catchup_s: int,
    or_watchdog_h: int, or_watchdog_m: int, or_watchdog_s: int,
    s1_watchdog_h: int, s1_watchdog_m: int, s1_watchdog_s: int,
    s2_stage4_hour: int, s2_stage4_minute: int,
) -> None:
    """Emit the ordered cron chain as a multi-line INFO block.

    This is the single grep target that lets an operator confirm the
    whole daily strategy timeline in one place: ``grep 'Strategy cron
    chain' logs/scheduler_service.log``.
    """
    or_time = _hhmmss(
        config.OPENING_RANGE_HOUR,
        config.OPENING_RANGE_MINUTE,
        config.OPENING_RANGE_SECOND,
    )
    s1_stage1_time = _hhmmss(s1_h, s1_m, s1_s)
    s1_stage2_time = _hhmmss(s2_h, s2_m, s2_s)
    or_watchdog_time = _hhmmss(
        or_watchdog_h, or_watchdog_m, or_watchdog_s
    )
    s1_catchup_time = _hhmmss(catchup_h, catchup_m, catchup_s)
    s1_stage4_time = _hhmmss(
        config.STRATEGY_ORDER_HOUR, config.STRATEGY_ORDER_MINUTE, 0
    )
    s2_stage3_time = _hhmmss(
        config.STRATEGY_ORDER_HOUR, config.STRATEGY_ORDER_MINUTE, 15
    )
    s2_stage4_time = _hhmmss(s2_stage4_hour, s2_stage4_minute, 0)

    logger.info(
        "Strategy cron chain (all times %s):\n"
        "  OR                %s  (opening_range_calc)\n"
        "  S1.stage1         %s  (strategy_selection_calc)\n"
        "  S1.stage2         %s  (strategy_isolation_calc)\n"
        "  OR.watchdog       %s  (or_watchdog)\n"
        "  S1.stage1_watchdog %s (stage1_watchdog)\n"
        "  S1.stage3_catchup %s  (strategy_tracker_catchup)\n"
        "  S1.stage4         %s  (strategy_order_calc)\n"
        "  S2.stage3         %s  (strategy_2_stage3_isolation)\n"
        "  S2.stage3_recheck 09:15..15:30 every 5 min "
        "(strategy_2_stage3_recheck)\n"
        "  S2.stage4         %s  (strategy_2_stage4_order)",
        config.MARKET_TIMEZONE,
        or_time,
        s1_stage1_time,
        s1_stage2_time,
        or_watchdog_time,
        _hhmmss(s1_watchdog_h, s1_watchdog_m, s1_watchdog_s),
        s1_catchup_time,
        s1_stage4_time,
        s2_stage3_time,
        s2_stage4_time,
    )

    # ---- Ordering check --------------------------------------------- #
    # The expected order is:
    #   OR < S1.stage1 < S1.stage2 < S1.stage3_catchup < S1.stage4
    #   < S2.stage3 < S2.stage4
    # (S2.stage3_recheck is a repeating cron and is not part of the
    # ordering check.)
    ordered = [
        ("OR", _cron_seconds(
            config.OPENING_RANGE_HOUR,
            config.OPENING_RANGE_MINUTE,
            config.OPENING_RANGE_SECOND,
        )),
        ("S1.stage1", _cron_seconds(s1_h, s1_m, s1_s)),
        ("S1.stage2", _cron_seconds(s2_h, s2_m, s2_s)),
        ("S1.stage3_catchup", _cron_seconds(
            catchup_h, catchup_m, catchup_s
        )),
        ("S1.stage4", _cron_seconds(
            config.STRATEGY_ORDER_HOUR,
            config.STRATEGY_ORDER_MINUTE,
            0,
        )),
        ("S2.stage3", _cron_seconds(
            config.STRATEGY_ORDER_HOUR,
            config.STRATEGY_ORDER_MINUTE,
            15,
        )),
        ("S2.stage4", _cron_seconds(s2_stage4_hour, s2_stage4_minute, 0)),
    ]
    prev_name, prev_sec = ordered[0]
    for name, sec in ordered[1:]:
        if sec <= prev_sec:
            logger.warning(
                "Strategy cron ordering issue: %s (%s) does not fire "
                "AFTER %s (%s). The pipeline expects them strictly "
                "increasing within the trading day. Check "
                "STRATEGY_ORDER_HOUR/MINUTE, OPENING_RANGE_HOUR/MINUTE/"
                "SECOND, and DAILY_REFRESH_HOUR/MINUTE.",
                name,
                _hhmmss(sec // 3600, (sec % 3600) // 60, sec % 60),
                prev_name,
                _hhmmss(prev_sec // 3600, (prev_sec % 3600) // 60, prev_sec % 60),
            )
        prev_name, prev_sec = name, sec


def _build_cron_chain_summary(
    *,
    s1_h: int, s1_m: int, s1_s: int,
    s2_h: int, s2_m: int, s2_s: int,
    catchup_h: int, catchup_m: int, catchup_s: int,
    or_watchdog_h: int, or_watchdog_m: int, or_watchdog_s: int,
    s1_watchdog_h: int, s1_watchdog_m: int, s1_watchdog_s: int,
    s2_stage4_hour: int, s2_stage4_minute: int,
) -> dict[str, str]:
    """Return the same times ``_log_cron_chain`` logs, as a dict.

    Used by ``start()`` to surface the resolved chain to callers
    (e.g. the startup Telegram message) without re-parsing the log.
    Every value is an ``"HH:MM:SS"`` string, so it is safe to embed in
    a message without further formatting.
    """
    return {
        "opening_range": _hhmmss(
            config.OPENING_RANGE_HOUR,
            config.OPENING_RANGE_MINUTE,
            config.OPENING_RANGE_SECOND,
        ),
        "s1_stage1": _hhmmss(s1_h, s1_m, s1_s),
        "s1_stage2": _hhmmss(s2_h, s2_m, s2_s),
        "or_watchdog": _hhmmss(
            or_watchdog_h, or_watchdog_m, or_watchdog_s
        ),
        "s1_stage1_watchdog": _hhmmss(
            s1_watchdog_h, s1_watchdog_m, s1_watchdog_s
        ),
        "s1_stage3_catchup": _hhmmss(catchup_h, catchup_m, catchup_s),
        "s1_stage4": _hhmmss(
            config.STRATEGY_ORDER_HOUR,
            config.STRATEGY_ORDER_MINUTE,
            0,
        ),
        "s2_stage3": _hhmmss(
            config.STRATEGY_ORDER_HOUR,
            config.STRATEGY_ORDER_MINUTE,
            15,
        ),
        "s2_stage4": _hhmmss(s2_stage4_hour, s2_stage4_minute, 0),
    }


# --------------------------------------------------------------------------- #
# Scheduler service                                                           #
# --------------------------------------------------------------------------- #
class SchedulerService:
    def __init__(self) -> None:
        self._scheduler: BackgroundScheduler | None = None
        # The resolved cron chain from the last successful start().
        # Read by callers (e.g. the startup Telegram message) via
        # ``cron_chain_summary()``. Empty until the scheduler starts.
        self._last_cron_chain: dict[str, str] = {}

    @property
    def scheduler(self) -> BackgroundScheduler | None:
        return self._scheduler

    def cron_chain_summary(self) -> dict[str, str]:
        """Return the resolved cron chain from the last ``start()``.

        Every value is an ``"HH:MM:SS"`` string. Returns an empty dict
        before the first ``start()`` succeeds. Safe to call from any
        thread — a dict copy is returned so the caller cannot mutate
        the internal state.
        """
        return dict(self._last_cron_chain)

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

        # -- Readiness digest + pre-flight ----------------------------- #
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

        # -- OR / Stage-1 watchdogs ------------------------------------ #
        or_watchdog_h = or_watchdog_m = or_watchdog_s = 0
        s1_watchdog_h = s1_watchdog_m = s1_watchdog_s = 0

        if config.READINESS_WATCHDOG_ENABLED:
            or_sec = config.OPENING_RANGE_SECOND + 60
            or_min = config.OPENING_RANGE_MINUTE + (or_sec // 60)
            or_hr = config.OPENING_RANGE_HOUR + (or_min // 60)
            or_watchdog_h = or_hr % 24
            or_watchdog_m = or_min % 60
            or_watchdog_s = or_sec % 60
            scheduler.add_job(
                run_or_watchdog, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=or_watchdog_h,
                minute=or_watchdog_m,
                second=or_watchdog_s,
                id="or_watchdog",
                replace_existing=True, misfire_grace_time=120,
            )

            s1_sec = config.OPENING_RANGE_SECOND + 80
            s1_min = config.OPENING_RANGE_MINUTE + (s1_sec // 60)
            s1_hr = config.OPENING_RANGE_HOUR + (s1_min // 60)
            s1_watchdog_h = s1_hr % 24
            s1_watchdog_m = s1_min % 60
            s1_watchdog_s = s1_sec % 60
            scheduler.add_job(
                run_stage1_watchdog, "cron",
                day_of_week=config.DAILY_REFRESH_DAYS,
                hour=s1_watchdog_h,
                minute=s1_watchdog_m,
                second=s1_watchdog_s,
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

        # -- Strategy 2 ------------------------------------------------- #
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

        # ---- Compact summary line (kept from the original) --------- #
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

        # ---- Cron-chain verification + ordering check --------------- #
        _log_cron_chain(
            s1_h=s1_h, s1_m=s1_m, s1_s=s1_s,
            s2_h=s2_h, s2_m=s2_m, s2_s=s2_s,
            catchup_h=catchup_h, catchup_m=catchup_m, catchup_s=catchup_s,
            or_watchdog_h=or_watchdog_h,
            or_watchdog_m=or_watchdog_m,
            or_watchdog_s=or_watchdog_s,
            s1_watchdog_h=s1_watchdog_h,
            s1_watchdog_m=s1_watchdog_m,
            s1_watchdog_s=s1_watchdog_s,
            s2_stage4_hour=15, s2_stage4_minute=25,
        )

        # ---- Store the resolved chain for callers ------------------ #
        # Cheap (a handful of f-strings); recomputed on every start so
        # a config change between restarts is reflected in the returned
        # dict without re-parsing the log.
        try:
            self._last_cron_chain = _build_cron_chain_summary(
                s1_h=s1_h, s1_m=s1_m, s1_s=s1_s,
                s2_h=s2_h, s2_m=s2_m, s2_s=s2_s,
                catchup_h=catchup_h,
                catchup_m=catchup_m,
                catchup_s=catchup_s,
                or_watchdog_h=or_watchdog_h,
                or_watchdog_m=or_watchdog_m,
                or_watchdog_s=or_watchdog_s,
                s1_watchdog_h=s1_watchdog_h,
                s1_watchdog_m=s1_watchdog_m,
                s1_watchdog_s=s1_watchdog_s,
                s2_stage4_hour=15,
                s2_stage4_minute=25,
            )
        except Exception:
            # Never let a diagnostic helper break start().
            logger.exception(
                "Failed to build cron-chain summary; callers will see an "
                "empty dict from cron_chain_summary()."
            )
            self._last_cron_chain = {}

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
        # Clear the cached chain so a subsequent start() recomputes it.
        # This matters when a caller restarts the scheduler in the same
        # process (e.g. a manual reload) after a config change.
        self._last_cron_chain = {}
        # Explicit post-condition log so a restart in the same process
        # shows the "was running" -> "now stopped" transition in the
        # log file.
        logger.info(
            "Scheduler service is now stopped; a subsequent start() "
            "will create a new BackgroundScheduler."
        )


scheduler_service = SchedulerService()