# token_tasks/strategy_isolation_task.py
"""Scheduled wrapper for Strategy 1 — Stage 2.

Post-Stage-2 Stage-3 catch-up (this revision — cron-flow fix)
-----------------------------------------------------------
After the Stage-2 isolation service writes the ``stage_2_isolation``
tree to disk, the wrapper now calls
``strategy_live_tracker_service.run_catchup()`` exactly once.

Why: on the normal cron-only flow (engine launched before 09:00, the
daily refresh at 08:30 gated out, Stage 1 at 09:16:30, Stage 2 at
09:17:00, Stage 4 at 09:18:00), the Stage-3 tracker was started at
07:00 with an empty Stage-2 cache. Nothing in the individual cron
chain restarts the tracker, so Stage 4 at 09:18:00 read an empty
``stage_3_summary.json`` and reported ``no_activations`` / ``placed=0``.

Calling ``run_catchup()`` here:

    1. forces the tracker to reload the Stage-2 contracts this run just
       wrote to disk, and
    2. runs the per-contract R2 scan across the 09:15–09:20 window so
       any R2 touches that happened between 09:15 and 09:17 (i.e.
       before the tracker's cache was refreshed) are still captured.

Combined with the periodic 30-second reload in
``strategy_live_tracker_service._run()``, this guarantees that by the
time Stage 4 fires at 09:18:00 the stage-3 summary reflects every
contract that reached its own R2 during the strategy window.

The catch-up call is wrapped in ``try/except`` so a tracker failure
can never break the Stage-2 cron. Stage 2's own Telegram message and
log line are sent before the catch-up so the operator sees the Stage-2
outcome even if the catch-up then fails.
"""

from __future__ import annotations

import logging

from services.strategy_isolation_service import strategy_isolation_service
from services.telegram_service import telegram_service
from services.telegram_format import build

logger = logging.getLogger(__name__)


def _build_completion_message(summary: dict) -> str:
    bullets: list[str] = [
        f"Evaluated: {summary.get('evaluated')}",
        f"Isolated (contracts): {summary.get('isolated')}",
        f"Isolated (strike pairs): {summary.get('pairs_isolated')}",
        f"Skipped: {summary.get('skipped')} · Errors: {summary.get('errors')}",
    ]
    for idx in summary.get("indexes") or []:
        ticker = idx.get("ticker") or "?"
        bullets.append(
            f"• {ticker}: {idx.get('isolated')}/{idx.get('evaluated')} "
            f"isolated · {idx.get('pairs_isolated')} pairs · "
            f"{idx.get('errors')} errors"
        )

    return build(
        emoji="🔒",
        title="Strategy 1 · Stage 2 · Completed",
        fields=[
            ("Today", summary.get("today")),
            ("Mode", summary.get("mode")),
            ("Catch-up", "yes" if summary.get("catchup") else "no"),
            ("Post-market", "yes" if summary.get("post_market") else "no"),
            ("Pairing", summary.get("pairing")),
        ],
        sections=[("Breakdown", "📊", bullets)],
        status="success" if int(summary.get("isolated") or 0) > 0 else "warning",
        status_detail=f"{summary.get('isolated')} contracts isolated",
    )


def run_strategy_isolation() -> None:
    logger.info("Strategy 1 stage 2 starting")
    telegram_service.send_message(
        build(
            emoji="🔒",
            title="Strategy 1 · Stage 2 · Started",
            fields=[("Trigger", "scheduled")],
            sections=[(
                "Doing",
                "🔍",
                [
                    "Read stage-1 tree",
                    "Compute today's 09:15 first-candle cross",
                    "Apply bullish-priority isolation rules",
                    "CE/PE pairing: both sides copied if either isolates",
                ],
            )],
            status="started",
        ),
        level="INFO",
    )
    try:
        summary = strategy_isolation_service.run(reason="scheduled")
        isolated = int(summary.get("isolated") or 0)
        level = "SUCCESS" if isolated > 0 else "WARNING"
        telegram_service.send_message(
            _build_completion_message(summary), level=level
        )
        logger.info("Strategy 1 stage 2 completed: isolated=%d", isolated)

        # -----------------------------------------------------------------
        # Post-Stage-2 Stage-3 catch-up.
        #
        # The Stage-2 tree was just written to disk. The S1 live tracker
        # was started at process boot (e.g. 07:00) with an empty Stage-2
        # cache and, on the cron-only flow, would not see these new
        # contracts before Stage 4 runs at 09:18:00.
        #
        # Calling ``run_catchup()`` here:
        #   - forces the tracker to reload the freshly-written contracts
        #     (run_catchup unconditionally reloads from disk), and
        #   - runs the per-contract R2 scan across 09:15–09:20 so any
        #     R2 touches that happened between 09:15 and 09:17 are still
        #     recorded before Stage 4 reads stage_3_summary.json.
        #
        # Wrapped in try/except so a tracker error can never break the
        # Stage-2 cron.
        # -----------------------------------------------------------------
        try:
            # Local import — avoids loading the whole tracker module at
            # scheduler startup and keeps the dependency direction clean.
            from services.strategy_live_tracker_service import (
                strategy_live_tracker_service,
            )

            tracker_summary = strategy_live_tracker_service.run_catchup(
                mode="post_stage_2",
                catchup=True,
            )
            logger.info(
                "Strategy 1 stage 3 post-Stage-2 catch-up completed: "
                "status=%s activated_total=%s",
                tracker_summary.get("status"),
                tracker_summary.get("activated_total"),
            )
        except Exception:
            logger.exception(
                "Strategy 1 stage 3 post-Stage-2 catch-up failed "
                "(Stage-2 cron still completed successfully)"
            )
    except Exception as exc:
        logger.exception("Strategy 1 stage 2 failed")
        telegram_service.send_message(
            build(
                emoji="❌",
                title="Strategy 1 · Stage 2 · Failed",
                fields=[("Error", f"{type(exc).__name__}: {exc}")],
                status="failed",
            ),
            level="ERROR",
        )