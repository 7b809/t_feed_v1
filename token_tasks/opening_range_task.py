"""Scheduled task wrapper for the Opening Range calculation.

Mirrors the pattern used by ``daily_refresh.py``: logs, sends a
Telegram notification, and never raises so APScheduler does not
disable the job on a transient failure.
"""

from __future__ import annotations

import logging

from services.opening_range_service import opening_range_service
from services.telegram_service import telegram_service

logger = logging.getLogger(__name__)


def run_opening_range_calculation() -> None:
    logger.info("Opening range calculation starting")
    telegram_service.send_message(
        "Opening Range Calc Started",
        "Calculating opening range levels for all instruments.",
        level="INFO",
    )
    try:
        summary = opening_range_service.calculate_all()
        telegram_service.send_message(
            "Opening Range Calc Completed",
            (
                f"Day: {summary.get('day')}\n"
                f"Calculated: {summary.get('calculated')}/{summary.get('total')}\n"
                f"Skipped: {summary.get('skipped')}  "
                f"Errors: {summary.get('errors')}"
            ),
            level="SUCCESS",
        )
        logger.info("Opening range calculation completed: %s", summary)
    except Exception as exc:
        logger.exception("Opening range calculation failed")
        telegram_service.send_message(
            "Opening Range Calc Failed",
            f"{type(exc).__name__}: {exc}",
            level="ERROR",
        )