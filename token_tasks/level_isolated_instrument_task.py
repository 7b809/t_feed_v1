# token_tasks/level_isolated_instrument_task.py
"""Scheduled wrapper + manual trigger for the level-isolation engine.

Used by:
  * scheduler_service — periodic recheck job (every 5 min during market)
  * routes/isolated_instrument_routes.py — manual backfill trigger
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
    """Called by the scheduler every 5 minutes during market hours.

    Idempotent: if an instrument is already isolated for the day, this
    is a no-op. Otherwise it replays intraday candles from any missed
    touch that the live feed did not observe (e.g. a mid-session restart
    whose backfill window started after the touch happened but whose
    live feed only started a few minutes ago).
    """
    logger.info("[level-isolation-task] recheck start reason=%s", reason)
    try:
        result = level_isolated_instrument_service.run_backfill(reason=reason)
        status = result.get("status")
        logger.info("[level-isolation-task] recheck result status=%s", status)
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