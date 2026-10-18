"""Scheduled wrapper for Strategy 1 — Stage 4 (order placement)."""

from __future__ import annotations

import logging

from services.strategy_order_service import strategy_order_service
from services.telegram_service import telegram_service
from services.telegram_format import build

logger = logging.getLogger(__name__)

def _build_completion_message(summary: dict) -> str:
    orders = summary.get("orders") or []
    bullets = [
        f"Candidates: {summary.get('candidates')}",
        f"Picks: {summary.get('picks')}",
        f"Placed: {summary.get('placed')}",
    ]
    if orders:
        o = orders[0]
        fields = [
            ("Ticker", o.get("ticker")),
            ("Strike", o.get("strike_price")),
            ("Type", o.get("strike_type")),
            ("Side", o.get("side")),
            ("Qty", o.get("quantity")),
            ("Entry LTP", o.get("entry_ltp")),
            ("Entry source", o.get("entry_ltp_source")),
            ("R2", o.get("r2_value")),
            ("Touched at", o.get("index_touched_at")),
            ("Key", o.get("upstox_instrument_key") or o.get("instrument_key")),
        ]
    else:
        fields = [("Result", "no strike met selection criteria")]

    return build(
        emoji="💰",
        title="Strategy 1 · Stage 4 · Completed",
        fields=[
            ("Mode", summary.get("mode")),
            ("Catch-up", "yes" if summary.get("catchup") else "no"),
            ("Post-market", "yes" if summary.get("post_market") else "no"),
            ("Scheduled", summary.get("scheduled_time_ist")),
            ("Actual", summary.get("actual_time_ist")),
        ],
        sections=[
            ("Counters", "🔢", bullets),
            ("Order", "📄", []),
        ] + ([("Selected", "🏆", [])] if orders else []),
        status="success" if int(summary.get("placed") or 0) > 0 else "warning",
        status_detail=f"placed={summary.get('placed')}",
    ) if False else _merged_message(summary, orders, bullets, fields)

def _merged_message(summary, orders, bullets, fields) -> str:
    """Compose the completion message with all key fields in one block."""
    return build(
        emoji="💰",
        title="Strategy 1 · Stage 4 · Completed",
        fields=[
            ("Mode", summary.get("mode")),
            ("Catch-up", "yes" if summary.get("catchup") else "no"),
            ("Post-market", "yes" if summary.get("post_market") else "no"),
            ("Scheduled", summary.get("scheduled_time_ist")),
            ("Actual", summary.get("actual_time_ist")),
        ] + fields,
        sections=[("Counters", "🔢", bullets)],
        status="success" if int(summary.get("placed") or 0) > 0 else "warning",
        status_detail=f"placed={summary.get('placed')}",
    )

def run_strategy_order() -> None:
    logger.info("Strategy 1 stage 4 starting")
    telegram_service.send_message(
        build(
            emoji="💰",
            title="Strategy 1 · Stage 4 · Started",
            sections=[(
                "Doing",
                "🔍",
                [
                    "Read stage-3 activations",
                    "Pick nearest-to-ATM per index",
                    "Apply FIFO by R2 touch",
                    "Place paper order",
                ],
            )],
            status="started",
        ),
        level="INFO",
    )
    try:
        summary = strategy_order_service.run(reason="scheduled")
        placed = int(summary.get("placed") or 0)
        level = "SUCCESS" if placed > 0 else "WARNING"
        telegram_service.send_message(
            _build_completion_message(summary), level=level
        )
        logger.info("Strategy 1 stage 4 completed: placed=%d", placed)
    except Exception as exc:
        logger.exception("Strategy 1 stage 4 failed")
        telegram_service.send_message(
            build(
                emoji="❌",
                title="Strategy 1 · Stage 4 · Failed",
                fields=[("Error", f"{type(exc).__name__}: {exc}")],
                status="failed",
            ),
            level="ERROR",
        )