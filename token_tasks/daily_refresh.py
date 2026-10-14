"""Daily refresh job — runs Mon–Fri at the configured IST time.

Does exactly the same work as the /admin/hard-refresh endpoint:
download latest backup, reload the registry, reset live_crosses
state, and restart the live feed.
"""

from __future__ import annotations

import logging

from core import config
from services.telegram_service import telegram_service

logger = logging.getLogger(__name__)


def run_daily_refresh() -> None:
    # Imported here so the module can be imported before the app is
    # fully wired (e.g. in tests) without triggering live services.
    from routes.admin_routes import _run_hard_refresh_sync

    logger.info("Daily refresh starting (scheduled %02d:%02d %s)", config.DAILY_REFRESH_HOUR, config.DAILY_REFRESH_MINUTE, config.MARKET_TIMEZONE)
    telegram_service.send_message(
        "Daily Refresh Started",
        f"Scheduled refresh at {config.DAILY_REFRESH_HOUR:02d}:{config.DAILY_REFRESH_MINUTE:02d} {config.MARKET_TIMEZONE}.",
        level="STARTUP",
    )
    try:
        result = _run_hard_refresh_sync()
        logger.info("Daily refresh completed: %s", result)
    except Exception as exc:
        logger.exception("Daily refresh failed")
        telegram_service.send_message(
            "Daily Refresh Failed",
            f"{type(exc).__name__}: {exc}",
            level="ERROR",
        )