"""Scheduled wrapper for stage 4 (order placement).

Sends start / complete / failure notifications. The completion message
includes the selected strike details and a short candidate list.
"""

from __future__ import annotations

import logging

from services.strategy_order_service import strategy_order_service
from services.telegram_service import telegram_service

logger = logging.getLogger(__name__)


def _build_completion_message(summary: dict) -> str:
    orders = summary.get("orders") or []
    lines = [
        f"Candidates: {summary.get('candidates')}",
        f"Picks: {summary.get('picks')}",
        f"Placed: {summary.get('placed')}",
    ]

    if orders:
        o = orders[0]
        lines.append("")
        lines.append("Selected:")
        lines.append(
            f"  • {o.get('ticker')} {o.get('strike_price')} "
            f"{o.get('strike_type')}  side={o.get('side')} "
            f"qty={o.get('quantity')}"
        )
        lines.append(
            f"    key={o.get('upstox_instrument_key') or o.get('instrument_key')}"
        )
        lines.append(
            f"    entry_ltp={o.get('entry_ltp')}  "
            f"r2={o.get('r2_value')}"
        )
        lines.append(
            f"    index_touched_at={o.get('index_touched_at')}"
        )
    else:
        lines.append("")
        lines.append("No strike met the selection criteria.")

    return "\n".join(lines)


def run_strategy_order() -> None:
    logger.info("Strategy stage 4 starting")
    telegram_service.send_message(
        "Strategy Stage 4 Started",
        "Selecting final strike and placing paper order.",
        level="INFO",
    )
    try:
        summary = strategy_order_service.run(reason="scheduled")
        placed = int(summary.get("placed") or 0)
        level = "SUCCESS" if placed > 0 else "WARNING"
        telegram_service.send_message(
            "Strategy Stage 4 Completed"
            if level == "SUCCESS" else "Strategy Stage 4 Finished",
            _build_completion_message(summary),
            level=level,
        )
        logger.info("Strategy stage 4 completed: %s", summary)
    except Exception as exc:
        logger.exception("Strategy stage 4 failed")
        telegram_service.send_message(
            "Strategy Stage 4 Failed",
            f"{type(exc).__name__}: {exc}",
            level="ERROR",
        )