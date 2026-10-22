# File: token_tasks/strategy_tracker_catchup_task.py
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

Diagnostics (added for the 2026-10-22 observability patch set)
--------------------------------------------------------------
This wrapper used ``logging.getLogger(__name__)``, which means every
log line from it was console-only — nothing reached
``logs/strategy_tracker_catchup_task.log`` or
``logs_text/strategy_tracker_catchup_task.txt``. Combined with the
wrapper collapsing three distinct run states into a single
"completed: status=X activated_total=N" line that only appeared at
INFO, an operator had no way to tell from the file logs whether:

    * the catch-up had fired at all (the scheduler might have
      silently dropped the job),
    * it had found 0 stage-2 contracts (upstream Stage 1/2 broken),
    * it had found contracts but no new touches (normal quiet day),
    * it had found new touches (its purpose), or
    * it had completed in time for Stage 4 to read its output.

On 2026-10-22 this job fired at 09:17:30 with 68 contracts in the
tree but 0 activations, and there was no log line anywhere saying
*why* — the Telegram stream showed only the downstream Stage-4
``no_activations`` message at 09:18:00.

This wrapper now:

    * uses ``get_logger(__file__)`` so every line reaches the file
      handlers;
    * emits one ``[stage_3_catchup wrapper] starting`` INFO line
      *before* the run, naming the mode and the seconds remaining
      until Stage 4 fires;
    * times the ``run_catchup()`` call and includes the elapsed time
      on every post-run line;
    * emits one ``[stage_3_catchup wrapper] diagnosis`` aggregate
      INFO line per run with the summed counters
      (``contracts / activated / indexes``), the elapsed time, and
      the seconds remaining until Stage 4;
    * emits a state-specific INFO or WARNING line for the three
      sub-states (see "Sub-states" below);
    * emits one per-index breakdown line with
      ``contracts / activated / reason_code`` so a partially-broken
      run is visible without reading ``stage_3_decisions.json``;
    * emits a **latency WARNING** when the catch-up takes longer
      than ``_STAGE_4_LATENCY_WARNING_SECONDS`` (25 s), because
      Stage 4 may have started before the catch-up wrote its
      summary — the operator needs to know the safety net itself
      was racing the cron;
    * preserves the original Telegram noise gate: a success
      message is only sent when ``status == "ok"`` **and**
      ``activated_total > 0``. This job is deliberately quiet on
      the normal no-new-touch case — the post-Stage-2 hook in
      ``strategy_isolation_task.py`` already handled the empty-tree
      and missing-level alerts for the day, and duplicating them
      here would just be noise;
    * augments the Telegram success message with
      ``Indexes with activations / Elapsed / Stage 4 in`` fields
      so the operator can confirm the catch-up finished with
      headroom;
    * augments the failure Telegram message with the same timing
      fields so the operator can tell whether the failure is
      still recoverable before Stage 4 fires.

Sub-states
----------
``_classify_run()`` returns one of:

    "disabled"        — STRATEGY_ENABLED or STRATEGY_TRACKER_ENABLED
                        is false. The catch-up did nothing.
    "empty_tree"      — the Stage-2 tree has 0 contracts. **Stage 4
                        will report no_activations**; the operator
                        needs to check Stage 1/2 immediately (a
                        30-second window still remains before
                        Stage 4).
    "no_new_touches"  — the tree has contracts but no new touches
                        were recorded since the post-Stage-2 hook.
                        This is the *normal* quiet-day case; the
                        catch-up did its job (reload + re-scan) and
                        confirmed the tracker's view is up to date.
    "activated"       — one or more additional contracts were
                        activated by the catch-up. The success
                        Telegram message is sent.

Timing budget
-------------
The job fires at 09:17:30 and Stage 4 fires at 09:18:00, so the
catch-up has 30 s to run before Stage 4 reads ``stage_3_summary.json``.
The wrapper emits a WARNING if the elapsed time exceeds 25 s,
leaving less than 5 s of headroom.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger
from services.strategy_live_tracker_service import (
    strategy_live_tracker_service,
)
from services.telegram_format import build
from services.telegram_service import telegram_service

logger = get_logger(__file__)

# Threshold above which the catch-up is considered to be racing Stage 4.
# The job fires at 09:17:30 and Stage 4 at 09:18:00, so anything above
# 25 s leaves <5 s of headroom.
_STAGE_4_LATENCY_WARNING_SECONDS = 25.0


# --------------------------------------------------------------------------- #
# Timing helpers                                                              #
# --------------------------------------------------------------------------- #
def _market_now() -> datetime:
    try:
        return datetime.now(ZoneInfo(config.MARKET_TIMEZONE))
    except Exception:
        return datetime.now()


def _seconds_to_stage_4() -> int | None:
    """Return whole seconds remaining until the scheduled Stage-4 time.

    ``None`` on any calculation failure. Used purely for diagnostic
    log lines and Telegram fields — Stage-4 fires independently of
    this value.
    """
    try:
        now = _market_now()
        target = now.replace(
            hour=config.STRATEGY_ORDER_HOUR,
            minute=config.STRATEGY_ORDER_MINUTE,
            second=0,
            microsecond=0,
        )
        return int((target - now).total_seconds())
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Counter helpers                                                             #
# --------------------------------------------------------------------------- #
def _total_contracts(summary: dict[str, Any]) -> int:
    """Sum ``contracts_total`` across the per-index decisions.

    The S1 tracker's ``run_catchup()`` does not return a top-level
    ``stage2_contracts`` field — the count is per index. This helper
    exposes the aggregate for the diagnostic and the Telegram message.
    """
    indexes = summary.get("indexes") or []
    return sum(int(i.get("contracts_total") or 0) for i in indexes)


def _total_activated(summary: dict[str, Any]) -> int:
    return int(summary.get("activated_total") or 0)


def _indexes_with_activations(summary: dict[str, Any]) -> int:
    indexes = summary.get("indexes") or []
    return sum(
        1 for i in indexes if int(i.get("contracts_activated") or 0) > 0
    )


# --------------------------------------------------------------------------- #
# Sub-state classification                                                    #
# --------------------------------------------------------------------------- #
def _classify_run(summary: dict[str, Any]) -> str:
    """Return a short sub-state string for the current run.

    See the module docstring's "Sub-states" section for the
    interpretation of each value.
    """
    status = str(summary.get("status") or "")
    if status == "disabled":
        return "disabled"

    contracts = _total_contracts(summary)
    if contracts == 0:
        return "empty_tree"

    if _total_activated(summary) == 0:
        return "no_new_touches"

    return "activated"


# --------------------------------------------------------------------------- #
# Structured log diagnosis                                                    #
# --------------------------------------------------------------------------- #
def _log_diagnostics(
    summary: dict[str, Any],
    *,
    elapsed_seconds: float,
    seconds_to_stage_4_after: int | None,
) -> None:
    """Emit one structured per-run diagnosis block to the file logger.

    Aggregate line is always INFO. The state-specific line after it is
    the one that names the failing sub-step (or confirms normal
    operation). Emitted on every run — including the ones that produce
    no Telegram message — so the log file tells the full story.
    """
    status = str(summary.get("status") or "")
    contracts = _total_contracts(summary)
    activated = _total_activated(summary)
    indexes_count = len(summary.get("indexes") or [])
    indexes_activated = _indexes_with_activations(summary)
    sub_state = _classify_run(summary)

    # ---- Aggregate line ------------------------------------------------ #
    logger.info(
        "[stage_3_catchup wrapper] diagnosis — status=%s sub_state=%s "
        "indexes=%d indexes_with_activations=%d contracts=%d "
        "activated_total=%d elapsed=%.2fs stage_4_in=%ss",
        status or "?",
        sub_state,
        indexes_count,
        indexes_activated,
        contracts,
        activated,
        elapsed_seconds,
        str(seconds_to_stage_4_after)
        if seconds_to_stage_4_after is not None else "?",
    )

    # ---- State-specific consequence ---------------------------------- #
    if sub_state == "disabled":
        logger.info(
            "[stage_3_catchup wrapper] disabled by config — "
            "no work performed."
        )
        return

    if sub_state == "empty_tree":
        logger.warning(
            "[stage_3_catchup wrapper] stage-2 tree has 0 contracts — "
            "Stage 4 will report no_activations. This is the "
            "2026-10-22 failure signature. Check GET "
            "/admin/strategy/isolation and GET /admin/strategy "
            "(Stage 1 reason field) immediately — Stage 4 fires in "
            "%ss.",
            str(seconds_to_stage_4_after)
            if seconds_to_stage_4_after is not None else "?",
        )
        return

    if sub_state == "no_new_touches":
        logger.info(
            "[stage_3_catchup wrapper] %d contract(s) in the tree but no "
            "new touches since the post-Stage-2 hook. This is the "
            "normal quiet-day case — the catch-up reloaded the tree "
            "and confirmed the tracker's view is up to date.",
            contracts,
        )
        return

    if sub_state == "activated":
        logger.info(
            "[stage_3_catchup wrapper] %d additional contract(s) "
            "activated by the catch-up (%d/%d index(es) contributed) — "
            "Stage 4 will see them in %ss.",
            activated,
            indexes_activated,
            indexes_count,
            str(seconds_to_stage_4_after)
            if seconds_to_stage_4_after is not None else "?",
        )
        return

    logger.warning(
        "[stage_3_catchup wrapper] unexpected sub_state=%r — no "
        "specific diagnosis available.",
        sub_state,
    )


def _log_per_index_breakdown(summary: dict[str, Any]) -> None:
    """Emit one per-index line so a partially-broken run is visible.

    INFO when the index produced any contracts, WARNING when it did
    not — the two cases need different operator attention.
    """
    for idx in (summary.get("indexes") or []):
        try:
            idx_key = str(idx.get("index_key") or "?")
            contracts = int(idx.get("contracts_total") or 0)
            activated = int(idx.get("contracts_activated") or 0)
            reason_code = str(idx.get("reason_code") or "?")
            if contracts == 0:
                logger.warning(
                    "[stage_3_catchup wrapper] %s: 0 contracts — "
                    "Stage-2 produced nothing for this index "
                    "(reason=%s). Stage 4 will report no_activations "
                    "for it.",
                    idx_key, reason_code,
                )
            else:
                logger.info(
                    "[stage_3_catchup wrapper] %s: %d/%d contract(s), "
                    "activated=%d, reason=%s",
                    idx_key, activated, contracts, activated,
                    reason_code,
                )
        except Exception:
            logger.exception(
                "[stage_3_catchup wrapper] failed parsing an index "
                "entry: %r", idx,
            )


# --------------------------------------------------------------------------- #
# Telegram message builders                                                   #
# --------------------------------------------------------------------------- #
def _build_success_message(
    summary: dict[str, Any],
    *,
    elapsed_seconds: float,
    seconds_to_stage_4_after: int | None,
) -> str:
    """Telegram message for the ``activated > 0`` case.

    Preserves the original message shape (Mode / Activated / Day /
    Window / Result) and adds three timing fields so the operator can
    confirm the catch-up finished with headroom before Stage 4.
    """
    activated = _total_activated(summary)
    indexes_activated = _indexes_with_activations(summary)
    indexes_count = len(summary.get("indexes") or [])

    return build(
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
            (
                "Indexes with activations",
                f"{indexes_activated} / {indexes_count}",
            ),
            ("Elapsed", f"{elapsed_seconds:.1f}s"),
            (
                "Stage 4 in",
                f"{seconds_to_stage_4_after}s"
                if seconds_to_stage_4_after is not None else "?",
            ),
        ],
        sections=[(
            "Result",
            "📊",
            [
                f"{activated} additional contract(s) activated by the "
                f"catch-up cron before Stage 4 order placement.",
            ],
        )],
        status="success",
        status_detail=f"activated={activated}",
    )


def _build_failure_message(
    exc: BaseException,
    *,
    elapsed_seconds: float,
    seconds_to_stage_4_after: int | None,
) -> str:
    """Telegram message for the catch-up-raised case.

    Naming the remaining seconds until Stage 4 tells the operator
    whether the failure is still recoverable (there is time to POST
    /admin/strategy/tracker/start) or whether Stage 4 has already
    read stale data.
    """
    return build(
        emoji="❌",
        title="Strategy 1 · Stage 3 Catch-up · Failed",
        fields=[
            ("Error", f"{type(exc).__name__}: {exc}"),
            ("Elapsed", f"{elapsed_seconds:.1f}s"),
            (
                "Stage 4 in",
                f"{seconds_to_stage_4_after}s"
                if seconds_to_stage_4_after is not None else "?",
            ),
        ],
        sections=[(
            "Consequence",
            "⚠️",
            [
                "Stage 4 reads stage_3_summary.json and will use "
                "whatever the tracker last wrote — possibly an empty "
                "or stale summary.",
                "If Stage 4 has not fired yet, recover by running "
                "POST /admin/strategy/tracker/start.",
            ],
        )],
        status="failed",
    )


# --------------------------------------------------------------------------- #
# Public entry point                                                          #
# --------------------------------------------------------------------------- #
def run_strategy_tracker_catchup() -> None:
    # ---- Pre-run log line --------------------------------------------- #
    seconds_before = _seconds_to_stage_4()
    logger.info(
        "[stage_3_catchup wrapper] starting — mode=scheduled_catchup "
        "stage_4_in=%ss",
        str(seconds_before) if seconds_before is not None else "?",
    )

    started_at = time.monotonic()
    try:
        summary = strategy_live_tracker_service.run_catchup(
            mode="scheduled_catchup",
            catchup=True,
        )
        elapsed = time.monotonic() - started_at
        seconds_after = _seconds_to_stage_4()

        # ---- Structured diagnosis (file log, always emitted) -------- #
        _log_diagnostics(
            summary,
            elapsed_seconds=elapsed,
            seconds_to_stage_4_after=seconds_after,
        )
        _log_per_index_breakdown(summary)

        # ---- Latency warning: catch-up may have raced Stage 4 ------ #
        if elapsed > _STAGE_4_LATENCY_WARNING_SECONDS:
            logger.warning(
                "[stage_3_catchup wrapper] catch-up took %.1fs — above "
                "the %.0fs threshold. Stage 4 fires in %ss; it may "
                "have started before the catch-up finished writing its "
                "summary. The safety net itself was racing the cron.",
                elapsed,
                _STAGE_4_LATENCY_WARNING_SECONDS,
                str(seconds_after) if seconds_after is not None else "?",
            )

        # ---- Preserve the original Telegram noise gate ------------- #
        # Send the success message only when the catch-up actually
        # activated something. The post-Stage-2 hook
        # (strategy_isolation_task.py) already handles the empty-tree
        # and missing-level alerts for the day, so duplicating them
        # here would just be noise. The log lines above are the
        # operator's visibility into the quiet case.
        activated = _total_activated(summary)
        status = str(summary.get("status") or "unknown")

        if status == "ok" and activated > 0:
            telegram_service.send_message(
                _build_success_message(
                    summary,
                    elapsed_seconds=elapsed,
                    seconds_to_stage_4_after=seconds_after,
                ),
                level="SUCCESS",
            )

        # ---- Post-run aggregate log line --------------------------- #
        logger.info(
            "[stage_3_catchup wrapper] completed — status=%s "
            "activated_total=%d elapsed=%.2fs stage_4_in=%ss",
            status,
            activated,
            elapsed,
            str(seconds_after) if seconds_after is not None else "?",
        )
    except Exception as exc:
        elapsed = time.monotonic() - started_at
        seconds_after = _seconds_to_stage_4()

        logger.exception(
            "[stage_3_catchup wrapper] catch-up RAISED — Stage 4 at "
            "%02d:%02d will read whatever the tracker last wrote "
            "(possibly an empty stage_3_summary.json). elapsed=%.2fs "
            "stage_4_in=%ss",
            config.STRATEGY_ORDER_HOUR,
            config.STRATEGY_ORDER_MINUTE,
            elapsed,
            str(seconds_after) if seconds_after is not None else "?",
        )

        # Best-effort alert — the operator needs to know the safety
        # net itself failed, because Stage 4 will then read whatever
        # the tracker last wrote.
        try:
            telegram_service.send_message(
                _build_failure_message(
                    exc,
                    elapsed_seconds=elapsed,
                    seconds_to_stage_4_after=seconds_after,
                ),
                level="ERROR",
            )
        except Exception:
            # Never let a Telegram failure mask the original error.
            pass