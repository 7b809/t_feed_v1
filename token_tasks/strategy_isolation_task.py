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

Additional diagnostics (this revision)
--------------------------------------
Building on the 2026-10-22 patch, this revision adds four layers of
defence that close the last remaining silent-failure paths on the
Stage-2 → Stage-3 boundary:

    * **A ``missing_or`` / ``missing_key`` / ``with_or_canonical``
      summary line** on the Stage-2 completion Telegram message. The
      Stage-2 service now counts isolated contracts whose strike
      folder OR is missing (``contracts_missing_or``), how many of
      those are covered by the canonical fallback
      (``contracts_with_or_canonical``), and how many lack a real
      Upstox key (``contracts_missing_key``). These are the exact
      diagnostics that the Oct 22 investigation had to reconstruct
      from the log after the fact. Surfacing them on the completion
      message means the same information reaches the operator's
      phone at 09:17:00, not 09:18:00 when Stage 4 reports
      ``no_activations``.

    * **A "partial_isolated" Telegram branch** when Stage 2
      reported isolations but the missing-OR count is non-trivial.
      Previously the "Success" status was based on ``isolated > 0``
      alone, so a Stage-2 run that isolated 68 contracts with 68
      missing OR files emitted a "✅ Success" message. The new
      branch sends a WARNING with the missing-OR count on the phone
      so the operator can intervene before Stage 4.

    * **An ``isolate_by_side`` field** in the completion message's
      breakdown ("CE-triggered: N, PE-triggered: M"). Whether
      today's isolations were triggered by calls or puts is often
      the first hint that something is off (e.g. 68 PE isolations
      on a bullish day), and this information was previously only
      visible by reading each contract's ``isolation_meta.json``.

    * **An explicit "Stage 2 raised" Telegram branch.** When the
      Stage-2 service itself raises (not just the post-hook), the
      wrapper now names the exception class and states that the
      post-hook was NOT attempted. Previously the "Failed" message
      only named the error, not the consequence for the pipeline.

    * **A ``stage_2_service_raised`` flag** in the post-hook's
      log context. If the Stage-2 service raised *and* the post-hook
      then found a populated tree (e.g. from a previous run), the
      post-hook's per-index breakdown now names the state explicitly
      — so the operator is not misled by a stale-but-populated tree.

Telegram message shape
----------------------
The completion message is now one of three shapes:

    * **Success** — isolated > 0, no missing-OR, no missing-key.
    * **Partial** — isolated > 0, but at least one contract has
      no strike OR (or no key). WARNING.
    * **No isolations** — isolated == 0. WARNING, with the
      per-contract summary in the breakdown section.

Every message carries the same fields (Today / Mode / Catch-up /
Post-market / Pairing) so the operator can always orient themselves
at a glance.
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
def _classify_completion(summary: dict) -> str:
    """Return a completion sub-state string for the Stage-2 summary.

    Values:
        "success"  — isolated > 0 and no missing OR / key.
        "partial"  — isolated > 0 but some contracts have missing OR
                     (that the canonical fallback does *not* cover) or
                     missing key.
        "empty"    — isolated == 0. Stage 4 will be empty for today.
        "disabled" — STRATEGY_ENABLED or STRATEGY_ISOLATION_ENABLED
                     is false.
    """
    status = str(summary.get("status") or "")
    if status == "disabled":
        return "disabled"

    isolated = int(summary.get("isolated") or 0)
    if isolated == 0:
        return "empty"

    missing_or = int(summary.get("contracts_missing_or") or 0)
    with_canonical = int(summary.get("contracts_with_or_canonical") or 0)
    truly_missing_or = max(0, missing_or - with_canonical)
    missing_key = int(summary.get("contracts_missing_key") or 0)

    if truly_missing_or > 0 or missing_key > 0:
        return "partial"
    return "success"


def _build_completion_message(summary: dict) -> str:
    """Build the Stage-2 completion message.

    Branches on ``_classify_completion()`` so the phone sees the
    correct status:
      * "success" — SUCCESS, green
      * "partial" — WARNING, with the missing-OR / missing-key counts
      * "empty"   — WARNING, with a pointer to the Stage-1 reason
      * "disabled" — INFO
    """
    completion_state = _classify_completion(summary)
    isolated = int(summary.get("isolated") or 0)
    pairs = int(summary.get("pairs_isolated") or 0)
    missing_or = int(summary.get("contracts_missing_or") or 0)
    with_canonical = int(summary.get("contracts_with_or_canonical") or 0)
    truly_missing_or = max(0, missing_or - with_canonical)
    missing_key = int(summary.get("contracts_missing_key") or 0)
    by_side = summary.get("isolate_by_side") or {}
    ce_count = int(by_side.get("CE") or 0)
    pe_count = int(by_side.get("PE") or 0)

    # ---- Common fields --------------------------------------------- #
    fields = [
        ("Today", summary.get("today")),
        ("Mode", summary.get("mode")),
        ("Catch-up", "yes" if summary.get("catchup") else "no"),
        ("Post-market", "yes" if summary.get("post_market") else "no"),
        ("Pairing", summary.get("pairing")),
    ]

    # ---- Per-index breakdown --------------------------------------- #
    index_lines: list[str] = []
    for idx in summary.get("indexes") or []:
        ticker = idx.get("ticker") or "?"
        index_lines.append(
            f"• {ticker}: {idx.get('isolated')}/{idx.get('evaluated')} "
            f"isolated · {idx.get('pairs_isolated')} pairs · "
            f"{idx.get('errors')} errors"
        )

    # ---- Aggregate health lines ------------------------------------ #
    health_lines: list[str] = [
        f"Isolated (contracts): {isolated}",
        f"Isolated (strike pairs): {pairs}",
        f"Skipped: {summary.get('skipped')} · Errors: {summary.get('errors')}",
        f"CE-triggered: {ce_count} · PE-triggered: {pe_count}",
    ]
    warnings: list[str] = []

    if truly_missing_or > 0:
        examples = summary.get("missing_or_examples") or []
        warnings.append(
            f"⚠️ {truly_missing_or} contract(s) have NO opening_range.json "
            f"in their strike folder AND no canonical fallback — the "
            f"S1/S2 trackers will skip them and Stage 4 will report "
            f"no_activations for them."
        )
        if examples:
            warnings.append(
                "   examples: " + ", ".join(str(x) for x in examples[:3])
            )

    if with_canonical > 0 and truly_missing_or == 0:
        # Canonical fallback covers them — informational only.
        health_lines.append(
            f"Strike-folder OR missing but canonical present: "
            f"{with_canonical} (trackers will use the fallback)"
        )

    if missing_key > 0:
        examples = summary.get("missing_key_examples") or []
        warnings.append(
            f"⚠️ {missing_key} contract(s) have NO upstox_instrument_key "
            f"— they cannot be subscribed or traded."
        )
        if examples:
            warnings.append(
                "   examples: " + ", ".join(str(x) for x in examples[:3])
            )

    # ---- Dispatch per completion state ---------------------------- #
    if completion_state == "disabled":
        return build(
            emoji="🚫",
            title="Strategy 1 · Stage 2 · Disabled",
            fields=fields + [
                (
                    "Reason",
                    "STRATEGY_ENABLED or STRATEGY_ISOLATION_ENABLED is "
                    "false",
                ),
            ],
            status="info",
        )

    if completion_state == "empty":
        return build(
            emoji="📭",
            title="Strategy 1 · Stage 2 · No Isolations",
            fields=fields,
            sections=[
                (
                    "Breakdown",
                    "📊",
                    index_lines or ["No per-index data available."],
                ),
                (
                    "Pipeline health",
                    "🔧",
                    [
                        "⚠️ 0 contracts isolated today. Stage 4 will "
                        "report no_activations for today.",
                        "Per-contract reasons are recorded in each "
                        "strike's isolation_meta.json — check the "
                        "stage-1 tree for last_day_cross / today's "
                        "first-candle cross.",
                    ],
                ),
            ],
            status="warning",
            status_detail="0 contracts isolated",
        )

    if completion_state == "partial":
        return build(
            emoji="⚠️",
            title="Strategy 1 · Stage 2 · Partial",
            fields=fields,
            sections=[
                (
                    "Breakdown",
                    "📊",
                    index_lines or ["No per-index data available."],
                ),
                (
                    "Pipeline health",
                    "🔧",
                    [*warnings, *health_lines],
                ),
            ],
            status="warning",
            status_detail=(
                f"{isolated} isolated · {truly_missing_or} missing OR · "
                f"{missing_key} missing key"
            ),
        )

    # completion_state == "success"
    return build(
        emoji="🔒",
        title="Strategy 1 · Stage 2 · Completed",
        fields=fields,
        sections=[
            (
                "Breakdown",
                "📊",
                index_lines or ["No per-index data available."],
            ),
            (
                "Pipeline health",
                "🔧",
                health_lines,
            ),
        ],
        status="success",
        status_detail=f"{isolated} contracts isolated",
    )


def _build_failed_message(exc: BaseException) -> str:
    """Build the Stage-2 raised message.

    Names the exception class explicitly and states the consequence
    for the post-hook: the post-hook is NOT attempted, so Stage 4 at
    09:18:00 will read whatever the tracker last wrote (possibly an
    empty or stale stage_3_summary.json).
    """
    return build(
        emoji="❌",
        title="Strategy 1 · Stage 2 · Failed",
        fields=[
            ("Error", f"{type(exc).__name__}: {exc}"),
        ],
        sections=[
            (
                "Consequence",
                "⚠️",
                [
                    "The post-Stage-2 Stage-3 catch-up hook was NOT "
                    "attempted.",
                    "Stage 4 at 09:18:00 IST will read whatever "
                    "stage_3_summary.json the tracker last wrote — "
                    "possibly an empty or stale file.",
                    "If Stage 4 has not fired yet, recover by running "
                    "POST /admin/strategy/tracker/start and then "
                    "POST /admin/strategy/tracker/status to confirm.",
                ],
            ),
        ],
        status="failed",
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
    stage_2_service_raised: bool = False,
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

    The ``stage_2_service_raised`` flag is set by the caller when the
    Stage-2 service raised in *this* run. It is used to disambiguate
    "the tree is populated because Stage 2 just succeeded" from "the
    tree is populated because Stage 2 succeeded in an earlier run and
    raised this time". Without this flag a raised Stage-2 followed by
    a populated-tree post-hook would look identical to a healthy run.
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
        # If the Stage-2 service raised in this run, the tree may be
        # populated by a *previous* run — the "no new touches" state
        # then means "nothing changed since before this run raised",
        # which is a very different diagnostic from a quiet day.
        if stage_2_service_raised:
            logger.warning(
                "[stage_2 hook] catch-up evaluated %d contract(s) across "
                "%d index(es) and activated %d — BUT Stage 2 raised in "
                "this run. The populated tree is from a PREVIOUS run. "
                "Stage 4 will act on stale data unless Stage 2 is "
                "re-run successfully. Fix Stage 2 first.",
                total_contracts,
                len(indexes),
                activated,
            )
            return

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

    # Track whether the Stage-2 service itself raised. The post-hook
    # uses this to disambiguate "populated because Stage 2 just
    # succeeded" from "populated because Stage 2 succeeded in an
    # earlier run and raised this time".
    stage_2_service_raised = False
    summary: dict = {}

    try:
        summary = strategy_isolation_service.run(reason="scheduled")
    except Exception as exc:
        stage_2_service_raised = True
        logger.exception("Strategy 1 stage 2 service raised")
        telegram_service.send_message(
            _build_failed_message(exc),
            level="ERROR",
        )
        # Do NOT attempt the post-hook when the Stage-2 service itself
        # raised — the tree may be in an intermediate state and running
        # the tracker against it would produce a misleading summary.
        logger.info(
            "[stage_2 hook] skipped — the Stage-2 service raised; the "
            "post-Stage-2 catch-up was NOT attempted. Stage 4 will read "
            "whatever the tracker last wrote (possibly stale)."
        )
        return

    # ---- Completion message ---------------------------------------- #
    isolated = int(summary.get("isolated") or 0)
    completion_state = _classify_completion(summary)
    telegram_level = (
        "SUCCESS" if completion_state == "success"
        else "INFO" if completion_state == "disabled"
        else "WARNING"
    )
    telegram_service.send_message(
        _build_completion_message(summary), level=telegram_level
    )
    logger.info(
        "Strategy 1 stage 2 completed: isolated=%d pairs=%d "
        "missing_or=%s with_canonical=%s missing_key=%s by_side=CE:%s,PE:%s",
        isolated,
        int(summary.get("pairs_isolated") or 0),
        summary.get("contracts_missing_or"),
        summary.get("contracts_with_or_canonical"),
        summary.get("contracts_missing_key"),
        (summary.get("isolate_by_side") or {}).get("CE", 0),
        (summary.get("isolate_by_side") or {}).get("PE", 0),
    )

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
            "activated_total=%s contracts_total=%s indexes=%d",
            tracker_summary.get("status"),
            tracker_summary.get("activated_total"),
            tracker_summary.get("contracts_total"),
            len(tracker_summary.get("indexes") or []),
        )
        _log_catchup_outcome(
            tracker_summary,
            seconds_to_stage_4=seconds_to_stage4,
            stage_2_service_raised=stage_2_service_raised,
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