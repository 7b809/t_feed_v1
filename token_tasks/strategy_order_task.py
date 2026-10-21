# File: token_tasks/strategy_order_task.py
"""Scheduled wrapper for Strategy 1 — Stage 4 (order placement).

Completion-message branching
----------------------------
The service returns one of three top-level ``status`` values on a
non-order run, and each is now surfaced differently on Telegram:

    status = "ok"              -> normal completion message (order
                                  placed or no strike met criteria)
    status = "no_activations"  -> dedicated WARNING message telling the
                                  operator that **Stage 3 produced no
                                  activations today**, with pointers to
                                  the tracker status / start endpoints.
                                  This replaces the old, misleading
                                  "no strike met selection criteria"
                                  message, which hid the real cause
                                  (Stage 3 was not running).
    status = "disabled"        -> dedicated INFO message (one of
                                  STRATEGY_ENABLED / STRATEGY_ORDER_ENABLED
                                  is false).

``no_activations`` is by far the most common non-``ok`` status on a
day where the Stage-3 R2 tracker did not start (e.g. because the
unified refresh was skipped by the market-hours gate), so the message
deliberately points the operator at that exact remediation path.
"""

from __future__ import annotations

import logging

from services.strategy_order_service import strategy_order_service
from services.telegram_service import telegram_service
from services.telegram_format import build

logger = logging.getLogger(__name__)


def _build_no_activations_message(summary: dict) -> str:
    """Dedicated message when Stage 3 produced no activations today.

    Distinguishes "Stage 4 has nothing to pick from" from
    "Stage 4 ran but no strike met criteria" — the two look identical
    in a normal completion, but the root cause and the remediation
    are different.
    """
    return build(
        emoji="⚠️",
        title="Strategy 1 · Stage 4 · No Activations",
        fields=[
            ("Mode", summary.get("mode")),
            ("Catch-up", "yes" if summary.get("catchup") else "no"),
            ("Post-market", "yes" if summary.get("post_market") else "no"),
            ("Scheduled", summary.get("scheduled_time_ist")),
            ("Actual", summary.get("actual_time_ist")),
        ],
        sections=[(
            "Why",
            "🔍",
            [
                "Stage 3 produced 0 R2 activations today.",
                "Did the Stage-3 R2 tracker actually start?",
                "Check: GET /admin/strategy/tracker/status",
                "Fix:   POST /admin/strategy/tracker/start",
                "If the app was launched before 09:00, the "
                "market-hours gate may have skipped the tracker start.",
            ],
        )],
        status="warning",
        status_detail="no stage-3 activations — tracker may not be running",
    )


def _build_disabled_message(summary: dict) -> str:
    """Dedicated message when Stage 4 is disabled by config."""
    return build(
        emoji="🚫",
        title="Strategy 1 · Stage 4 · Disabled",
        fields=[
            ("Mode", summary.get("mode")),
            ("Scheduled", summary.get("scheduled_time_ist")),
            ("Actual", summary.get("actual_time_ist")),
            (
                "Reason",
                "STRATEGY_ENABLED or STRATEGY_ORDER_ENABLED is false",
            ),
        ],
        status="info",
    )


def _build_completion_message(summary: dict) -> str:
    """Build the Telegram message for a non-disabled Stage-4 run.

    Branches on ``summary["status"]``:
      * ``no_activations`` -> dedicated warning (missing Stage 3)
      * ``disabled``       -> dedicated info (config off)
      * anything else      -> normal completion message (order placed
                              or no strike met criteria)
    """
    status = summary.get("status")

    # ---- Branch 1: Stage 3 produced nothing ------------------------- #
    if status == "no_activations":
        return _build_no_activations_message(summary)

    # ---- Branch 2: Stage 4 disabled by config ---------------------- #
    if status == "disabled":
        return _build_disabled_message(summary)

    # ---- Branch 3: normal Stage-4 path ----------------------------- #
    orders = summary.get("orders") or []
    bullets = [
        f"Candidates: {summary.get('candidates')}",
        f"Picks: {summary.get('picks')}",
        f"Placed: {summary.get('placed')}",
    ]
    if orders:
        o = orders[0]
        order_fields = [
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
        order_fields = [
            ("Result", "no strike met selection criteria"),
        ]

    return build(
        emoji="💰",
        title="Strategy 1 · Stage 4 · Completed",
        fields=[
            ("Mode", summary.get("mode")),
            ("Catch-up", "yes" if summary.get("catchup") else "no"),
            ("Post-market", "yes" if summary.get("post_market") else "no"),
            ("Scheduled", summary.get("scheduled_time_ist")),
            ("Actual", summary.get("actual_time_ist")),
        ] + order_fields,
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
        status = str(summary.get("status") or "")

        # Log level mirrors the status the wrapper will surface on
        # Telegram, so a grep of the log files matches what the
        # operator saw on the phone.
        if status == "no_activations":
            log_level = logging.WARNING
        elif status == "disabled":
            log_level = logging.INFO
        elif placed > 0:
            log_level = logging.INFO
        else:
            log_level = logging.WARNING

        telegram_level = (
            "SUCCESS" if placed > 0
            else "WARNING" if status in ("no_activations", "")
            else "INFO"
        )

        telegram_service.send_message(
            _build_completion_message(summary), level=telegram_level
        )
        logger.log(
            log_level,
            "Strategy 1 stage 4 completed: status=%s placed=%d",
            status, placed,
        )
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