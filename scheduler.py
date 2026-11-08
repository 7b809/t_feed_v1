# scheduler.py
"""Scheduler: daily refresh, pivot check, isolation scan, disconnect.

Deferred startup
----------------
When ``main.lifespan`` decides to defer startup (TEST_FLAG=false and
current time < cutoff), it calls::

    start_scheduler(deferred_until=<cutoff datetime>)

This registers all normal recurring jobs **plus** a one-shot
``deferred_startup`` job at the cutoff. At that instant the one-shot
calls :func:`jobs.deferred_startup.run_deferred_startup_sequence` which
runs the startup pipeline. Both the deferred kickoff and the recurring
``daily_refresh @ 09:00`` cron land at the same second; the
``run_daily_refresh`` per-day guard ensures only one actually runs the
pipeline.
"""

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.config import (
    TIMEZONE,
    DAILY_REFRESH_HOUR, DAILY_REFRESH_MINUTE,
    PIVOT_CHECK_HOUR, PIVOT_CHECK_MINUTE,
    DISCONNECT_HOUR, DISCONNECT_MINUTE,
    ISOLATION_SCAN_INTERVAL_SECONDS,
)
from core.logger import get_logger
from core.telegram_notifier import send_telegram_message
from jobs.daily_refresh import (
    run_daily_refresh, disconnect_feed,
)
from jobs.deferred_startup import run_deferred_startup_sequence
from jobs.isolation_scan import run_isolation_scan

logger = get_logger(__file__)


def _pivot_check_job() -> None:
    """Re-check pivot levels at 09:18 (see config).

    NOTE: this uses the legacy ``is_pivot_levels_updated_today`` check,
    which trusts Mongo's ``trade_date_ist`` field. When a deferred
    startup runs *before* 09:15, ``_prepare_or_levels`` writes a
    ``trade_date_ist=<today>`` document from **yesterday's** candles. In
    that case this job no-ops and OR levels stay stale until the next
    restart. Recommendation: set ``DEFERRED_STARTUP_MINUTE=17`` or
    ``18`` so the deferred daily refresh sees today's first candle.
    """
    try:
        from core.pivot_levels_loader import (
            is_pivot_levels_updated_today as updated,
            load_pivot_levels_to_local,
        )
        if updated():
            load_pivot_levels_to_local()
            logger.info("Pivot levels reloaded at 09:18 job")
        else:
            logger.warning("Pivot levels still not updated at 09:18 job")
    except Exception:
        logger.exception("Pivot check job failed")


def start_scheduler(deferred_until=None) -> BackgroundScheduler:
    """Start the background scheduler.

    :param deferred_until: optional ``datetime`` (IST-aware). When
        provided, registers a one-shot ``deferred_startup`` job at that
        instant which runs the deferred startup sequence.
    """
    scheduler = BackgroundScheduler(timezone=TIMEZONE)

    # Daily refresh (idempotent per day, so concurrent kickoff is safe)
    scheduler.add_job(
        run_daily_refresh,
        CronTrigger(
            day_of_week="mon-fri",
            hour=DAILY_REFRESH_HOUR,
            minute=DAILY_REFRESH_MINUTE,
            timezone=TIMEZONE,
        ),
        id="daily_refresh",
        name="Daily Refresh",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Pivot-levels recheck
    scheduler.add_job(
        _pivot_check_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour=PIVOT_CHECK_HOUR,
            minute=PIVOT_CHECK_MINUTE,
            timezone=TIMEZONE,
        ),
        id="pivot_check",
        name="Pivot Levels Check",
        replace_existing=True,
    )

    # Isolation scan
    scheduler.add_job(
        run_isolation_scan,
        IntervalTrigger(seconds=ISOLATION_SCAN_INTERVAL_SECONDS),
        id="isolation_scan",
        name=f"Isolation Scan ({ISOLATION_SCAN_INTERVAL_SECONDS}s)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # End-of-day disconnect
    scheduler.add_job(
        disconnect_feed,
        CronTrigger(
            day_of_week="mon-fri",
            hour=DISCONNECT_HOUR,
            minute=DISCONNECT_MINUTE,
            timezone=TIMEZONE,
        ),
        id="disconnect_feed",
        name="Disconnect Feed",
        replace_existing=True,
    )

    # Deferred startup one-shot (only when the caller requested it)
    if deferred_until is not None:
        scheduler.add_job(
            run_deferred_startup_sequence,
            DateTrigger(run_date=deferred_until, timezone=TIMEZONE),
            id="deferred_startup",
            name=f"Deferred Startup @ {deferred_until.strftime('%H:%M')}",
            replace_existing=True,
            misfire_grace_time=300,
            max_instances=1,
            coalesce=True,
        )
        logger.warning(
            "[STARTUP-GATE] Deferred startup scheduled at %s",
            deferred_until.strftime("%Y-%m-%d %H:%M:%S %Z"),
        )

    scheduler.start()
    logger.info(
        "Scheduler started. Jobs: daily_refresh @ %02d:%02d, "
        "pivot_check @ %02d:%02d, isolation_scan every %ds, "
        "disconnect_feed @ %02d:%02d%s",
        DAILY_REFRESH_HOUR, DAILY_REFRESH_MINUTE,
        PIVOT_CHECK_HOUR, PIVOT_CHECK_MINUTE,
        ISOLATION_SCAN_INTERVAL_SECONDS,
        DISCONNECT_HOUR, DISCONNECT_MINUTE,
        (
            f", deferred_startup @ {deferred_until.strftime('%H:%M')}"
            if deferred_until is not None else ""
        ),
    )
    send_telegram_message("✅ Project started. Scheduler running.")
    return scheduler