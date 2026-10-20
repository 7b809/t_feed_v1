"""Cron watchdogs for the OR and Stage-1 jobs.

If the OR cron silently fails to fire (scheduler died, misfire
exceeded, clock drift), the first symptom an operator sees is a
cascade of confusing Stage-1 / Stage-2 errors 20 seconds later.

These watchdogs run 60 s after each critical cron and alert on
Telegram if the expected output file was not written today.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from core import config
from core.logger import get_logger
from services.telegram_service import telegram_service

logger = get_logger(__file__)


def _today_iso() -> str:
    return datetime.now().astimezone().date().isoformat()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _or_ran_today() -> bool:
    """Check whether today's NIFTY opening_range.json was written."""
    try:
        from services.instrument_registry import instrument_registry
        from services.opening_range_service import _path_for
        for entry in instrument_registry.all():
            if entry.get("instrument_key") == "NSE_INDEX|Nifty 50":
                path = _path_for(entry)
                payload = _read_json(path) if path.exists() else None
                return bool(payload and payload.get("day") == _today_iso())
        return False
    except Exception:
        logger.exception("[watchdog:OR] check failed")
        return False


def _stage1_ran_today() -> bool:
    """Check whether today's Stage-1 summary exists and is fresh."""
    try:
        path = (
            Path(config.STRATEGY_BASE_DIR)
            / config.STRATEGY_STAGE_1_NAME
            / "stage_1_summary.json"
        )
        payload = _read_json(path)
        if not payload:
            return False
        return str(payload.get("day") or "") == _today_iso()
    except Exception:
        logger.exception("[watchdog:Stage1] check failed")
        return False


def run_or_watchdog() -> None:
    """09:17:10 — alert if the OR cron did not produce today's output."""
    if not config.READINESS_WATCHDOG_ENABLED:
        return
    if _or_ran_today():
        logger.info("[watchdog:OR] OK — today's OR was written")
        return

    logger.error("[watchdog:OR] OR did not run for %s", _today_iso())
    try:
        from services.scheduler_service import scheduler_service
        sched = scheduler_service.scheduler
        job_count = len(sched.get_jobs()) if sched else 0
    except Exception:
        job_count = -1

    telegram_service.send_message(
        "OR Watchdog",
        (
            f"❌ OR calculation did not run today ({_today_iso()})\n"
            f"⏰ Expected at "
            f"{config.OPENING_RANGE_HOUR:02d}:"
            f"{config.OPENING_RANGE_MINUTE:02d}:"
            f"{config.OPENING_RANGE_SECOND:02d} IST\n"
            f"🔧 Scheduler jobs: {job_count}\n"
            f"➡️ POST /admin/opening-range/recalculate"
        ),
        level="ERROR",
    )


def run_stage1_watchdog() -> None:
    """09:17:30 — alert if Stage 1 did not run (OR was available)."""
    if not config.READINESS_WATCHDOG_ENABLED:
        return
    if _stage1_ran_today():
        logger.info("[watchdog:Stage1] OK — today's Stage-1 summary written")
        return

    logger.error("[watchdog:Stage1] Stage 1 did not run for %s", _today_iso())
    telegram_service.send_message(
        "Stage 1 Watchdog",
        (
            f"❌ Stage 1 did not produce output today ({_today_iso()})\n"
            f"⏰ Expected 20 s after OR "
            f"({config.OPENING_RANGE_HOUR:02d}:"
            f"{config.OPENING_RANGE_MINUTE:02d}:"
            f"{config.OPENING_RANGE_SECOND:02d} + 20 s)\n"
            f"➡️ POST /admin/strategy/recalculate"
        ),
        level="ERROR",
    )