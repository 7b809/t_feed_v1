# File: token_tasks/strategy1_cutoff_task.py
"""Scheduled 11:00 IST task — Strategy 1 cutoff.

If Strategy 1 has not fired an order by 11:00, activate Strategy 2 for
the rest of the day and alert the user via Telegram.

Two suppression cases:

  1. Strategy 1 already fired -> no-op (S1 wins the day).
  2. Late-start skip rule set -> no-op (S2 was already active since
     process start; the "S2 now active" message would be misleading).

Only when neither applies does this task send the "window closed /
Strategy 2 activated" Telegram message.
"""

from __future__ import annotations

from core.logger import get_logger
from services.strategy_state_service import strategy_state_service
from services.strategy2_service import strategy2_service
from services.telegram_service import telegram_service

logger = get_logger(__file__)

_DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
_THIN = "────────────────────────────────"


def run_strategy1_cutoff() -> None:
    logger.info("[strategy1-cutoff] running")

    # Step 1: advance the cutoff state machine.
    try:
        state = strategy_state_service.on_strategy1_cutoff_reached()
    except Exception:
        logger.exception("[strategy1-cutoff] state update failed")
        return

    # Step 2: suppress misleading messages.

    # (a) Strategy 1 fired already — S1 wins, S2 stays off.
    if state.get("strategy1_fired"):
        logger.info(
            "[strategy1-cutoff] Strategy 1 already fired at %s — no action",
            state.get("strategy1_fired_at"),
        )
        return

    # (b) Late-start skip — S2 was activated at process start; the
    # "S2 now active" message below would be wrong.
    if state.get("strategy1_skip_reason") == "late_start":
        logger.info(
            "[strategy1-cutoff] S1 disabled by late-start rule — "
            "S2 already active since %s; cutoff message suppressed",
            state.get("strategy2_activated_at"),
        )
        return

    # Step 3: normal path — S2 just became active; replay existing
    # touches so pairs from earlier in the day are not missed.
    try:
        strategy2_service.rescan_existing_touches()
    except Exception:
        logger.exception("[strategy1-cutoff] strategy2 rescan failed")

    msg = (
        f"{_DIV}\n"
        f"⏰ STRATEGY 1 · WINDOW CLOSED\n"
        f"{_DIV}\n"
        f"🕒 Cutoff:      {state.get('computed_at') or '11:00 IST'}\n"
        f"📊 S1 eligible: {state.get('strategy1_eligible')}\n"
        f"✅ S1 fired:    {state.get('strategy1_fired')}\n"
        f"{_THIN}\n"
        f"Strategy 1 did NOT place an order in the 09:16–11:00 window.\n"
        f"Strategy 2 is now ACTIVE — watching R3/S3 pairs on all\n"
        f"NIFTY and SENSEX instruments until 15:25 IST.\n"
        f"{_DIV}\n"
        f"🔒 Strategy 2 active"
    )
    try:
        telegram_service.send_message(
            "Strategy 1 · Cutoff Reached",
            msg,
            level="INFO",
        )
    except Exception:
        logger.exception("[strategy1-cutoff] telegram send failed")