# token_tasks/strategy_tracker_catchup_task.py
"""Scheduled wrapper — Strategy 1 Stage-3 tracker catch-up.

Runs 30 seconds before the Stage-4 order cron (default 09:17:30, i.e.
``STRATEGY_ORDER_HOUR:STRATEGY_ORDER_MINUTE - 30 seconds``) and calls
``strategy_live_tracker_service.run_catchup()`` so the S1 tracker:

    1. reloads the freshly-written Stage-2 contracts from disk, and
    2. runs the per-contract R2 scan across the 09:15–09:20 window,
       capturing any R2 touches the tracker missed before its in-memory
       cache was refreshed.

This is a **belt-and-braces safety net** on top of the post-Stage-2
hook inside ``token_tasks/strategy_isolation_task.py``. It protects
against:

    * an unusually slow Stage 2 that finishes past its isolation cron
      time, or
    * any future refactor that breaks the post-Stage-2 hook.

The job is idempotent: ``run_catchup()`` reloads from disk and re-scans
the same window every time; contracts that were already activated keep
their existing ``r2_activation.json`` record (the ``already_activated``
branch in ``_decide_for_contract_own``).

Never raises — a failure here must not disable the cron job. A failure
is logged at ERROR level and (best-effort) surfaced on Telegram so the
operator can see the tracker is not doing its job before Stage 4 fires.
"""

from __future__ import annotations

import logging

from services.strategy_live_tracker_service import (
    strategy_live_tracker_service,
)
from services.telegram_format import build
from services.telegram_service import telegram_service

logger = logging.getLogger(__name__)


def run_strategy_tracker_catchup() -> None:
    logger.info("Strategy 1 stage 3 tracker catch-up starting")
    try:
        summary = strategy_live_tracker_service.run_catchup(
            mode="scheduled_catchup",
            catchup=True,
        )
        activated = int(summary.get("activated_total") or 0)
        status = str(summary.get("status") or "unknown")

        # Only send a Telegram message when something actually happened.
        # On a normal day the post-Stage-2 hook in
        # ``strategy_isolation_task`` will already have captured every
        # touch, so this job's catch-up pass typically returns
        # ``activated_total == 0``. Suppress the "nothing to report"
        # case to avoid Telegram noise — the operator sees the real
        # Stage-3 completion message from the post-Stage-2 hook.
        if status == "ok" and activated > 0:
            telegram_service.send_message(
                build(
                    emoji="🎯",
                    title="Strategy 1 · Stage 3 Catch-up",
                    fields=[
                        ("Mode", "scheduled_catchup"),
                        ("Activated", activated),
                        ("Day", summary.get("day")),
                        (
                            "Window",
                            f"{summary.get('window_start')} – "
                            f"{summary.get('window_end')}",
                        ),
                    ],
                    sections=[(
                        "Result",
                        "📊",
                        [
                            f"{activated} additional contract(s) activated "
                            f"by the catch-up cron before Stage 4 order "
                            f"placement.",
                        ],
                    )],
                    status="success",
                    status_detail=f"activated={activated}",
                ),
                level="SUCCESS",
            )

        logger.info(
            "Strategy 1 stage 3 tracker catch-up completed: "
            "status=%s activated_total=%s",
            status,
            activated,
        )
    except Exception as exc:
        logger.exception(
            "Strategy 1 stage 3 tracker catch-up failed "
            "(Stage-4 order cron will still run)"
        )
        # Best-effort alert — the operator needs to know the safety net
        # itself failed, because Stage 4 will then read whatever the
        # tracker last wrote (possibly an empty summary).
        try:
            telegram_service.send_message(
                build(
                    emoji="❌",
                    title="Strategy 1 · Stage 3 Catch-up · Failed",
                    fields=[("Error", f"{type(exc).__name__}: {exc}")],
                    status="failed",
                ),
                level="ERROR",
            )
        except Exception:
            # Never let a Telegram failure mask the original error.
            pass