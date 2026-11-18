"""Background scheduler for weekday MEGA data backups."""
from __future__ import annotations
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from core.config import (
    APP_NAME, DAILY_DATA_BACKUP_COLLECTION, DAILY_DATA_BACKUP_DAYS,
    DAILY_DATA_BACKUP_DIR, DAILY_DATA_BACKUP_ENABLED, DAILY_DATA_BACKUP_HOUR,
    DAILY_DATA_BACKUP_MINUTE, DATA_DIR, MEGA_BACKUP_ROOT, MEGA_EMAIL,
    MEGA_ENABLED, MEGA_PASSWORD, TIMEZONE,
)
from core.logger import get_logger
from core.mongo_client import get_mongo_db
from core.telegram_notifier import notify_event, send_telegram_message
from services.daily_data_backup_service import DailyDataBackupService

logger = get_logger(__file__)
_scheduler: BackgroundScheduler | None = None
_backup_service: DailyDataBackupService | None = None


def _run_scheduled_backup() -> None:
    if _backup_service is None:
        return
    try:
        result = _backup_service.run_daily_backup(reason="scheduled")
        logger.info("Scheduled MEGA backup finished: %s", result.get("status"))
    except Exception:
        logger.exception("Scheduled MEGA backup raised an unexpected exception")


def start_mega_backup_scheduler() -> BackgroundScheduler | None:
    """Start the backup scheduler without making order-service startup fragile."""
    global _scheduler, _backup_service
    if _scheduler is not None and _scheduler.running:
        return _scheduler
    if not DAILY_DATA_BACKUP_ENABLED or not MEGA_ENABLED:
        logger.info("Daily MEGA backup scheduler disabled by configuration")
        return None
    if not MEGA_EMAIL or not MEGA_PASSWORD:
        logger.warning("MEGA backup enabled but MEGA_EMAIL/MEGA_PASSWORD are not configured")
        notify_event(
            "MEGA backup not configured",
            ["Set MEGA_EMAIL and MEGA_PASSWORD in the private server .env, then restart."],
            "WARNING",
        )
        return None
    try:
        _backup_service = DailyDataBackupService(
            data_dir=str(DATA_DIR),
            staging_dir=DAILY_DATA_BACKUP_DIR,
            app_name=APP_NAME,
            mega_enabled=MEGA_ENABLED,
            mega_email=MEGA_EMAIL,
            mega_password=MEGA_PASSWORD,
            mega_backup_root=MEGA_BACKUP_ROOT,
            telegram_sender=send_telegram_message,
            state_collection=get_mongo_db()[DAILY_DATA_BACKUP_COLLECTION],
        )
        scheduler = BackgroundScheduler(timezone=TIMEZONE)
        scheduler.add_job(
            _run_scheduled_backup,
            CronTrigger(
                day_of_week=DAILY_DATA_BACKUP_DAYS,
                hour=DAILY_DATA_BACKUP_HOUR,
                minute=DAILY_DATA_BACKUP_MINUTE,
                timezone=TIMEZONE,
            ),
            id="daily_sandbox_mega_backup",
            name="Daily Sandbox MEGA Backup",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        scheduler.start()
        _scheduler = scheduler
        logger.info(
            "Daily sandbox MEGA backup scheduled @ %02d:%02d IST (%s) -> %s/",
            DAILY_DATA_BACKUP_HOUR, DAILY_DATA_BACKUP_MINUTE,
            DAILY_DATA_BACKUP_DAYS, MEGA_BACKUP_ROOT,
        )
        notify_event(
            "MEGA daily backup scheduler started",
            [
                f"Schedule: {DAILY_DATA_BACKUP_DAYS} at {DAILY_DATA_BACKUP_HOUR:02d}:{DAILY_DATA_BACKUP_MINUTE:02d} IST",
                f"Source: {DATA_DIR}",
                f"Destination: {MEGA_BACKUP_ROOT}/",
            ],
            "STARTUP",
        )
        return _scheduler
    except Exception:
        logger.exception("MEGA backup scheduler could not initialise; application will continue")
        notify_event("MEGA backup scheduler initialisation failed", ["Check sandbox service logs."], "ERROR")
        _backup_service = None
        return None


def stop_mega_backup_scheduler() -> None:
    """Stop the scheduler during FastAPI shutdown."""
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            logger.exception("Could not stop MEGA backup scheduler cleanly")
        _scheduler = None


def get_mega_backup_scheduler() -> BackgroundScheduler | None:
    return _scheduler


def get_backup_service() -> DailyDataBackupService | None:
    return _backup_service
