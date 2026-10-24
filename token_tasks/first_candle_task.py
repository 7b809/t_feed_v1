# File: token_tasks/first_candle_task.py
"""Scheduled 09:16:10 task — compute OR levels + detect EMA crosses.

Runs Mon–Fri after the first 1-min candle closes.

On non-trading days, the service returns a ``skipped=True`` summary
and this wrapper sends a distinct "SKIPPED · Next open in ..." message
instead of the usual completion summary.
"""

from __future__ import annotations

from core.logger import get_logger
from services.first_candle_service import first_candle_service
from services.telegram_service import telegram_service

logger = get_logger(__file__)

_DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
_THIN = "────────────────────────────────"


def _build_skip_message(summary: dict) -> str:
    return (
        f"{_DIV}\n"
        f"⏸️ FIRST CANDLE · SKIPPED\n"
        f"{_DIV}\n"
        f"🎯 Reason:      {summary.get('skip_message') or summary.get('skip_reason')}\n"
        f"⏭️ Next open:   {summary.get('countdown_label') or '—'}\n"
        f"⏱️ Countdown:   {summary.get('countdown_str') or '—'}\n"
        f"{_THIN}\n"
        f"OR levels + EMA cross will be computed automatically\n"
        f"at the next market session's 09:16:10 IST."
    )


def _build_success_message(summary: dict) -> str:
    return (
        f"{_DIV}\n"
        f"🏁 09:16 OR LEVELS COMPLETE\n"
        f"{_DIV}\n"
        f"📊 Total:       {summary.get('total')}\n"
        f"✅ Computed:    {summary.get('computed')}\n"
        f"⏭️ Skipped:     {summary.get('instrument_skipped', summary.get('skipped', 0))}\n"
        f"❌ Errors:      {summary.get('errors')}\n"
        f"📈 Crosses:     {summary.get('crosses_detected')}\n"
        f"⏱️ Elapsed:     {summary.get('elapsed_seconds')}s"
    )


def run_first_candle_calculation() -> None:
    logger.info("[first-candle-task] starting")
    try:
        summary = first_candle_service.run(reason="scheduled")

        if summary.get("skipped"):
            telegram_service.send_message(
                "First Candle · Skipped",
                _build_skip_message(summary),
                level="INFO",
            )
            return

        level = "INFO" if summary.get("errors", 0) == 0 else "WARNING"
        telegram_service.send_message(
            "First Candle · OR Levels",
            _build_success_message(summary),
            level=level,
        )
    except Exception as exc:
        logger.exception("[first-candle-task] failed")
        telegram_service.send_message(
            "First Candle Calculation Failed",
            f"{type(exc).__name__}: {exc}",
            level="ERROR",
        )