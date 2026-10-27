# token_tasks/level_isolated_instrument_task.py
"""Scheduled wrapper + manual trigger for the level-isolation engine.

Used by:
  * scheduler_service — periodic recheck job (every 5 min during market)
  * routes/isolated_instrument_routes.py — manual backfill trigger

After every recheck (scheduled or manual), ``_try_promote()`` asks the
strategy-entry engine whether the current ARMED winner differs from the
official isolated instrument, and if so applies the promotion. This is
the 5-minute safety net so promotions land even when the browser is
closed.

Logging
-------
The recheck result line now includes the ``reason`` / ``skip_reason``
returned by the isolation service so operators can distinguish
``missing_inputs`` (self-heal territory) from ``outside_window`` (the
scheduled job fired before 09:16) from ``no_merged_data`` (the intraday
API returned no candles).

Self-heal
---------
``run_level_isolation_recheck`` delegates to
``level_isolated_instrument_service.run_backfill`` which (since the
2026-10-27 fix) calls ``_maybe_self_heal()`` internally when the
first_candle / level_instruments files are missing.  This task does
not need to do anything special for self-heal — the service handles
it before acquiring its lock.
"""

from __future__ import annotations

from core import config
from core.logger import get_logger
from services.isolated_instrument_promotion_service import (
    isolated_instrument_promotion_service,
)
from services.level_isolated_instrument_service import (
    level_isolated_instrument_service,
)
from services.strategy_entry_signal_service import (
    strategy_entry_signal_service,
)
from services.telegram_service import telegram_service

logger = get_logger(__file__)

_DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
_THIN = "────────────────────────────────"

def _try_promote() -> None:
    """Promote the entry-signal winner to be the active instrument.

    Idempotent per day when STRATEGY_ENTRY_ALLOW_RE_PROMOTION is False.
    Silently no-ops when:
      * strategy entry is disabled,
      * promotion is disabled,
      * no isolated instrument exists,
      * no side has ARMED yet,
      * a promotion has already been applied today (default).
    """
    if not config.STRATEGY_ENTRY_ENABLED:
        return
    if not config.STRATEGY_ENTRY_ALLOW_PROMOTION:
        return
    try:
        signal = strategy_entry_signal_service.compute()
        result = isolated_instrument_promotion_service.apply_promotion(signal)
        if result.get("promoted"):
            logger.info(
                "[level-isolation-task] promotion applied "
                "from=%s to=%s reason=%s",
                (result.get("promotion") or {}).get("from_instrument_key"),
                (result.get("promotion") or {}).get("to_instrument_key"),
                (result.get("promotion") or {}).get("reason"),
            )
    except Exception:
        logger.exception("[level-isolation-task] promotion step failed")

def run_level_isolation_recheck(reason: str = "scheduled") -> None:
    """Called by the scheduler every 5 minutes during market hours."""
    logger.info("[level-isolation-task] recheck start reason=%s", reason)
    try:
        result = level_isolated_instrument_service.run_backfill(reason=reason)
        status = result.get("status")
        # Surface the reason so ``missing_inputs`` / ``outside_window`` /
        # ``no_merged_data`` are distinguishable in the log stream.
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
        return

    # Promote the entry-signal winner (5-min safety net, in case the
    # browser is closed).
    _try_promote()

def run_level_isolation_backfill_manual() -> dict:
    """Manual backfill from the admin route. Never raises."""
    try:
        result = level_isolated_instrument_service.run_backfill(reason="manual")
        # Also run promotion on the manual path.
        _try_promote()
        return result
    except Exception as exc:
        logger.exception("[level-isolation-task] manual backfill failed")
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }