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
    * strategy_2_stage3_recheck     — cron, every 5 min between
                                      STRATEGY_2_TRACKER_WINDOW_START and
                                      STRATEGY_2_TRACKER_WINDOW_END
                                      (self-gating: only fires when
                                       Stage 2 has produced at least
                                       one isolated contract, so the
                                       "no isolation yet" zero-data
                                       Telegram messages stop at the
                                       source)

Jobs (Daily backup):
    * daily_backup                  — cron, 16:00 IST every day (default)
        Zips the folders listed in DAILY_BACKUP_FOLDERS and saves the
        archive inline to MongoDB when it fits under
        DAILY_BACKUP_MAX_BLOB_MB, otherwise saves metadata-only with a
        clear "skipped_size_limit" reason. Gated by DAILY_BACKUP_ENABLED.

Strategy 2 recheck self-gating
------------------------------
The 5-minute recheck (`strategy_2_stage3_recheck`) is wrapped in
`_strategy2_recheck()`, which:

  1. Short-circuits when the local stage-2 tree
     (`<STRATEGY_BASE_DIR>/<STRATEGY_STAGE_2_NAME>`) does not exist or
     contains no strike folders — i.e. Stage 1/2 produced nothing.
  2. Otherwise delegates to `run_strategy_2_isolation`, which itself
     suppresses Telegram output when nothing changed (see
     `token_tasks/strategy_2_isolation_task.py`).

Result: no more "Stage 2 contracts: 0 / Touched: 0 / No contracts
touched the isolation level yet" messages every 5 minutes.
"""

from __future__ import annotations

import logging
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from core import config

logger = logging.getLogger(__name__)


def _stage2_tree_has_contracts() -> bool:
    """Return True when the stage-2 isolation tree has any strike folders.

    This is the gate for the Strategy 2 Stage 3 5-minute recheck: if
    Stage 1/2 produced nothing, there is nothing to watch and running
    the isolation service on every tick would only generate zero-data
    "no isolation yet" log lines and (previously) Telegram messages.

    The check is intentionally cheap — it walks the top three levels
    of the tree and returns as soon as it finds one strike folder. It
    never raises; any I/O error is treated as "no contracts" so the
    scheduler keeps running.
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


def _strategy2_recheck() -> None:
    """Self-gating wrapper for the 5-minute Strategy 2 Stage 3 recheck.

    Skips the isolation run entirely when Stage 2 has no isolated
    contracts, so the scheduler stops producing zero-data rechecks
    (and their Telegram messages) on quiet days and on days where
    Stage 1 was skipped due to a late OR candle.
    """
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

        # -- Strategy 1 -------------------------------------------------- #
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

        # -- Strategy 2 -------------------------------------------------- #
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

            # Periodic Stage 3 re-check every 5 minutes, but self-gated:
            # `_strategy2_recheck` short-circuits when the stage-2 tree
            # has no isolated contracts, so the "no isolation yet"
            # zero-data messages stop at the source. When the tree does
            # have contracts, `run_strategy_2_isolation` still applies
            # its own Telegram gate (state-change only).
            scheduler.add_job(
                _strategy2_recheck, "cron",
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