"""Scheduled wrapper for stage 2 (EMA-cross isolation).

Sends start / complete / failure notifications. The completion message
includes per-index breakdown plus the paired-count line.
"""

from __future__ import annotations

import logging

from services.strategy_isolation_service import strategy_isolation_service
from services.telegram_service import telegram_service

logger = logging.getLogger(__name__)


def _build_completion_message(summary: dict) -> str:
    lines = [
        f"Today: {summary.get('today')}",
        f"Evaluated: {summary.get('evaluated')}",
        f"Isolated (contracts): {summary.get('isolated')}",
        f"Isolated (strike pairs): {summary.get('pairs_isolated')}",
        f"Skipped: {summary.get('skipped')}  "
        f"Errors: {summary.get('errors')}",
    ]

    for idx in summary.get("indexes") or []:
        ticker = idx.get("ticker") or "?"
        lines.append(
            f"  • {ticker}: isolated {idx.get('isolated')}/"
            f"{idx.get('evaluated')}  "
            f"pairs {idx.get('pairs_isolated')}  "
            f"errors {idx.get('errors')}"
        )
    return "\n".join(lines)


def run_strategy_isolation() -> None:
    logger.info("Strategy stage 2 starting")
    telegram_service.send_message(
        "Strategy Stage 2 Started",
        "Applying EMA-cross isolation on stage-1 strikes.",
        level="INFO",
    )
    try:
        summary = strategy_isolation_service.run(reason="scheduled")
        isolated = int(summary.get("isolated") or 0)
        level = "SUCCESS" if isolated > 0 else "WARNING"
        telegram_service.send_message(
            "Strategy Stage 2 Completed"
            if level == "SUCCESS" else "Strategy Stage 2 Finished",
            _build_completion_message(summary),
            level=level,
        )
        logger.info("Strategy stage 2 completed: %s", summary)
    except Exception as exc:
        logger.exception("Strategy stage 2 failed")
        telegram_service.send_message(
            "Strategy Stage 2 Failed",
            f"{type(exc).__name__}: {exc}",
            level="ERROR",
        )