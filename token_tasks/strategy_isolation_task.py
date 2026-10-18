"""Scheduled wrapper for Strategy 1 — Stage 2."""

from __future__ import annotations

import logging

from services.strategy_isolation_service import strategy_isolation_service
from services.telegram_service import telegram_service
from services.telegram_format import build

logger = logging.getLogger(__name__)

def _build_completion_message(summary: dict) -> str:
    bullets: list[str] = [
        f"Evaluated: {summary.get('evaluated')}",
        f"Isolated (contracts): {summary.get('isolated')}",
        f"Isolated (strike pairs): {summary.get('pairs_isolated')}",
        f"Skipped: {summary.get('skipped')} · Errors: {summary.get('errors')}",
    ]
    for idx in summary.get("indexes") or []:
        ticker = idx.get("ticker") or "?"
        bullets.append(
            f"• {ticker}: {idx.get('isolated')}/{idx.get('evaluated')} "
            f"isolated · {idx.get('pairs_isolated')} pairs · "
            f"{idx.get('errors')} errors"
        )

    return build(
        emoji="🔒",
        title="Strategy 1 · Stage 2 · Completed",
        fields=[
            ("Today", summary.get("today")),
            ("Mode", summary.get("mode")),
            ("Catch-up", "yes" if summary.get("catchup") else "no"),
            ("Post-market", "yes" if summary.get("post_market") else "no"),
            ("Pairing", summary.get("pairing")),
        ],
        sections=[("Breakdown", "📊", bullets)],
        status="success" if int(summary.get("isolated") or 0) > 0 else "warning",
        status_detail=f"{summary.get('isolated')} contracts isolated",
    )

def run_strategy_isolation() -> None:
    logger.info("Strategy 1 stage 2 starting")
    telegram_service.send_message(
        build(
            emoji="🔒",
            title="Strategy 1 · Stage 2 · Started",
            fields=[("Trigger", "scheduled")],
            sections=[(
                "Doing",
                "🔍",
                [
                    "Read stage-1 tree",
                    "Compute today's 09:15 first-candle cross",
                    "Apply bullish-priority isolation rules",
                    "CE/PE pairing: both sides copied if either isolates",
                ],
            )],
            status="started",
        ),
        level="INFO",
    )
    try:
        summary = strategy_isolation_service.run(reason="scheduled")
        isolated = int(summary.get("isolated") or 0)
        level = "SUCCESS" if isolated > 0 else "WARNING"
        telegram_service.send_message(
            _build_completion_message(summary), level=level
        )
        logger.info("Strategy 1 stage 2 completed: isolated=%d", isolated)
    except Exception as exc:
        logger.exception("Strategy 1 stage 2 failed")
        telegram_service.send_message(
            build(
                emoji="❌",
                title="Strategy 1 · Stage 2 · Failed",
                fields=[("Error", f"{type(exc).__name__}: {exc}")],
                status="failed",
            ),
            level="ERROR",
        )