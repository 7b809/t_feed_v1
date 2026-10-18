"""Scheduled wrapper for Strategy 2 — Stage 3."""

from __future__ import annotations

import logging

from services.strategy_2_isolation_service import strategy_2_isolation_service
from services.telegram_service import telegram_service
from services.telegram_format import build

logger = logging.getLogger(__name__)


def _build_completion_message(summary: dict) -> str:
    isolated = summary.get("isolated")
    bullets: list[str] = [
        f"Stage 2 contracts: {summary.get('stage2_contracts', 0)}",
        f"Touched: {summary.get('touched_contracts', 0)}",
    ]
    if isolated:
        bullets.append(
            f"🔒 Isolated: {isolated.get('ticker')} "
            f"{isolated.get('strike_price')} {isolated.get('strike_type')}"
        )
        bullets.append(
            f"   Level: {isolated.get('isolation_level_name')} = "
            f"{isolated.get('isolation_level')}"
        )
        bullets.append(
            f"   Touch: {isolated.get('touch_minute')} "
            f"(ltp {isolated.get('touch_ltp')})"
        )
    else:
        bullets.append("No contracts touched the isolation level yet.")

    return build(
        emoji="🔒",
        title="Strategy 2 · Stage 3 · Completed",
        fields=[
            ("Level", summary.get("isolation_level_name")),
            ("Mode", summary.get("mode")),
            ("Catch-up", "yes" if summary.get("catchup") else "no"),
            ("Post-market", "yes" if summary.get("post_market") else "no"),
            ("Actual", summary.get("actual_time_ist")),
        ],
        sections=[("Result", "🎯", bullets)],
        status="success" if isolated else "warning",
        status_detail=(
            "isolated pick ready for stage 4"
            if isolated else "no isolation yet"
        ),
    )


def run_strategy_2_isolation() -> None:
    logger.info("Strategy 2 stage 3 starting")
    telegram_service.send_message(
        build(
            emoji="🚀",
            title="Strategy 2 · Stage 3 · Started",
            fields=[("Trigger", "scheduled")],
            sections=[(
                "Doing",
                "🔍",
                [
                    "Read stage-2 isolated contracts",
                    "Compute each contract's own isolation level",
                    "Filter to ONE contract: ticker priority + nearest ATM",
                ],
            )],
            status="started",
        ),
        level="INFO",
    )
    try:
        summary = strategy_2_isolation_service.run(reason="scheduled")
        level = "SUCCESS" if summary.get("isolated") else "WARNING"
        telegram_service.send_message(
            _build_completion_message(summary), level=level
        )
        logger.info(
            "Strategy 2 stage 3 completed: touched=%d isolated=%s",
            summary.get("touched_contracts", 0),
            "yes" if summary.get("isolated") else "no",
        )
    except Exception as exc:
        logger.exception("Strategy 2 stage 3 failed")
        telegram_service.send_message(
            build(
                emoji="❌",
                title="Strategy 2 · Stage 3 · Failed",
                fields=[("Error", f"{type(exc).__name__}: {exc}")],
                status="failed",
            ),
            level="ERROR",
        )