"""Scheduled wrapper for Strategy 1 — Stage 1.

Nicely formatted Telegram messages with icons, dividers, sections.
"""

from __future__ import annotations

import logging

from services.strategy_selection_service import strategy_selection_service
from services.telegram_service import telegram_service
from services.telegram_format import build

logger = logging.getLogger(__name__)

def _build_completion_message(summary: dict) -> str:
    indexes = summary.get("indexes") or []
    ok = sum(1 for i in indexes if i.get("status") == "ok")
    skipped = sum(1 for i in indexes if i.get("status") == "skipped")
    errors = sum(1 for i in indexes if i.get("status") == "error")
    total_strikes = sum(int(i.get("strike_count") or 0) for i in indexes)
    total_contracts = sum(int(i.get("contract_count") or 0) for i in indexes)
    total_resolved = sum(int(i.get("resolved_count") or 0) for i in indexes)

    index_bullets: list[str] = []
    for i in indexes:
        ticker = i.get("ticker") or "?"
        status = i.get("status") or "?"
        if status == "ok":
            index_bullets.append(
                f"✅ {ticker}: {i.get('strike_count')} strikes · "
                f"{i.get('contract_count')} contracts · "
                f"{i.get('resolved_count')} resolved"
            )
        else:
            reason = i.get("reason") or i.get("error") or "—"
            emoji = "⚠️" if status == "skipped" else "❌"
            index_bullets.append(f"{emoji} {ticker}: {status} ({reason})")

    return build(
        emoji="📊",
        title="Strategy 1 · Stage 1 · Completed",
        fields=[
            ("Mode", summary.get("mode")),
            ("Catch-up", "yes" if summary.get("catchup") else "no"),
            ("Post-market", "yes" if summary.get("post_market") else "no"),
            ("Scheduled", summary.get("scheduled_time_ist")),
            ("Actual", summary.get("actual_time_ist")),
            ("Avg buffer", summary.get("avg_buffer_points")),
            ("Buffer mode", summary.get("buffer_mode")),
        ],
        sections=[
            (
                "Index results",
                "🎯",
                [
                    f"Total: {len(indexes)} · ✅ {ok} · ⚠️ {skipped} · ❌ {errors}",
                    *index_bullets,
                ],
            ),
            (
                "Aggregate",
                "📈",
                [
                    f"Strikes: {total_strikes}",
                    f"Contracts: {total_contracts}",
                    f"Resolved: {total_resolved}",
                ],
            ),
        ],
        status="success" if ok > 0 else "warning",
        status_detail=f"{ok}/{len(indexes)} indexes ok",
    )

def run_strategy_selection() -> None:
    logger.info("Strategy 1 stage 1 starting")
    telegram_service.send_message(
        build(
            emoji="🚀",
            title="Strategy 1 · Stage 1 · Started",
            fields=[
                ("Buffer mode", "typical"),
                ("Trigger", "scheduled"),
            ],
            sections=[(
                "Doing",
                "🔍",
                [
                    "Buffer filter on OR midpoint",
                    "Upstox history fetch (30 days)",
                    "Last-day-cross tail analysis",
                ],
            )],
            status="started",
        ),
        level="INFO",
    )
    try:
        summary = strategy_selection_service.run(reason="scheduled")
        ok = sum(1 for i in summary.get("indexes", []) if i.get("status") == "ok")
        level = "SUCCESS" if ok > 0 else "WARNING"
        telegram_service.send_message(
            _build_completion_message(summary), level=level
        )
        logger.info("Strategy 1 stage 1 completed: ok=%d", ok)
    except Exception as exc:
        logger.exception("Strategy 1 stage 1 failed")
        telegram_service.send_message(
            build(
                emoji="❌",
                title="Strategy 1 · Stage 1 · Failed",
                fields=[("Error", f"{type(exc).__name__}: {exc}")],
                status="failed",
            ),
            level="ERROR",
        )