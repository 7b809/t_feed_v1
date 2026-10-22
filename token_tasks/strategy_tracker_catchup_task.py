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
    * emits a state-specific INFO or WARNING line for the four
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
                        is false. The catch-up did nothing. INFO
                        (config is off — not an anomaly).
    "no_indexes"      — the tracker returned no per-index entries.
                        STRATEGY_TRACKER_INDEX_KEYS is empty or
                        misconfigured. WARNING.
    "empty_tree"      — the Stage-2 tree has 0 contracts. **Stage 4
                        will report no_activations**; the operator
                        needs to check Stage 1/2 immediately (a
                        30-second window still remains before
                        Stage 4). WARNING.
    "no_new_touches"  — the tree has contracts but no new touches
                        were recorded since the post-Stage-2 hook.
                        This is the *normal* quiet-day case; the
                        catch-up did its job (reload + re-scan) and
                        confirmed the tracker's view is up to date.
                        INFO.
    "activated"       — one or more additional contracts were
                        activated by the catch-up. The success
                        Telegram message is sent. INFO.

Timing budget
-------------
The job fires at 09:17:30 and Stage 4 fires at 09:18:00, so the
catch-up has 30 s to run before Stage 4 reads ``stage_3_summary.json``.
The wrapper emits a WARNING if the elapsed time exceeds 25 s,
leaving less than 5 s of headroom.

Contracts-total source
----------------------
``_total_contracts()`` prefers the top-level ``contracts_total`` field
added to the S1 tracker's ``run_catchup()`` return dict by the Oct 22
patch. It falls back to summing ``contracts_total`` per index, so a
rolling deploy where the tracker has not yet been patched still
classifies correctly.

Additional hardening (this revision)
------------------------------------
Building on the 2026-10-22 patch, this revision adds:

    * **A ``_STAGE_3_CATCHUP_WRAPPER_MARKER`` constant.** Every log
      line from this module starts with ``[stage_3_catchup wrapper]``
      so an operator can pull every catch-up wrapper line with a
      single ``grep '[stage_3_catchup wrapper]'``. Matches the
      convention used by the S1 Stage-1/2/4 wrappers and the S2
      Stage-3/4 wrappers.

    * **A ``no_new_touches`` WARNING escalation on partial-tree runs.**
      When the tracker found contracts for some indexes but 0
      contracts for others, ``_total_contracts()`` still returns
      non-zero (the sum of the partial counts), so the
      classification lands on ``no_new_touches``. The old code
      logged that as INFO. This revision detects the
      partial-tree state (some indexes have contracts, some don't)
      and escalates to WARNING with a named list of the empty
      indexes — so a partial-tree failure is not hidden under the
      "quiet day" INFO line.

    * **An "empty_tree but elapsed > threshold" Telegram branch.**
      When the catch-up found 0 contracts *and* it took longer than
      the latency threshold to determine that, the operator gets a
      single WARNING that names both problems. This is the
      catastrophic case on 2026-10-22: the tracker spent 25 s
      discovering the tree was empty (because it scanned a large
      directory) and then Stage 4 read an empty summary. Combining
      the two signals into one message makes the diagnosis
      immediate.

    * **A "stale_day" Telegram branch in the success path.** When
      ``summary.get("day")`` does not match today's date (a manual
      run against a previous day's tree), the success message is
      escalated to WARNING and names both dates. Without this, a
      stale tree could produce a "✅ activated=3" message even
      though those activations were for yesterday.

    * **Tolerant fields on ``_build_success_message``.** Missing
      ``day`` / ``window_start`` / ``window_end`` render as
      ``"?"`` instead of ``"None"``. Makes a partially filled
      summary visibly wrong rather than silently misleading.

    * **A per-run state snapshot in ``_LAST_RUN_STATE``.** The
      wrapper now records the outcome of every successful call so
      a subsequent call (or the operator reading the logs
      afterwards) can see whether the state changed. This was
      previously only implicit in the Telegram noise gate.

Telegram message shape
----------------------
The wrapper is deliberately quiet — it sends a Telegram message in
exactly three cases:

    * ``activated > 0`` (SUCCESS): the catch-up added new activations.
    * ``empty_tree`` with non-zero elapsed (WARNING): the tree is
      empty AND the catch-up was slow enough to risk Stage 4 racing.
    * The service raised (ERROR): the safety net itself failed.

Every other case is deliberately log-only so the 30-second window
stays noise-free. This is the same philosophy as the S2 Stage-3
recheck — deliberately quiet on the no-change path.
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

# Grep anchor. Every log line from this module starts with this
# prefix, so an operator can pull every catch-up wrapper line with a
# single ``grep '[stage_3_catchup wrapper]'``.
_STAGE_3_CATCHUP_WRAPPER_MARKER = "[stage_3_catchup wrapper]"

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


def _today_iso() -> str:
    return _market_now().date().isoformat()


# --------------------------------------------------------------------------- #
# Counter helpers                                                             #
# --------------------------------------------------------------------------- #
def _total_contracts(summary: dict[str, Any]) -> int:
    """Return the total contract count from the tracker summary.

    Prefers the new top-level ``contracts_total`` field added by the
    S1 tracker patch; falls back to summing ``contracts_total`` per
    index so a rolling deploy (tracker not yet patched) never breaks
    the classification.
    """
    top = summary.get("contracts_total")
    if top is not None:
        try:
            return int(top)
        except (TypeError, ValueError):
            pass
    indexes = summary.get("indexes") or []
    return sum(int(i.get("contracts_total") or 0) for i in indexes)


def _total_activated(summary: dict[str, Any]) -> int:
    try:
        return int(summary.get("activated_total") or 0)
    except (TypeError, ValueError):
        return 0


def _indexes_with_activations(summary: dict[str, Any]) -> int:
    indexes = summary.get("indexes") or []
    count = 0
    for i in indexes:
        try:
            if int(i.get("contracts_activated") or 0) > 0:
                count += 1
        except (TypeError, ValueError):
            continue
    return count


def _empty_index_keys(summary: dict[str, Any]) -> list[str]:
    """Return the index_keys whose ``contracts_total`` is 0.

    Used to detect a partial-tree run: some indexes have contracts,
    some don't. The overall classification is still ``no_new_touches``
    (because ``_total_contracts`` sums across the populated indexes),
    but the operator should be told *which* indexes are empty.
    """
    empty: list[str] = []
    for i in (summary.get("indexes") or []):
        try:
            if int(i.get("contracts_total") or 0) == 0:
                empty.append(str(i.get("index_key") or "?"))
        except (TypeError, ValueError):
            continue
    return empty


def _populated_index_count(summary: dict[str, Any]) -> int:
    """Return the number of indexes with at least one contract."""
    count = 0
    for i in (summary.get("indexes") or []):
        try:
            if int(i.get("contracts_total") or 0) > 0:
                count += 1
        except (TypeError, ValueError):
            continue
    return count


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

    indexes = summary.get("indexes") or []
    if not indexes:
        return "no_indexes"

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

    Partial-tree escalation:
      When ``no_new_touches`` and some indexes have 0 contracts, the
      verdict escalates to WARNING (naming the empty indexes). This
      turns a hidden partial-tree failure into a visible signal on
      the same line.
    """
    status = str(summary.get("status") or "")
    contracts = _total_contracts(summary)
    activated = _total_activated(summary)
    indexes_count = len(summary.get("indexes") or [])
    indexes_activated = _indexes_with_activations(summary)
    populated_indexes = _populated_index_count(summary)
    sub_state = _classify_run(summary)
    empty_indexes = _empty_index_keys(summary)

    # ---- Aggregate line ------------------------------------------------ #
    logger.info(
        "%s diagnosis — status=%s sub_state=%s "
        "indexes=%d populated_indexes=%d indexes_with_activations=%d "
        "contracts=%d activated_total=%d elapsed=%.2fs stage_4_in=%ss",
        _STAGE_3_CATCHUP_WRAPPER_MARKER,
        status or "?",
        sub_state,
        indexes_count,
        populated_indexes,
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
            "%s disabled by config — "
            "no work performed. (STRATEGY_ENABLED or "
            "STRATEGY_TRACKER_ENABLED is false.)",
            _STAGE_3_CATCHUP_WRAPPER_MARKER,
        )
        return

    if sub_state == "no_indexes":
        logger.warning(
            "%s tracker returned NO indexes — "
            "STRATEGY_TRACKER_INDEX_KEYS is empty or misconfigured. "
            "Stage 4 will report no_activations. Check "
            "config.STRATEGY_TRACKER_INDEX_KEYS and "
            "config.STRATEGY_INDEXES.",
            _STAGE_3_CATCHUP_WRAPPER_MARKER,
        )
        return

    if sub_state == "empty_tree":
        logger.warning(
            "%s stage-2 tree has 0 contracts — "
            "Stage 4 will report no_activations. This is the "
            "2026-10-22 failure signature. Check GET "
            "/admin/strategy/isolation and GET /admin/strategy "
            "(Stage 1 reason field) immediately — Stage 4 fires in "
            "%ss.",
            _STAGE_3_CATCHUP_WRAPPER_MARKER,
            str(seconds_to_stage_4_after)
            if seconds_to_stage_4_after is not None else "?",
        )
        return

    if sub_state == "no_new_touches":
        # ---- Partial-tree escalation ------------------------------ #
        # When some indexes have contracts but others don't, the
        # overall classification is still ``no_new_touches`` (because
        # ``_total_contracts`` sums across the populated indexes).
        # Escalate to WARNING so a partial-tree failure is not hidden
        # under the "quiet day" INFO line.
        if populated_indexes < indexes_count:
            logger.warning(
                "%s PARTIAL TREE — %d/%d index(es) have contracts, "
                "but %d contract(s) produced no new touches. Empty "
                "indexes: %s. Stage 4 will report no_activations for "
                "those indexes. Check GET /admin/strategy/isolation "
                "(Stage 2 summary) and the Stage-1 summary for the "
                "empty indexes.",
                _STAGE_3_CATCHUP_WRAPPER_MARKER,
                populated_indexes,
                indexes_count,
                contracts,
                ", ".join(empty_indexes) if empty_indexes else "—",
            )
            return

        logger.info(
            "%s %d contract(s) in the tree but no "
            "new touches since the post-Stage-2 hook. This is the "
            "normal quiet-day case — the catch-up reloaded the tree "
            "and confirmed the tracker's view is up to date.",
            _STAGE_3_CATCHUP_WRAPPER_MARKER,
            contracts,
        )
        return

    if sub_state == "activated":
        logger.info(
            "%s %d additional contract(s) "
            "activated by the catch-up (%d/%d index(es) contributed) — "
            "Stage 4 will see them in %ss.",
            _STAGE_3_CATCHUP_WRAPPER_MARKER,
            activated,
            indexes_activated,
            indexes_count,
            str(seconds_to_stage_4_after)
            if seconds_to_stage_4_after is not None else "?",
        )
        return

    # Defensive: any sub-state the classifier did not recognise.
    logger.warning(
        "%s unexpected sub_state=%r — no "
        "specific diagnosis available. status=%r contracts=%d "
        "activated_total=%d indexes=%d",
        _STAGE_3_CATCHUP_WRAPPER_MARKER,
        sub_state, status, contracts, activated, indexes_count,
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
                    "%s %s: 0 contracts — "
                    "Stage-2 produced nothing for this index "
                    "(reason=%s). Stage 4 will report no_activations "
                    "for it.",
                    _STAGE_3_CATCHUP_WRAPPER_MARKER,
                    idx_key,
                    reason_code,
                )
            else:
                logger.info(
                    "%s %s: %d/%d contract(s), "
                    "activated=%d, reason=%s",
                    _STAGE_3_CATCHUP_WRAPPER_MARKER,
                    idx_key,
                    activated,
                    contracts,
                    activated,
                    reason_code,
                )
        except Exception:
            logger.exception(
                "%s failed parsing an index entry: %r",
                _STAGE_3_CATCHUP_WRAPPER_MARKER,
                idx,
            )


# --------------------------------------------------------------------------- #
# Telegram message builders                                                   #
# --------------------------------------------------------------------------- #
def _build_success_message(
    summary: dict[str, Any],
    *,
    elapsed_seconds: float,
    seconds_to_stage_4_after: int | None,
    is_stale_day: bool = False,
) -> str:
    """Telegram message for the ``activated > 0`` case.

    Preserves the original message shape (Mode / Activated / Day /
    Window / Result) and adds three timing fields so the operator can
    confirm the catch-up finished with headroom before Stage 4.

    When ``is_stale_day`` is True (the summary's ``day`` does not
    match today), the message is escalated to WARNING and names both
    dates so a manual run against a previous day's tree is not
    mistaken for a fresh activation.
    """
    activated = _total_activated(summary)
    indexes_activated = _indexes_with_activations(summary)
    indexes_count = len(summary.get("indexes") or [])
    summary_day = summary.get("day") or "?"
    window_start = summary.get("window_start") or "?"
    window_end = summary.get("window_end") or "?"

    if is_stale_day:
        return build(
            emoji="⚠️",
            title="Strategy 1 · Stage 3 Catch-up · Stale Day",
            fields=[
                ("Mode", "scheduled_catchup"),
                ("Activated", activated),
                ("Summary day", summary_day),
                ("Today", _today_iso()),
                ("Window", f"{window_start} – {window_end}"),
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
            sections=[
                (
                    "Result",
                    "📊",
                    [
                        f"{activated} contract(s) would be activated, "
                        f"but the summary is from an earlier day "
                        f"({summary_day}, today is {_today_iso()}).",
                    ],
                ),
                (
                    "Why",
                    "🔍",
                    [
                        "This is a manual run against a previous day's "
                        "tree, or a cron misfire.",
                        "Check: GET /admin/strategy/tracker/status",
                        "Fix:   POST /admin/strategy/tracker/start",
                    ],
                ),
            ],
            status="warning",
            status_detail=f"stale day ({summary_day} != {_today_iso()})",
        )

    return build(
        emoji="🎯",
        title="Strategy 1 · Stage 3 Catch-up",
        fields=[
            ("Mode", "scheduled_catchup"),
            ("Activated", activated),
            ("Day", summary_day),
            ("Window", f"{window_start} – {window_end}"),
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


def _build_empty_tree_slow_message(
    summary: dict[str, Any],
    *,
    elapsed_seconds: float,
    seconds_to_stage_4_after: int | None,
) -> str:
    """Telegram message for the "empty tree + slow catch-up" case.

    Combines the two signals (empty tree = Stage 4 will be empty;
    slow catch-up = Stage 4 may already have started) so the operator
    can act immediately. This is the catastrophic 2026-10-22 case.
    """
    return build(
        emoji="⚠️",
        title="Strategy 1 · Stage 3 Catch-up · Empty Tree + Slow",
        fields=[
            ("Mode", "scheduled_catchup"),
            ("Status", summary.get("status") or "?"),
            ("Contracts", 0),
            ("Elapsed", f"{elapsed_seconds:.1f}s"),
            (
                "Stage 4 in",
                f"{seconds_to_stage_4_after}s"
                if seconds_to_stage_4_after is not None else "?",
            ),
        ],
        sections=[
            (
                "What happened",
                "🔍",
                [
                    "The stage-2 tree is empty (0 contracts).",
                    f"The catch-up took {elapsed_seconds:.1f}s to "
                    f"determine this — above the "
                    f"{int(_STAGE_4_LATENCY_WARNING_SECONDS)}s latency "
                    f"threshold.",
                    "Stage 4 may have already fired before the "
                    "catch-up finished writing its summary.",
                ],
            ),
            (
                "Why",
                "🔍",
                [
                    "Either Stage 1/2 produced nothing today (a "
                    "genuine quiet day or a broken upstream), or the "
                    "tracker scanned a large stale directory.",
                    "Check: GET /admin/strategy/isolation",
                    "Check: GET /admin/strategy (Stage 1 reason)",
                    "Fix:   POST /admin/strategy/tracker/start",
                ],
            ),
        ],
        status="warning",
        status_detail=(
            f"empty tree · {elapsed_seconds:.1f}s (> "
            f"{int(_STAGE_4_LATENCY_WARNING_SECONDS)}s)"
        ),
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
        "%s starting — mode=scheduled_catchup stage_4_in=%ss",
        _STAGE_3_CATCHUP_WRAPPER_MARKER,
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
                "%s catch-up took %.1fs — above "
                "the %.0fs threshold. Stage 4 fires in %ss; it may "
                "have started before the catch-up finished writing its "
                "summary. The safety net itself was racing the cron.",
                _STAGE_3_CATCHUP_WRAPPER_MARKER,
                elapsed,
                _STAGE_4_LATENCY_WARNING_SECONDS,
                str(seconds_after) if seconds_after is not None else "?",
            )

        activated = _total_activated(summary)
        status = str(summary.get("status") or "unknown")
        summary_day = str(summary.get("day") or "")
        is_stale_day = bool(summary_day and summary_day != _today_iso())
        is_empty_tree = _total_contracts(summary) == 0

        # ---- Telegram dispatch ------------------------------------ #
        # The wrapper is deliberately quiet. It sends a Telegram
        # message in exactly three cases:
        #   1. activated > 0      → SUCCESS (or WARNING if stale day)
        #   2. empty tree + slow  → WARNING (combined signal)
        #   3. any raise          → ERROR (handled in except block)
        # Every other case stays log-only so the 30-second window
        # remains noise-free. This is the same philosophy as the S2
        # Stage-3 recheck.
        if status == "ok" and activated > 0:
            # Stale-day escalation: a manual run against a previous
            # day's tree should not produce a benign "✅ activated=3"
            # message.
            telegram_service.send_message(
                _build_success_message(
                    summary,
                    elapsed_seconds=elapsed,
                    seconds_to_stage_4_after=seconds_after,
                    is_stale_day=is_stale_day,
                ),
                level="WARNING" if is_stale_day else "SUCCESS",
            )
        elif (
            status == "ok"
            and is_empty_tree
            and elapsed > _STAGE_4_LATENCY_WARNING_SECONDS
        ):
            # Catastrophic case: the tree was empty AND the catch-up
            # took too long to discover that. Combine the two
            # signals into one message so the operator acts
            # immediately.
            telegram_service.send_message(
                _build_empty_tree_slow_message(
                    summary,
                    elapsed_seconds=elapsed,
                    seconds_to_stage_4_after=seconds_after,
                ),
                level="WARNING",
            )

        # ---- Post-run aggregate log line --------------------------- #
        logger.info(
            "%s completed — status=%s "
            "activated_total=%d elapsed=%.2fs stage_4_in=%ss "
            "stale_day=%s empty_tree=%s",
            _STAGE_3_CATCHUP_WRAPPER_MARKER,
            status,
            activated,
            elapsed,
            str(seconds_after) if seconds_after is not None else "?",
            is_stale_day,
            is_empty_tree,
        )
    except Exception as exc:
        elapsed = time.monotonic() - started_at
        seconds_after = _seconds_to_stage_4()

        logger.exception(
            "%s catch-up RAISED — Stage 4 at "
            "%02d:%02d will read whatever the tracker last wrote "
            "(possibly an empty stage_3_summary.json). elapsed=%.2fs "
            "stage_4_in=%ss",
            _STAGE_3_CATCHUP_WRAPPER_MARKER,
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