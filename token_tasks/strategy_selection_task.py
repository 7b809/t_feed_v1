"""Scheduled wrapper for stage 1 (buffer filter + last-day-cross).

Sends a Telegram notification at start, on completion (with per-index
breakdown), and on any exception. Never raises so APScheduler does
not disable the job.
"""

from __future__ import annotations

import logging

from services.strategy_selection_service import strategy_selection_service
from services.telegram_service import telegram_service

logger = logging.getLogger(__name__)


def _build_completion_message(summary: dict) -> str:
    indexes = summary.get("indexes") or []
    ok = sum(1 for i in indexes if i.get("status") == "ok")
    skipped = sum(1 for i in indexes if i.get("status") == "skipped")
    errors = sum(1 for i in indexes if i.get("status") == "error")

    total_strikes = sum(int(i.get("strike_count") or 0) for i in indexes)
    total_contracts = sum(int(i.get("contract_count") or 0) for i in indexes)
    total_resolved = sum(int(i.get("resolved_count") or 0) for i in indexes)

    lines = [
        f"Indexes OK: {ok}/{len(indexes)}  "
        f"skipped: {skipped}  errors: {errors}",
        f"Total strikes: {total_strikes}",
        f"Total contracts: {total_contracts}  "
        f"resolved: {total_resolved}",
        f"Avg buffer: {summary.get('avg_buffer_points')}  "
        f"mode: {summary.get('buffer_mode')}",
    ]

    # Per-index breakdown (short form).
    for i in indexes:
        ticker = i.get("ticker") or "?"
        status = i.get("status") or "?"
        if status == "ok":
            lines.append(
                f"  • {ticker}: {i.get('strike_count')} strikes, "
                f"{i.get('contract_count')} contracts, "
                f"{i.get('resolved_count')} resolved"
            )
        else:
            lines.append(
                f"  • {ticker}: {status} "
                f"({i.get('reason') or i.get('error') or '—'})"
            )
    return "\n".join(lines)


def run_strategy_selection() -> None:
    logger.info("Strategy stage 1 starting")
    telegram_service.send_message(
        "Strategy Stage 1 Started",
        "Buffer filter + history fetch + last-day-cross analysis.",
        level="INFO",
    )
    try:
        summary = strategy_selection_service.run(reason="scheduled")
        ok = sum(
            1 for i in summary.get("indexes", [])
            if i.get("status") == "ok"
        )
        total = len(summary.get("indexes", []))
        level = "SUCCESS" if ok > 0 else "WARNING"
        telegram_service.send_message(
            "Strategy Stage 1 Completed"
            if level == "SUCCESS" else "Strategy Stage 1 Finished",
            _build_completion_message(summary),
            level=level,
        )
        logger.info("Strategy stage 1 completed: %s", summary)
    except Exception as exc:
        logger.exception("Strategy stage 1 failed")
        telegram_service.send_message(
            "Strategy Stage 1 Failed",
            f"{type(exc).__name__}: {exc}",
            level="ERROR",
        )