# scheduler.py
"""Scheduler: daily refresh at 09:00, feed disconnect at 15:45 (Mon–Fri)."""

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core.config import TIMEZONE
from core.logger import get_logger
from core.telegram_notifier import send_telegram_message
from jobs.daily_refresh import run_daily_refresh, disconnect_feed

logger = get_logger(__file__)


def start_scheduler() -> BackgroundScheduler:
    """Start the background scheduler and send a *Project started* message."""
    scheduler = BackgroundScheduler(timezone=TIMEZONE)

    # Daily refresh at 09:00 IST, Monday–Friday
    scheduler.add_job(
        run_daily_refresh,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=0, timezone=TIMEZONE),
        id="daily_refresh",
        name="Daily Refresh",
        replace_existing=True,
    )

    # Disconnect feed at 15:45 IST, Monday–Friday
    scheduler.add_job(
        disconnect_feed,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=45, timezone=TIMEZONE),
        id="disconnect_feed",
        name="Disconnect Feed",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "Scheduler started. Jobs: daily_refresh at 09:00, disconnect_feed at 15:45."
    )
    send_telegram_message("✅ Project started. Scheduler running.")
    return scheduler