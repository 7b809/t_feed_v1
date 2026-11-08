# jobs/isolation_scan.py
"""Isolation scan tick + startup backfill helper."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from core.config import (
    TIMEZONE,
    ISOLATION_SCAN_START_HOUR,
    ISOLATION_SCAN_START_MINUTE,
    ISOLATION_SCAN_END_HOUR,
    ISOLATION_SCAN_END_MINUTE,
)
from core.logger import get_logger
from core.token_service import fetch_access_token
from jobs.daily_refresh import get_feed_service
from services.isolation_service import IsolationService
from services.isolation_backfill_service import IsolationBackfillService

logger = get_logger(__file__)

_isolation_service: IsolationService | None = None
_isolation_token: str | None = None


def get_isolation_service(token: str) -> IsolationService:
    global _isolation_service, _isolation_token
    if _isolation_service is None or _isolation_token != token:
        _isolation_service = IsolationService(token)
        _isolation_token = token
    return _isolation_service


def _within_scan_window(now: datetime) -> bool:
    if now.weekday() > 4:
        return False
    start = now.replace(
        hour=ISOLATION_SCAN_START_HOUR,
        minute=ISOLATION_SCAN_START_MINUTE,
        second=0, microsecond=0,
    )
    end = now.replace(
        hour=ISOLATION_SCAN_END_HOUR,
        minute=ISOLATION_SCAN_END_MINUTE,
        second=0, microsecond=0,
    )
    return start <= now <= end


def run_isolation_scan() -> None:
    """Scheduler tick: every ISOLATION_SCAN_INTERVAL_SECONDS."""
    now = datetime.now(ZoneInfo(TIMEZONE))
    if not _within_scan_window(now):
        return

    feed = get_feed_service()
    if not feed:
        logger.debug("isolation scan skipped: feed not connected")
        return

    try:
        token = fetch_access_token()
    except Exception:
        logger.exception("isolation scan skipped: token fetch failed")
        return

    try:
        service = get_isolation_service(token)
        record = service.scan_once(feed)
        if record:
            logger.info(
                "isolation created (live): %s (mode=%s)",
                record["trading_symbol"],
                record["standard_execution_mode"],
            )
    except Exception:
        logger.exception("isolation scan tick failed")


def run_startup_backfill() -> None:
    """One-shot at startup: reconstruct missed isolation, if any."""
    try:
        token = fetch_access_token()
    except Exception:
        logger.exception("Backfill skipped: token fetch failed")
        return

    try:
        service = get_isolation_service(token)
        backfill = IsolationBackfillService(token, service)
        record = backfill.backfill_if_needed()
        if record:
            logger.warning(
                "STARTUP BACKFILL: isolation reconstructed — %s | %s/%s "
                "| mode=%s | backfilled_at=%s",
                record["trading_symbol"],
                record["trigger_index_canonical"],
                record["trigger_side"],
                record["standard_execution_mode"],
                record.get("backfilled_at"),
            )
        else:
            logger.info("Startup backfill: nothing to reconstruct")
    except Exception:
        logger.exception("Startup backfill failed")