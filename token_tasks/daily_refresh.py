"""Daily refresh job — runs Mon–Fri at the configured IST time.

Delegates to the unified refresh routine in ``routes.admin_routes``
so the daily refresh executes exactly the same task list as project
startup and manual hard refresh.
"""

from __future__ import annotations

import logging

from core import config
from services.telegram_service import telegram_service

logger = logging.getLogger(__name__)


def run_daily_refresh() -> None:
    # Imported here to avoid any import cycle at module load time.
    from routes.admin_routes import _run_full_refresh_sync

    logger.info(
        "Daily refresh starting (scheduled %02d:%02d %s)",
        config.DAILY_REFRESH_HOUR,
        config.DAILY_REFRESH_MINUTE,
        config.MARKET_TIMEZONE,
    )
    telegram_service.send_message(
        "Daily Refresh Triggered",
        (
            f"Scheduled refresh at {config.DAILY_REFRESH_HOUR:02d}:"
            f"{config.DAILY_REFRESH_MINUTE:02d} {config.MARKET_TIMEZONE}."
        ),
        level="STARTUP",
    )
    try:
        result = _run_full_refresh_sync("daily")
        logger.info("Daily refresh completed: %s", result)
    except Exception as exc:
        logger.exception("Daily refresh failed")
        telegram_service.send_message(
            "Daily Refresh Failed",
            f"{type(exc).__name__}: {exc}",
            level="ERROR",
        )