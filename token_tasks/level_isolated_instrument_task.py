# token_tasks/level_isolated_instrument_task.py
"""Scheduled wrapper + manual trigger for the level-isolation engine.

Used by:
  * scheduler_service — periodic recheck job (every 5 min during market)
  * routes/isolated_instrument_routes.py — manual backfill trigger

Post-G1 v4.1 dummy-order rewrite
--------------------------------
The old ``_try_promote()`` orchestration (which called
``strategy_entry_signal_service.compute()`` and
``isolated_instrument_promotion_service.apply_promotion()``) has been
removed from this task.

Under the new ``t_feed_v1-market-engine-v3`` order strategy, no
"promotion" ever occurs — the isolated bias-side instrument stays the
same for the whole session, and the dummy-order service
(``services/dummy_order_service.py``) independently resolves either the
same instrument (support / reversal routing) or its opposite leg
(resistance routing) based on the first post-isolation 15/3 SR
confirmation.

Consequently this task now only drives the *isolation* layer:

  * ``level_isolated_instrument_service.run_backfill`` — self-heal OR
    files, verify intraday H/L, replay the day if not already isolated.
  * Telegram alert on unexpected recheck failure.

The dummy-order service runs on its own scheduler tick
(``dummy_order_tracker``) at ``DUMMY_ORDER_TRACK_INTERVAL_SECONDS``.
"""

from __future__ import annotations

from core.logger import get_logger
from services.level_isolated_instrument_service import (
    level_isolated_instrument_service,
)
from services.telegram_service import telegram_service

logger = get_logger(__file__)

_DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
_THIN = "────────────────────────────────"

def run_level_isolation_recheck(reason: str = "scheduled") -> None:
    """Called by the scheduler every 5 minutes during market hours."""
    logger.info("[level-isolation-task] recheck start reason=%s", reason)
    try:
        result = level_isolated_instrument_service.run_backfill(reason=reason)
        status = result.get("status")
        reason_val = (
            result.get("reason")
            or result.get("skip_reason")
            or "—"
        )
        logger.info(
            "[level-isolation-task] recheck result status=%s reason=%s",
            status, reason_val,
        )
    except Exception as exc:
        logger.exception("[level-isolation-task] recheck failed")
        try:
            telegram_service.send_message(
                "Level Isolation · Recheck Failed",
                f"{_DIV}\n❌ RECHECK FAILED\n{_DIV}\n"
                f"Error: {type(exc).__name__}: {exc}",
                level="ERROR",
            )
        except Exception:
            pass

def run_level_isolation_backfill_manual() -> dict:
    """Manual backfill from the admin route. Never raises."""
    try:
        return level_isolated_instrument_service.run_backfill(reason="manual")
    except Exception as exc:
        logger.exception("[level-isolation-task] manual backfill failed")
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }