# jobs/deferred_startup.py
"""Deferred startup sequence.

Fired by a scheduler ``DateTrigger`` one-shot at the configured cutoff
(`DEFERRED_STARTUP_HOUR:DEFERRED_STARTUP_MINUTE`) when the process
started before that time **and** ``TEST_FLAG=false``.

Runs the same sequence ``main.lifespan`` runs at normal startup, in
order:

    1. run_daily_refresh()    → token, feed, OR levels
    2. run_startup_backfill() → reconstruct any missed isolation

Both calls are idempotent:

  * ``run_daily_refresh`` has a per-day guard.
  * ``run_startup_backfill`` is a no-op when today's isolation exists.

This means the recurring ``daily_refresh @ 09:00`` cron firing at the
same instant as the deferred kickoff cannot cause a double-run.
"""

from __future__ import annotations

from core.logger import get_logger
from jobs.daily_refresh import run_daily_refresh
from jobs.isolation_scan import run_startup_backfill

logger = get_logger(__file__)


def run_deferred_startup_sequence() -> None:
    """Run the deferred startup sequence (called once at cutoff)."""
    logger.warning(
        "[STARTUP-GATE] Deferred startup cutoff reached — running startup "
        "sequence now (daily refresh → backfill)."
    )

    # 1. Daily refresh (idempotent — the 09:00 cron will also fire)
    try:
        run_daily_refresh()
        logger.info("[STARTUP-GATE] Deferred daily refresh complete.")
    except Exception:
        logger.exception("[STARTUP-GATE] Deferred daily refresh failed.")

    # 2. Startup backfill (idempotent — no-op if isolation exists)
    try:
        run_startup_backfill()
        logger.info("[STARTUP-GATE] Deferred backfill complete.")
    except Exception:
        logger.exception("[STARTUP-GATE] Deferred backfill failed.")

    logger.warning(
        "[STARTUP-GATE] Deferred startup sequence fully complete."
    )