# jobs/daily_refresh.py
"""Daily refresh job: token, feed, OR levels, Telegram summary.

This module owns the singleton ``MarketFeedService`` instance for the
whole process. Downstream consumers (isolation scan, isolation API,
isolation service) must use :func:`get_feed_service` to read it rather
than importing the module-level ``_feed_service`` directly — that keeps
the accessor and the writer in one place, so the lifetime of the feed
is easy to reason about.

OR levels flow (replaces the old pivot-levels check)
----------------------------------------------------
  1. If ``OR_AUTO_COMPUTE_ON_STARTUP`` is False → skip, mark not ready.
  2. Otherwise call :meth:`ORLevelsService.ensure_or_levels`, which:
       a. reads ``pivot_levels`` from Mongo — if ``trade_date_ist`` is
          today, uses it as-is (saves a local copy, no candle fetch);
       b. otherwise fetches 1-min intraday candles for the 4 indices via
          ``HistoryV3Api``, computes OR levels locally, and writes them
          back to Mongo + ``data/or_levels.json``.
  3. Pre-market restarts (before the 09:15 candle has closed) will see
     the compute step defer with ``OR levels ready = False`` and
     ``OR levels source = deferred_no_candles`` — the 09:18 pivot check
     and each isolation scan tick will retry once market opens.

Idempotency
-----------
``run_daily_refresh()`` is guarded to run **at most once per calendar
day** per process. This matters because:

  * In deferred-startup mode, both the ``deferred_startup`` one-shot
    *and* the recurring ``daily_refresh @ 09:00`` cron fire at the
    same instant. Whichever arrives first runs the pipeline; the
    second sees the guard and returns immediately.

  * Any future manual calls (or accidental double-scheduling) are
    also silently no-oped rather than doubling token fetches, feed
    connects, OR-level computations, and Telegram summaries.

The guard is set only **after** the pipeline reaches the
"Daily refresh completed" line. Early returns from token-fetch or
token-validate failures do **not** set the guard, so a later call
(e.g. the 09:18 pivot_check retry, or a manual force) can still
attempt a full run.

Callers that legitimately need a second run (manual testing via
``POST /trigger/refresh?force=true``) pass ``force=True`` to bypass.

Concurrency
-----------
The idempotency check and the pipeline body are both wrapped in a
process-wide ``threading.Lock`` (``_daily_refresh_lock``). This is the
crucial detail: without the lock, two simultaneous callers (the
``deferred_startup`` one-shot and the ``daily_refresh @ 09:00`` cron
both firing at 09:18:00) can **both** observe ``_last_daily_refresh_date
!= today`` before either has a chance to set it. Both would then run
the full pipeline — two feed connects, two ``MarketFeedService``
threads, doubled ``[HEALTH]`` output every 300 s, doubled Telegram
summaries, and three parallel ``MongoClient`` initialisations from the
``get_mongo_db()`` race.

With the lock, the second caller blocks on ``acquire()`` until the
first has finished; by the time it proceeds, ``_last_daily_refresh_date``
is set to today and the guard immediately returns. Feed thread count
stays at 1.

Locking is also correct for ``force=True``: a manual force call after
the guard has been set will wait for any in-flight refresh to finish,
then re-run the pipeline. That matches operator expectations.

The lock is a plain ``threading.Lock`` (not reentrant). No code path
re-enters ``run_daily_refresh`` from within the pipeline, so this is
safe. If a future change introduces that pattern, switch to
``threading.RLock``.

Backward-compatibility shim
---------------------------
``is_pivot_levels_updated_today`` and ``load_pivot_levels_to_local``
are still re-exported from this module because ``scheduler.py`` imports
them at module load time (``from jobs.daily_refresh import
is_pivot_levels_updated_today``). They are NOT used by
``run_daily_refresh`` anymore. Remove the shim once ``scheduler.py``'s
09:18 pivot check is migrated to ``ORLevelsService``.
"""

from __future__ import annotations

import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo

from core.config import OR_AUTO_COMPUTE_ON_STARTUP, TIMEZONE
from core.logger import get_logger
from core.market_feed_service import MarketFeedService
from core.telegram_notifier import (
    send_telegram_message,
    build_daily_refresh_summary,
)
from core.token_service import fetch_access_token, validate_access_token

# Backward-compat re-exports for scheduler.py. Not used here.
from core.pivot_levels_loader import (  # noqa: F401
    is_pivot_levels_updated_today,
    load_pivot_levels_to_local,
)

from services.or_levels_service import ORLevelsService

logger = get_logger(__file__)

# Module-level singleton so the disconnect job and the isolation
# pipeline share the same live feed instance.
_feed_service: MarketFeedService | None = None

# Guards ``run_daily_refresh`` to at most one full run per day (per
# process). Cleared implicitly on process restart (module reload).
_last_daily_refresh_date: str | None = None

# Process-wide serialisation for ``run_daily_refresh``. See the module
# docstring ("Concurrency" section) for why this is required: without
# it, the ``deferred_startup`` one-shot and the ``daily_refresh`` cron
# can both pass the per-day idempotency guard simultaneously and run
# the entire pipeline twice — doubling feed threads, health logs, and
# Telegram summaries.
_daily_refresh_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Accessors                                                                   #
# --------------------------------------------------------------------------- #
def get_feed_service() -> MarketFeedService | None:
    """Return the active ``MarketFeedService`` or ``None`` if not yet
    connected.

    Used by:
      * ``jobs/isolation_scan.py`` — to skip ticks when the feed is down
      * ``services/isolation_service.py`` — to snapshot option LTPs
      * ``api/routes/isolation.py`` — to serve the manual trigger route

    Never raises. The caller is expected to handle ``None``.
    """
    return _feed_service


def get_last_daily_refresh_date() -> str | None:
    """Return the ISO date of the last successful daily refresh, or
    ``None`` if the pipeline has not yet run today.

    Exposed by ``GET /startup-state`` so operators can confirm whether
    the idempotency guard is currently set for today.
    """
    return _last_daily_refresh_date


# --------------------------------------------------------------------------- #
# OR levels preparation                                                       #
# --------------------------------------------------------------------------- #
def _prepare_or_levels(token: str, tasks: dict) -> None:
    """Prepare today's OR levels (existing in Mongo, or computed from candles).

    Populates ``tasks`` in-place:
      * ``tasks["OR levels ready"]``  → bool
      * ``tasks["OR levels source"]`` → one of:
            "mongo_existing"         (loaded from Mongo, fresh today)
            "local_startup_backfill" (computed from intraday candles)
            "deferred_no_candles"    (pre-market, no 09:15 candle yet)
            "deferred_pre_market"    (before market open, no Mongo doc)
            "auto_compute_disabled"  (flag OFF)
            "error"                  (unexpected exception)

    Never raises — every failure path is captured in ``tasks``.
    """
    if not OR_AUTO_COMPUTE_ON_STARTUP:
        tasks["OR levels ready"] = False
        tasks["OR levels source"] = "auto_compute_disabled"
        logger.info(
            "OR levels auto-compute disabled "
            "(OR_AUTO_COMPUTE_ON_STARTUP=false)"
        )
        return

    now = datetime.now(ZoneInfo(TIMEZONE))
    pre_market = (
        now.weekday() > 4
        or now.hour < 9
        or (now.hour == 9 and now.minute < 17)
    )
    if pre_market:
        logger.info(
            "OR levels: pre-market (%s); will attempt Mongo read, "
            "compute deferred",
            now.strftime("%A %H:%M"),
        )

    try:
        or_doc = ORLevelsService(token).ensure_or_levels()
    except RuntimeError as exc:
        msg = str(exc)
        if "no indexes computed" in msg:
            # Expected when pre-market on a fresh day (no 09:15 candle yet)
            tasks["OR levels ready"] = False
            tasks["OR levels source"] = (
                "deferred_pre_market" if pre_market else "deferred_no_candles"
            )
            logger.warning(
                "OR levels deferred (no 09:15 candle yet): %s", msg
            )
        else:
            logger.exception("OR levels preparation failed (RuntimeError)")
            tasks["OR levels ready"] = False
            tasks["OR levels source"] = "error"
        return
    except Exception:
        logger.exception("OR levels preparation failed.")
        tasks["OR levels ready"] = False
        tasks["OR levels source"] = "error"
        return

    indexes = or_doc.get("indexes") or {}
    tasks["OR levels ready"] = bool(indexes)
    source = (
        or_doc.get("trigger")
        or or_doc.get("source")
        or "unknown"
    )
    tasks["OR levels source"] = source

    if indexes:
        logger.info(
            "OR levels ready (source=%s, indexes=%d)",
            source, len(indexes),
        )
    else:
        logger.warning("OR levels prepared but indexes empty")


# --------------------------------------------------------------------------- #
# Jobs                                                                        #
# --------------------------------------------------------------------------- #
def run_daily_refresh(force: bool = False) -> None:
    """Execute all daily refresh tasks and send **one** Telegram summary.

    Idempotent per calendar day (per process). In deferred-startup mode
    this method fires twice at the cutoff — once from the
    ``deferred_startup`` one-shot, once from the recurring
    ``daily_refresh @ 09:00`` cron. The guard ensures only the first
    actually runs the pipeline.

    The idempotency check **and** the full pipeline body run under
    ``_daily_refresh_lock`` so two callers that fire at the same instant
    cannot both pass the guard before either sets it. Without the lock,
    both would race through the check and the pipeline would run twice.

    :param force: when ``True``, bypass the per-day idempotency guard.
                  Used by ``POST /trigger/refresh?force=true``.
    """
    global _feed_service, _last_daily_refresh_date

    # Serialize check + run. The lock is held for the entire pipeline;
    # callers that arrive while a refresh is in flight simply block
    # until it completes, then observe the updated guard and return.
    with _daily_refresh_lock:
        # --- Idempotency guard ---
        today = date.today().isoformat()
        if not force and _last_daily_refresh_date == today:
            logger.info(
                "run_daily_refresh: already ran today (%s); skipping duplicate "
                "invocation (use force=True to override).",
                today,
            )
            return

        tasks: dict = {}

        # 1. Fetch token
        try:
            token = fetch_access_token()
            tasks["Token fetched"] = True
        except Exception:
            logger.exception("Token fetch failed.")
            tasks["Token fetched"] = False
            send_telegram_message(build_daily_refresh_summary(tasks))
            # Guard is NOT set on token-fetch failure, so a later call can
            # retry (e.g. manual /trigger/refresh, or the deferred startup
            # sequence running after a transient Mongo hiccup).
            return

        # 2. Validate token
        valid = validate_access_token(token)
        tasks["Token valid"] = valid
        if not valid:
            send_telegram_message(build_daily_refresh_summary(tasks))
            # Guard is NOT set on validation failure — same rationale as
            # above. An operator can re-validate after the upstream app
            # refreshes the token.
            return

        # 3. Connect to feed
        try:
            _feed_service = MarketFeedService(token)
            _feed_service.connect()
            tasks["Feed connected"] = True
        except Exception:
            logger.exception("Feed connection failed.")
            tasks["Feed connected"] = False

        # 4. Prepare OR levels
        #    (Mongo → if fresh today use it; else compute from intraday candles)
        _prepare_or_levels(token, tasks)

        # 5. Send single summary message
        summary = build_daily_refresh_summary(tasks)
        send_telegram_message(summary)
        logger.info("Daily refresh completed: %s", tasks)

        # --- Mark the guard only now, after the pipeline fully ran ---
        _last_daily_refresh_date = today
        logger.info("run_daily_refresh: marked complete for %s", today)


def disconnect_feed() -> None:
    """Disconnect the market feed (called at 15:45)."""
    global _feed_service
    if _feed_service:
        _feed_service.disconnect()
        logger.info("Feed disconnected at end of day.")