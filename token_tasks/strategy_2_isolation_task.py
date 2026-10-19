"""Scheduled wrapper for Strategy 2 — Stage 3.

Telegram noise gate
-------------------
The scheduler re-runs this every 5 minutes during 09:15–15:30 IST
(``strategy_2_stage3_recheck``). On a typical day Stage 2 has produced
zero isolated contracts (either because the day is quiet, or because
Stage 1 was skipped due to a late OR candle), so both the
"Started" and "Completed" messages would just say
"Stage 2 contracts: 0 / Touched: 0 / No contracts touched".

To avoid flooding Telegram with zero-data messages, this wrapper
suppresses the Telegram "Started" message when there is nothing to
watch (``stage2_contracts == 0``), and suppresses the "Completed"
message unless one of the following is true:

    * a contract just became the isolated pick (state changed from
      previous run), or
    * a new touch was recorded since the previous run, or
    * the run errored out.

The service itself still runs on every tick — only the Telegram
output is gated. When Stage 2 is empty (``stage2_contracts == 0``)
the wrapper logs a single INFO line and returns early: there is
nothing for Stage 3 to watch, so running the service is wasted work.
"""

from __future__ import annotations

import logging
from typing import Any

from services.strategy_2_isolation_service import strategy_2_isolation_service
from services.telegram_service import telegram_service
from services.telegram_format import build

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-process state used to suppress duplicate "no isolation yet" messages.
# ---------------------------------------------------------------------------
# These persist for the lifetime of the process; a restart resets them,
# which is fine because a restart also triggers a full pipeline refresh
# that repopulates the stage-2 tree.
_LAST_ISOLATED_KEY: str | None = None
_LAST_TOUCHED_COUNT: int = 0

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

def _isolated_key(isolated: dict[str, Any] | None) -> str | None:
    if not isolated:
        return None
    return f"{isolated.get('instrument_key')}"

def _should_send_completion(summary: dict[str, Any]) -> bool:
    """Return True when the completion message is worth sending.

    We only send when:
      * the isolated pick changed since the last run, or
      * a new touch was recorded since the last run.
    """
    global _LAST_ISOLATED_KEY, _LAST_TOUCHED_COUNT

    isolated_key = _isolated_key(summary.get("isolated"))
    touched = int(summary.get("touched_contracts") or 0)

    isolated_changed = isolated_key != _LAST_ISOLATED_KEY
    touched_changed = touched > _LAST_TOUCHED_COUNT

    # Update state regardless of whether we send, so the next run has
    # fresh baselines.
    _LAST_ISOLATED_KEY = isolated_key
    _LAST_TOUCHED_COUNT = touched

    return isolated_changed or touched_changed

def run_strategy_2_isolation() -> None:
    logger.info("Strategy 2 stage 3 starting")

    try:
        # First pass — check whether there is anything to watch. We
        # still run the service on every tick; this pass is only used
        # to decide whether to send the "Started" Telegram message.
        summary = strategy_2_isolation_service.run(reason="scheduled")

        stage2_contracts = int(summary.get("stage2_contracts") or 0)
        touched_contracts = int(summary.get("touched_contracts") or 0)
        isolated = summary.get("isolated")

        # ---- Zero-data path: nothing to watch ------------------------- #
        if stage2_contracts == 0:
            logger.info(
                "Strategy 2 stage 3: no stage-2 contracts "
                "(stage-1/2 produced nothing this run) — "
                "skipping Telegram and returning"
            )
            return

        # ---- Non-zero path: send Started only when a touch or an
        #      isolated pick exists, or when this is the first touch
        #      of the day. ------------------------------------------- #
        should_send_started = (
            isolated is not None
            or touched_contracts > 0
        )

        if should_send_started:
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

        # ---- Completion gate ----------------------------------------- #
        send_completion = _should_send_completion(summary)

        if send_completion:
            level = "SUCCESS" if isolated else "WARNING"
            telegram_service.send_message(
                _build_completion_message(summary), level=level
            )
        else:
            logger.info(
                "Strategy 2 stage 3: no state change "
                "(stage2=%d touched=%d isolated=%s) — Telegram suppressed",
                stage2_contracts,
                touched_contracts,
                "yes" if isolated else "no",
            )

        logger.info(
            "Strategy 2 stage 3 completed: stage2=%d touched=%d isolated=%s",
            stage2_contracts,
            touched_contracts,
            "yes" if isolated else "no",
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