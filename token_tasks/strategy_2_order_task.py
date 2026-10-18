"""Scheduled wrapper for Strategy 2 — Stage 4."""

from __future__ import annotations

import logging

from services.strategy_2_order_service import strategy_2_order_service
from services.telegram_service import telegram_service
from services.telegram_format import build

logger = logging.getLogger(__name__)


def _build_completion_message(summary: dict) -> str:
    status = summary.get("status")
    order = summary.get("order")
    isolated = summary.get("isolated")
    cross = summary.get("cross")

    bullets: list[str] = []
    if status == "no_isolated":
        bullets.append("No isolated contract from Stage 3 yet.")
    elif status == "no_cross_after_touch":
        bullets.append("Waiting for a fresh EMA cross after the S3 touch.")
    elif status == "disabled":
        bullets.append("Strategy 2 order stage is disabled.")
    else:
        bullets.append("Order placed (paper).")

    fields = [
        ("Mode", summary.get("mode")),
        ("Catch-up", "yes" if summary.get("catchup") else "no"),
        ("Post-market", "yes" if summary.get("post_market") else "no"),
    ]
    if isolated:
        fields += [
            ("Ticker", isolated.get("ticker")),
            ("Original type", isolated.get("strike_type")),
            ("Strike", isolated.get("strike_price")),
            ("S3 touch", isolated.get("touch_minute")),
        ]
    if cross:
        fields += [
            ("Cross type", cross.get("type")),
            ("Cross at", cross.get("minute")),
        ]
    if order:
        fields += [
            ("Order type", order.get("order_strike_type")),
            ("Side", f"{order.get('side')} × {order.get('quantity')}"),
            ("Entry LTP", f"{order.get('entry_ltp')} ({order.get('entry_ltp_source')})"),
            ("Key", order.get("order_instrument_key")),
        ]

    return build(
        emoji="💰",
        title="Strategy 2 · Stage 4 · Completed",
        fields=fields,
        sections=[("Result", "🎯", bullets)],
        status="success" if order else "warning",
        status_detail=f"placed={summary.get('placed', 0)}",
    )


def run_strategy_2_order() -> None:
    logger.info("Strategy 2 stage 4 starting")
    telegram_service.send_message(
        build(
            emoji="🚀",
            title="Strategy 2 · Stage 4 · Started",
            sections=[(
                "Doing",
                "🔍",
                [
                    "Read stage-3 isolated contract",
                    "Find first EMA cross after S3 touch",
                    "Bullish -> same type · Bearish -> opposite type",
                    "Place paper order at cross price",
                ],
            )],
            status="started",
        ),
        level="INFO",
    )
    try:
        summary = strategy_2_order_service.run(reason="scheduled")
        level = "SUCCESS" if summary.get("placed") else "WARNING"
        telegram_service.send_message(
            _build_completion_message(summary), level=level
        )
        logger.info(
            "Strategy 2 stage 4 completed: placed=%d status=%s",
            summary.get("placed", 0),
            summary.get("status"),
        )
    except Exception as exc:
        logger.exception("Strategy 2 stage 4 failed")
        telegram_service.send_message(
            build(
                emoji="❌",
                title="Strategy 2 · Stage 4 · Failed",
                fields=[("Error", f"{type(exc).__name__}: {exc}")],
                status="failed",
            ),
            level="ERROR",
        )