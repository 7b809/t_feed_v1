# File: token_tasks/strategy_isolation_task.py
"""Scheduled wrapper for Strategy 1 — Stage 2.

Post-Stage-2 Stage-3 catch-up
-----------------------------
After the Stage-2 isolation service writes the ``stage_2_isolation``
tree to disk, the wrapper calls
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

Combined with the periodic reload in
``strategy_live_tracker_service._run()`` (5 s while the Stage-2 tree
is empty, 30 s once populated), this guarantees that by the time
Stage 4 fires at 09:18:00 the stage-3 summary reflects every contract
that reached its own R2 during the strategy window.

The catch-up call is wrapped in ``try/except`` so a tracker failure
can never break the Stage-2 cron. Stage 2's own Telegram message and
log line are sent before the catch-up so the operator sees the Stage-2
outcome even if the catch-up then fails.

Diagnostics
-----------
Before this revision the module used ``logging.getLogger(__name__)``
and therefore never wrote to ``logs/strategy_isolation_task.log`` or
``logs_text/strategy_isolation_task.txt`` — every log line from this
file was console-only. That is why, during the 2026-10-22
``no_activations`` investigation, the Telegram stream showed nothing
from the post-Stage-2 hook even though the hook ran.

This module now:

    * uses ``get_logger(__file__)`` so every line reaches the file
      handlers and can be grepped afterwards;
    * logs ``[stage_2 hook] starting`` before the ``run_catchup()``
      call so a hook that raises immediately is still visible;
    * includes the time remaining until Stage 4
      (``_seconds_to_stage_4()``) on every hook log line, so the
      timing budget is visible without reconstructing timestamps;
    * **emits a latency WARNING** when the hook fires with fewer
      than ``_STAGE_4_LATENCY_WARNING_SECONDS`` seconds remaining
      until Stage 4. On a normal day the hook runs at 09:17:00 with
      60 s to spare and the warning does not fire; on a slow Stage-2
      the operator sees the impending race before Stage 4 fires.
    * **classifies the outcome via ``_classify_catchup_outcome()``**
      into one of four sub-states and emits a verdict per sub-state:

          ``empty_tree``      → WARNING (Stage 2 wrote nothing;
                                Stage 4 will report no_activations)
          ``no_new_touches``  → WARNING with pointers to
                                ``stage_3_decisions.json`` / OR files
                                / feed subscriptions (normal only if
                                no contract reached its own R2)
          ``activated``       → INFO (Stage 4 has data)
          ``unknown``         → WARNING with the raw sub-state value
                                (defensive)

    * logs one INFO per index on the populated path
      (``NSE_INDEX|Nifty 50: 28/28 contracts, activated=3, reason=...``)
      so a partially-broken run (one index OK, another empty) is
      visible;
    * logs one WARNING per index that produced 0 contracts;
    * reads ``contracts_total`` from the top-level return dict when
      present (the S1 tracker patch adds it), with a per-index-sum
      fallback so a rolling deploy never breaks;
    * on a raised exception, states the *consequence*: Stage 4 at
      09:18:00 will read whatever the tracker last wrote (possibly an
      empty stage_3_summary.json).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger
from services.strategy_isolation_service import strategy_isolation_service
from services.telegram_service import telegram_service
from services.telegram_format import build

logger = get_logger(__file__)

# Threshold below which the post-Stage-2 hook is considered to be
# racing Stage 4. The hook fires at 09:17:00 and Stage 4 at 09:18:00,
# so anything below 5 s of remaining budget is a genuine race.
_STAGE_4_LATENCY_WARNING_SECONDS = 5


# --------------------------------------------------------------------------- #
# Timing helpers                                                              #
# --------------------------------------------------------------------------- #
def _market_now() -> datetime:
    """Return ``datetime.now`` in the configured market timezone."""
    try:
        return datetime.now(ZoneInfo(config.MARKET_TIMEZONE))
    except Exception:
        return datetime.now()


def _seconds_to_stage_4() -> int | None:
    """Return whole seconds remaining until the scheduled Stage-4 time.

    ``None`` on any calculation failure. Used purely for diagnostic
    log lines — the actual Stage-4 cron fires independently and is not
    gated on this value.
    """
    try:
        now = _market_now()
        target = now.replace(
            hour=config.STRATEGY_ORDER_HOUR,
            minute=config.STRATEGY_ORDER_MINUTE,
            second=0,
            microsecond=0,
        )
        delta = (target - now).total_seconds()
        return int(delta)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Telegram message builders                                                   #
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Post-Stage-2 catch-up diagnostics                                           #
# --------------------------------------------------------------------------- #
def _catchup_contracts_total(tracker_summary: dict, indexes: list[dict]) -> int:
    """Return the total contract count from the tracker summary.

    Prefers the new top-level ``contracts_total`` field added by the
    S1 tracker patch; falls back to summing ``contracts_total`` per
    index so a rolling deploy (tracker not yet patched) never breaks
    the outcome classification.
    """
    top = tracker_summary.get("contracts_total")
    if top is not None:
        try:
            return int(top)
        except (TypeError, ValueError):
            pass
    total = 0
    for idx in indexes:
        try:
            total += int(idx.get("contracts_total") or 0)
        except (TypeError, ValueError):
            continue
    return total


def _classify_catchup_outcome(
    tracker_summary: dict,
    indexes: list[dict],
) -> str:
    """Return a short sub-state string for the current catch-up run.

    Values:
        "no_indexes"       — the tracker returned no per-index entries.
                             ``STRATEGY_TRACKER_INDEX_KEYS`` is empty or
                             misconfigured.
        "empty_tree"       — the tracker found 0 contracts total across
                             all indexes. Stage 2 wrote nothing.
        "no_new_touches"   — the tracker found contracts but no new
                             touches were activated. This is *normal*
                             on a quiet day; a WARNING is used so the
                             operator can distinguish it from a
                             broken-pipeline day by following the
                             pointers.
        "activated"        — one or more additional contracts were
                             activated by the catch-up. Stage 4 has
                             data.
        "unknown"          — the summary shape does not match any of
                             the above (defensive).
    """
    if not indexes:
        return "no_indexes"
    status = str(tracker_summary.get("status") or "")
    if status == "disabled":
        return "disabled"
    contracts = _catchup_contracts_total(tracker_summary, indexes)
    if contracts == 0:
        return "empty_tree"
    try:
        activated = int(tracker_summary.get("activated_total") or 0)
    except (TypeError, ValueError):
        activated = 0
    if activated == 0:
        return "no_new_touches"
    if activated > 0:
        return "activated"
    return "unknown"


def _log_catchup_outcome(
    tracker_summary: dict,
    *,
    seconds_to_stage_4: int | None,
) -> None:
    """Log a structured verdict of the post-Stage-2 catch-up.

    Emits one aggregate line and one per-index line. The verdict
    branches on the sub-state returned by
    ``_classify_catchup_outcome()``:

        * ``empty_tree``     → WARNING (Stage 2 wrote nothing)
        * ``no_new_touches`` → WARNING with pointers (normal only if
                                no contract reached its own R2)
        * ``activated``      → INFO (Stage 4 has data)
        * ``disabled``       → INFO (config off)
        * ``no_indexes``     → WARNING (misconfiguration)
        * ``unknown``        → WARNING with raw sub-state value
    """
    status = str(tracker_summary.get("status") or "")
    indexes = tracker_summary.get("indexes") or []
    sub_state = _classify_catchup_outcome(tracker_summary, indexes)

    # ---- No indexes at all: cannot diagnose anything further -------- #
    if sub_state == "no_indexes":
        logger.warning(
            "[stage_2 hook] catch-up returned NO indexes — "
            "STRATEGY_TRACKER_INDEX_KEYS is empty or misconfigured. "
            "Stage 4 will report no_activations."
        )
        return

    # ---- Disabled by config ---------------------------------------- #
    if sub_state == "disabled":
        logger.info(
            "[stage_2 hook] catch-up returned status=disabled — the "
            "S1 tracker is off by config (STRATEGY_ENABLED or "
            "STRATEGY_TRACKER_ENABLED is false). Stage 4 will report "
            "no_activations."
        )
        return

    # ---- Per-index breakdown -------------------------------------- #
    total_contracts = 0
    for idx in indexes:
        try:
            idx_key = str(idx.get("index_key") or "?")
            contracts_total = int(idx.get("contracts_total") or 0)
            contracts_activated = int(idx.get("contracts_activated") or 0)
            reason_code = str(idx.get("reason_code") or "?")
            total_contracts += contracts_total

            if contracts_total > 0:
                logger.info(
                    "[stage_2 hook] %s: %d/%d contracts, activated=%d, "
                    "reason=%s",
                    idx_key,
                    contracts_activated,
                    contracts_total,
                    contracts_activated,
                    reason_code,
                )
            else:
                logger.warning(
                    "[stage_2 hook] %s: 0 contracts loaded — Stage-2 tree "
                    "produced nothing for this index (reason=%s). "
                    "Stage 4 will report no_activations for it.",
                    idx_key,
                    reason_code,
                )
        except Exception:
            logger.exception(
                "[stage_2 hook] failed parsing an index entry: %r", idx,
            )

    # ---- Aggregate verdict per sub-state --------------------------- #
    if sub_state == "empty_tree":
        logger.warning(
            "[stage_2 hook] catch-up found 0 contracts TOTAL across %d "
            "index(es). Stage 2 wrote no stage_2_isolation output. "
            "Possible causes: Stage 2 skipped (reason=no_stage_1), all "
            "contracts failed to isolate, or Stage 1 produced 0 "
            "contracts. Check the Stage-2 summary and the Stage-1 "
            "summary. Stage 4 will report no_activations.",
            len(indexes),
        )
        return

    if sub_state == "no_new_touches":
        activated = int(tracker_summary.get("activated_total") or 0)
        logger.warning(
            "[stage_2 hook] catch-up evaluated %d contract(s) across "
            "%d index(es) but activated %d. This is NORMAL if no "
            "contract's own 09:15–09:20 window-high reached its own "
            "R2. Verify via stage_3_decisions.json (per-contract "
            "reason_codes) or /admin/strategy/stage3. If you expected "
            "activations, check (a) each strike folder under "
            "stage_1_initial contains opening_range.json with levels, "
            "and (b) the strike instrument_keys are subscribed to the "
            "live feed (GET /live/status).",
            total_contracts,
            len(indexes),
            activated,
        )
        return

    if sub_state == "activated":
        activated = int(tracker_summary.get("activated_total") or 0)
        logger.info(
            "[stage_2 hook] catch-up evaluated %d contract(s) across "
            "%d index(es) and activated %d. Stage 4 has data to act on "
            "(fires in %ss).",
            total_contracts,
            len(indexes),
            activated,
            str(seconds_to_stage_4) if seconds_to_stage_4 is not None else "?",
        )
        return

    # ---- Fallback for unknown sub-state ---------------------------- #
    logger.warning(
        "[stage_2 hook] catch-up returned an unrecognised shape "
        "(status=%r sub_state=%r). contracts_total=%d indexes=%d "
        "activated_total=%r. Check the S1 tracker's run_catchup() "
        "return dict.",
        status,
        sub_state,
        total_contracts,
        len(indexes),
        tracker_summary.get("activated_total"),
    )


# --------------------------------------------------------------------------- #
# Public entry point                                                          #
# --------------------------------------------------------------------------- #
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
        seconds_to_stage4 = _seconds_to_stage_4()
        logger.info(
            "[stage_2 hook] starting post-Stage-2 catch-up — "
            "stage_2 just wrote %d contract(s); tracker will be forced "
            "to reload from disk and re-scan the 09:15–09:20 window. "
            "Stage 4 fires in %s seconds.",
            isolated,
            str(seconds_to_stage4) if seconds_to_stage4 is not None else "?",
        )

        # ---- Timing-budget warning -------------------------------- #
        # On a normal day the hook runs at 09:17:00 with 60 s to
        # spare. If the budget is below 5 s the Stage-2 cron itself
        # was late — Stage 4 may fire before the catch-up finishes
        # writing its summary. The operator needs to know.
        if (
            seconds_to_stage4 is not None
            and 0 <= seconds_to_stage4 < _STAGE_4_LATENCY_WARNING_SECONDS
        ):
            logger.warning(
                "[stage_2 hook] only %ss remain until Stage 4 fires — "
                "the Stage-2 cron is late. Stage 4 may read the "
                "previous stage_3_summary.json. Check Stage-2 run "
                "duration in logs/strategy_isolation_service.log.",
                seconds_to_stage4,
            )

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
                "[stage_2 hook] catch-up returned: status=%s "
                "activated_total=%s indexes=%d",
                tracker_summary.get("status"),
                tracker_summary.get("activated_total"),
                len(tracker_summary.get("indexes") or []),
            )
            _log_catchup_outcome(
                tracker_summary,
                seconds_to_stage_4=seconds_to_stage4,
            )
        except Exception:
            logger.exception(
                "[stage_2 hook] catch-up RAISED — Stage-2 cron still "
                "completed successfully, but Stage 4 at %02d:%02d will "
                "read whatever the tracker last wrote (possibly an "
                "empty stage_3_summary.json).",
                config.STRATEGY_ORDER_HOUR,
                config.STRATEGY_ORDER_MINUTE,
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