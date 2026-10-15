"""Scheduled task wrapper for index strategy strike selection.

Runs once per trading day after the Opening Range has been calculated
(default trigger: 09:16:10 IST, matching the OR job). Mirrors the
pattern used by ``opening_range_task.py`` — logs, notifies via
Telegram, and never raises so APScheduler does not disable the job on
a transient failure.

The same service is also called from the unified refresh routine so
startup, daily refresh, and hard refresh always produce a fresh
strategy folder tree.
"""

from __future__ import annotations

import logging

from services.strategy_selection_service import strategy_selection_service
from services.telegram_service import telegram_service

logger = logging.getLogger(__name__)


def run_strategy_selection() -> None:
    logger.info("Strategy selection starting")
    telegram_service.send_message(
        "Strategy Selection Started",
        "Selecting strikes for configured indices.",
        level="INFO",
    )
    try:
        summary = strategy_selection_service.run(reason="scheduled")
        ok = sum(
            1 for i in summary.get("indexes", [])
            if i.get("status") == "ok"
        )
        telegram_service.send_message(
            "Strategy Selection Completed",
            (
                f"Indices processed: {ok}/"
                f"{len(summary.get('indexes', []))}\n"
                f"Avg buffer: {summary.get('avg_buffer_points')}\n"
                f"Mode: {summary.get('buffer_mode')}"
            ),
            level="SUCCESS",
        )
        logger.info("Strategy selection completed: %s", summary)
    except Exception as exc:
        logger.exception("Strategy selection failed")
        telegram_service.send_message(
            "Strategy Selection Failed",
            f"{type(exc).__name__}: {exc}",
            level="ERROR",
        )