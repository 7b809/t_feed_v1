"""Scheduled wrapper for stage 2 (EMA-cross isolation).

Runs once per trading day a few seconds after the stage-1 selection
job so the first 1-minute candle (09:15 – 09:16 IST) is finalised and
today's intraday is available from Upstox.

Mirrors the pattern used by ``strategy_selection_task.py`` — logs,
sends a Telegram notification, and never raises so APScheduler does
not disable the job on a transient failure.
"""

from __future__ import annotations

import logging

from services.strategy_isolation_service import strategy_isolation_service
from services.telegram_service import telegram_service

logger = logging.getLogger(__name__)


def run_strategy_isolation() -> None:
    logger.info("Strategy isolation starting")
    telegram_service.send_message(
        "Strategy Isolation Started",
        "Applying EMA-cross isolation on stage-1 strikes.",
        level="INFO",
    )
    try:
        summary = strategy_isolation_service.run(reason="scheduled")
        telegram_service.send_message(
            "Strategy Isolation Completed",
            (
                f"Evaluated: {summary.get('evaluated')}\n"
                f"Isolated: {summary.get('isolated')}\n"
                f"Skipped: {summary.get('skipped')}  "
                f"Errors: {summary.get('errors')}"
            ),
            level="SUCCESS",
        )
        logger.info("Strategy isolation completed: %s", summary)
    except Exception as exc:
        logger.exception("Strategy isolation failed")
        telegram_service.send_message(
            "Strategy Isolation Failed",
            f"{type(exc).__name__}: {exc}",
            level="ERROR",
        )