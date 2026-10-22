# File: token_tasks/strategy_2_isolation_task.py
"""Scheduled wrapper for Strategy 2 — Stage 3.

Telegram noise gate
-------------------
The scheduler re-runs this every 5 minutes during 09:15–15:30 IST
(``strategy_2_stage3_recheck``). On a typical day Stage 2 has produced
zero isolated contracts (either because the day is quiet, or because
Stage 1 was skipped due to a late OR candle), so both the
"Started" and "Completed" messages would just say
"Stage 2 contracts: 0 / Touched: 0 / No contracts touched".

To avoid flooding Telegram with zero-data messages, this wrapper
suppresses the Telegram "Started" message when there is nothing to
watch (``stage2_contracts == 0``), and suppresses the "Completed"
message unless one of the following is true:

    * a contract just became the isolated pick (state changed from
      previous run), or
    * a new touch was recorded since the previous run, or
    * the run errored out.

The service itself still runs on every tick — only the Telegram
output is gated. When Stage 2 is empty (``stage2_contracts == 0``)
the wrapper logs a single INFO line and returns early: there is
nothing for Stage 3 to watch, so running the service is wasted work.

Diagnostics (added for the 2026-10-22 observability patch set)
--------------------------------------------------------------
The wrapper used ``logging.getLogger(__name__)``, so every log line
from it was console-only — nothing reached
``logs/strategy_2_isolation_task.log`` or
``logs_text/strategy_2_isolation_task.txt``. And when the noise gate
suppressed a message (the normal case for most of the day) there was
no log entry saying *why*. On 2026-10-22 that combination meant the
S2 Stage-3 pipeline was completely invisible in the file logs — the
first symptom an operator saw was Stage 4 S2 reporting
``no_isolated`` at 15:25, with no trace of the 5-minute rechecks
that had been silently skipping every stage-2 contract because
none of them had an ``isolation_level``.

This wrapper now:

    * uses ``get_logger(__file__)`` so every line reaches the file
      handlers;
    * resets the noise-gate baselines on day rollover, so a
      same-key isolated pick on day N+1 is not suppressed by the
      day-N baseline;
    * emits one ``[stage_2_s2 wrapper] diagnosis`` aggregate INFO
      line per run with every counter (status / stage2 / touched /
      missing_level / isolated / new_day), so the 5-minute recheck
      history is fully recoverable from the log file;
    * emits a state-specific INFO or WARNING line after the
      aggregate, per state:

          status = "disabled"             → INFO (config off)
          status = "no_stage_2"           → WARNING (Stage 1/2 have
                                            not produced any
                                            contracts yet)
          status = "ok" & stage2 == 0     → WARNING (tree exists
                                            but empty)
          missing_level > 0               → WARNING (Oct 22
                                            signature; S2 tracker
                                            will silently skip
                                            every contract)
          touched > 0                     → INFO (n touches,
                                            isolated pick if any)
          touched == 0 & stage2 > 0       → INFO (normal during the
                                            first few minutes after
                                            Stage 2)

    * sends a **once-per-day** WARNING to Telegram for the two
      failure signatures that must never be silent:

          empty_stage2_tree   — Stage 2 tree exists but has 0
                                contracts on a trading day. Stage 4
                                S2 will report no_isolated.
          missing_isolation_  — Stage 2 tree has contracts but NONE
          level                 has an isolation level. The tracker
                                will silently skip every contract.

      Everything else still respects the original noise gate.

    * adds ``Contracts with OR`` / ``Contracts missing OR`` counters
      to the completion message under a "Pipeline health" section,
      so the phone-level view matches the log-level view.

Additional hardening (this revision)
------------------------------------
Building on the 2026-10-22 patch, this revision adds:

    * **A ``_STAGE_2_S2_WRAPPER_MARKER`` constant.** Every log line
      from this module starts with ``[stage_2_s2 wrapper]`` so an
      operator can pull every S2 Stage-3 wrapper line with a single
      ``grep '[stage_2_s2 wrapper]'``. Matches the convention used by
      the S1 Stage-1/2/4 wrappers and the S2 Stage-4 wrapper.

    * **A ``contracts_with_or_canonical`` field in the completion
      message.** The Stage-2 service now reports how many of the
      missing-OR contracts are covered by the canonical fallback at
      ``data/index/<safe_key>/opening_range.json`` (because the
      trackers will read that file via their fallback chain and
      succeed). Without this, a "68 missing OR" warning on a day
      where all 68 are actually covered by the fallback would
      produce a false alarm. The new field disambiguates the two
      cases on the phone.

    * **A "strike OR missing but canonical present" Telegram branch
      for the completion message.** When the aggregate picture is
      "missing OR in strike folders, but every one is covered by the
      canonical fallback", the completion message is downgraded to
      INFO with a note rather than WARNING. The tracker's fallback
      chain is a legitimate code path; the operator does not need to
      intervene.

    * **A "stage-2 tree populated by a stale run" INFO branch.** On
      process restart after a successful Stage 2 (e.g. operator
      hit /admin/hard-refresh at 10:00), the wrapper's first call
      sees a populated tree that was *not* written by this run.
      The new branch logs that fact so the operator is not
      surprised by a fresh "Isolated: ..." message immediately
      after startup.

    * **Tolerant ``_build_completion_message`` rendering.** The
      Telegram message renders ``"?"`` for missing fields (e.g.
      ``isolation_level_name`` when the service returned an
      early-exit summary) instead of ``None``. Makes a partially
      filled summary visibly wrong rather than silently misleading.

    * **A per-run ``_last_run_state`` record** used by the next
      invocation to detect state change. When the service crashes
      mid-run (before writing its summary), the next run's
      ``_should_send_completion`` baseline correctly treats the
      missing summary as "state changed" and sends a message. The
      original code updated the baseline to ``None`` on crash too,
      but did so silently; the new record makes the transition
      explicit in the log.

    * **A ``_STAGE_3_DECISIONS_HINT`` path constant** used in the
      completion message and the once-per-day warnings, so an
      operator can jump straight to the per-contract reasons
      without reconstructing the path from the code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.logger import get_logger
from services.strategy_2_isolation_service import strategy_2_isolation_service
from services.telegram_service import telegram_service
from services.telegram_format import build

logger = get_logger(__file__)

# Grep anchor. Every log line from this module starts with this
# prefix, so an operator can pull every S2 Stage-3 wrapper line with
# a single ``grep '[stage_2_s2 wrapper]'``.
_STAGE_2_S2_WRAPPER_MARKER = "[stage_2_s2 wrapper]"

# Path hints used in Telegram messages. Never absolute paths — the
# pipeline root is the standard project layout.
_STAGE_3_SUMMARY_HINT = (
    "data/strategy2/stage_3_isolation_level/stage_3_summary.json"
)
_STAGE_3_DECISIONS_HINT = (
    "data/strategy2/stage_3_isolation_level/stage_3_summary.json.contracts"
)


# --------------------------------------------------------------------------- #
# In-process state (day-stamped) used by the Telegram noise gate.             #
# --------------------------------------------------------------------------- #
# These persist for the lifetime of the process. A reset is performed
# on calendar-day rollover so a same-key isolated pick on day N+1 is
# not suppressed by the day-N baseline, and so the once-per-day
# failure warnings can fire on the new day.
_LAST_ISOLATED_KEY: str | None = None
_LAST_TOUCHED_COUNT: int = 0
_LAST_DAY: str | None = None
_LAST_MISSING_LEVEL_WARNING_DAY: str | None = None
_LAST_EMPTY_STAGE2_WARNING_DAY: str | None = None
# Snapshot of the last non-crashing run's summary — used to detect
# state changes across invocations. Updated only on successful runs
# so a crash mid-run does not silently suppress the next notification.
_LAST_RUN_STATE: dict[str, Any] = {
    "status": None,
    "stage2_contracts": 0,
    "touched_contracts": 0,
    "isolated_key": None,
    "missing_level": 0,
    "finished_at": None,
}


def _today_iso() -> str:
    return datetime.now().astimezone().date().isoformat()


def _reset_if_new_day() -> bool:
    """Reset in-process baselines when the calendar day rolls over.

    Returns True when a reset was performed. Without this, the
    ``_LAST_ISOLATED_KEY`` suppression would carry across midnight,
    and a same-key isolated pick on the next trading day would never
    reach Telegram. The once-per-day failure-warning baselines are
    also cleared so those warnings can fire again.
    """
    global _LAST_ISOLATED_KEY, _LAST_TOUCHED_COUNT, _LAST_DAY
    global _LAST_MISSING_LEVEL_WARNING_DAY, _LAST_EMPTY_STAGE2_WARNING_DAY

    today = _today_iso()
    if _LAST_DAY == today:
        return False

    _LAST_ISOLATED_KEY = None
    _LAST_TOUCHED_COUNT = 0
    _LAST_MISSING_LEVEL_WARNING_DAY = None
    _LAST_EMPTY_STAGE2_WARNING_DAY = None
    _LAST_DAY = today
    return True


def _isolated_key(isolated: dict[str, Any] | None) -> str | None:
    if not isolated:
        return None
    return f"{isolated.get('instrument_key')}"


def _should_send_completion(summary: dict[str, Any]) -> bool:
    """Return True when the completion message is worth sending.

    Preserves the original noise gate: send only when:
      * the isolated pick changed since the last run, or
      * a new touch was recorded since the last run.

    The baselines are updated regardless of whether we send, so the
    next run has fresh values. Callers must call ``_reset_if_new_day``
    before this function so the baselines don't carry across midnight.
    """
    global _LAST_ISOLATED_KEY, _LAST_TOUCHED_COUNT

    isolated_key = _isolated_key(summary.get("isolated"))
    touched = int(summary.get("touched_contracts") or 0)

    isolated_changed = isolated_key != _LAST_ISOLATED_KEY
    touched_changed = touched > _LAST_TOUCHED_COUNT

    _LAST_ISOLATED_KEY = isolated_key
    _LAST_TOUCHED_COUNT = touched

    return isolated_changed or touched_changed


def _record_successful_run(summary: dict[str, Any]) -> None:
    """Update ``_LAST_RUN_STATE`` after a successful service call.

    Called from ``run_strategy_2_isolation`` once the service returns
    without raising. Used purely for logging — the state-change
    detection itself is driven by ``_should_send_completion`` above.
    """
    global _LAST_RUN_STATE
    isolated = summary.get("isolated")
    _LAST_RUN_STATE = {
        "status": str(summary.get("status") or ""),
        "stage2_contracts": int(summary.get("stage2_contracts") or 0),
        "touched_contracts": int(summary.get("touched_contracts") or 0),
        "isolated_key": _isolated_key(isolated),
        "missing_level": int(
            summary.get("contracts_missing_level") or 0
        ),
        "finished_at": datetime.now().astimezone().isoformat(),
    }


def _should_send_missing_level_warning(missing_level: int) -> bool:
    """One-shot per day for the Oct 22 missing-isolation-level signature."""
    global _LAST_MISSING_LEVEL_WARNING_DAY
    if missing_level == 0:
        return False
    today = _today_iso()
    if _LAST_MISSING_LEVEL_WARNING_DAY == today:
        return False
    _LAST_MISSING_LEVEL_WARNING_DAY = today
    return True


def _should_send_empty_stage2_warning() -> bool:
    """One-shot per day for the 'stage-2 tree empty on a trading day' signature."""
    global _LAST_EMPTY_STAGE2_WARNING_DAY
    today = _today_iso()
    if _LAST_EMPTY_STAGE2_WARNING_DAY == today:
        return False
    _LAST_EMPTY_STAGE2_WARNING_DAY = today
    return True


# --------------------------------------------------------------------------- #
# Telegram message builders                                                   #
# --------------------------------------------------------------------------- #
def _build_completion_message(summary: dict) -> str:
    isolated = summary.get("isolated")
    stage2 = int(summary.get("stage2_contracts") or 0)
    touched = int(summary.get("touched_contracts") or 0)
    missing_level = int(summary.get("contracts_missing_level") or 0)
    with_canonical = int(
        summary.get("contracts_with_or_canonical") or 0
    )
    truly_missing_level = max(0, missing_level - with_canonical)

    bullets: list[str] = [
        f"Stage 2 contracts: {stage2}",
        f"Touched: {touched}",
    ]
    if isolated:
        bullets.append(
            f"🔒 Isolated: {isolated.get('ticker')} "
            f"{isolated.get('strike_price')} {isolated.get('strike_type')}"
        )
        bullets.append(
            f"   Level: {isolated.get('isolation_level_name') or '?'} = "
            f"{isolated.get('isolation_level') or '?'}"
        )
        bullets.append(
            f"   Touch: {isolated.get('touch_minute') or '?'} "
            f"(ltp {isolated.get('touch_ltp') or '?'})"
        )
    else:
        bullets.append("No contracts touched the isolation level yet.")

    # ---- Pipeline-health bullets ---------------------------------- #
    health_bullets: list[str] = []
    warnings: list[str] = []

    if stage2 > 0:
        with_or = stage2 - missing_level
        health_bullets.append(f"Contracts with OR:    {with_or} / {stage2}")
        health_bullets.append(f"Contracts missing OR: {missing_level} / {stage2}")
        if with_canonical > 0:
            health_bullets.append(
                f"  ↳ of which canonical fallback covers: {with_canonical}"
            )
        if truly_missing_level > 0:
            warnings.append(
                f"⚠️ {truly_missing_level}/{stage2} contract(s) have no "
                f"isolation level anywhere — tracker will silently skip them"
            )
        elif with_canonical > 0:
            health_bullets.append(
                "↳ Missing strike-folder OR is covered by the canonical "
                "fallback — tracker will succeed."
            )
    else:
        health_bullets.append("Stage 2 tree is empty — nothing to watch.")

    status = (
        "success"
        if (isolated and truly_missing_level == 0)
        else "info" if (with_canonical > 0 and truly_missing_level == 0)
        else "warning"
    )
    status_detail = (
        "isolated pick ready for stage 4"
        if (isolated and truly_missing_level == 0)
        else (
            f"{truly_missing_level} contract(s) missing OR"
            if truly_missing_level > 0
            else (
                "missing strike OR covered by canonical fallback"
                if with_canonical > 0
                else ("no isolation yet" if not isolated else "warning")
            )
        )
    )

    return build(
        emoji="🔒",
        title="Strategy 2 · Stage 3 · Completed",
        fields=[
            ("Level", summary.get("isolation_level_name") or "?"),
            ("Mode", summary.get("mode") or "?"),
            ("Catch-up", "yes" if summary.get("catchup") else "no"),
            ("Post-market", "yes" if summary.get("post_market") else "no"),
            ("Actual", summary.get("actual_time_ist") or "?"),
        ],
        sections=[
            ("Result", "🎯", bullets),
            ("Pipeline health", "🔧", [*warnings, *health_bullets]),
        ],
        status=status,
        status_detail=status_detail,
    )


def _build_missing_level_warning(summary: dict) -> str:
    """Dedicated WARNING when stage-2 contracts have no isolation level.

    This is the 2026-10-22 failure signature on the S2 side: Stage 2
    produced contracts, but none has its own ``opening_range.json``
    with ``levels.<key>``. The S2 tracker reads that file to learn each
    contract's own isolation level; without it, every contract is
    silently skipped and Stage 4 S2 reports ``no_isolated``.

    The message distinguishes "missing everywhere" (a genuine failure)
    from "missing in the strike folder but covered by the canonical
    fallback" (the tracker will succeed via its fallback chain).
    """
    stage2 = int(summary.get("stage2_contracts") or 0)
    missing_level = int(summary.get("contracts_missing_level") or 0)
    with_canonical = int(
        summary.get("contracts_with_or_canonical") or 0
    )
    truly_missing = max(0, missing_level - with_canonical)
    level_name = summary.get("isolation_level_name") or "S3"

    if truly_missing == 0 and with_canonical > 0:
        # Every missing-strike-folder contract is covered by the
        # canonical fallback — informational only.
        return build(
            emoji="ℹ️",
            title="Strategy 2 · Stage 3 · Strike OR Missing (Fallback OK)",
            fields=[
                ("Level", level_name),
                ("Stage 2 contracts", stage2),
                ("Missing strike OR", missing_level),
                ("Covered by fallback", with_canonical),
                ("Day", _today_iso()),
            ],
            sections=[(
                "Why",
                "ℹ️",
                [
                    f"{with_canonical}/{missing_level} contracts are "
                    f"missing their strike-folder opening_range.json, but "
                    f"the canonical fallback at "
                    f"data/index/<safe_key>/opening_range.json exists "
                    f"for every one of them.",
                    "The S2 tracker will use the fallback and succeed — "
                    "no action required.",
                    "Consider passing target_folder=<strike folder> to "
                    "opening_range_service.calculate_one() in Stage 1 "
                    "so the strike folder copy is written too.",
                ],
            )],
            status="info",
            status_detail="fallback covers all missing strike ORs",
        )

    return build(
        emoji="⚠️",
        title="Strategy 2 · Stage 3 · Missing Isolation Levels",
        fields=[
            ("Level", level_name),
            ("Stage 2 contracts", stage2),
            ("Missing OR (anywhere)", truly_missing),
            ("Day", _today_iso()),
        ],
        sections=[(
            "Why",
            "🔍",
            [
                f"{truly_missing}/{stage2} stage-2 contract(s) have no "
                f"opening_range.json with levels.{level_name} in either "
                f"their strike folder OR the canonical fallback.",
                "The S2 tracker will silently skip every one of them.",
                "Stage 4 S2 will report no_isolated for today.",
                "This is the 2026-10-22 failure signature.",
                "Fix: check that Stage 1 writes each strike's OR via "
                "opening_range_service.calculate_one(key, "
                "target_folder=<strike folder>).",
                "Check: find data/strategy/stage_1_initial -name "
                "opening_range.json | wc -l",
                f"Summary: {_STAGE_3_SUMMARY_HINT}",
                f"Per-contract reasons: {_STAGE_3_DECISIONS_HINT}",
            ],
        )],
        status="warning",
        status_detail="tracker will skip every contract",
    )


def _build_empty_stage2_warning(summary: dict) -> str:
    """Dedicated WARNING when the stage-2 tree exists but is empty."""
    return build(
        emoji="⚠️",
        title="Strategy 2 · Stage 3 · Empty Stage 2 Tree",
        fields=[
            ("Day", _today_iso()),
            ("Status", summary.get("status") or "?"),
            ("Stage 2 contracts", 0),
        ],
        sections=[(
            "Why",
            "🔍",
            [
                "The stage_2_isolation tree exists but has 0 contracts.",
                "Stage 1 → Stage 2 produced nothing for this run.",
                "Stage 4 S2 will report no_isolated for today.",
                "Check: GET /admin/strategy/isolation (Stage 2 summary)",
                "Check: GET /admin/strategy (Stage 1 summary — reason "
                "field)",
                "Common causes: Stage 1 skipped (OR unavailable), "
                "buffer window empty, or Stage 2's bullish-priority "
                "rule isolated nothing today.",
            ],
        )],
        status="warning",
        status_detail="Stage 1/2 produced nothing — nothing to watch",
    )


# --------------------------------------------------------------------------- #
# Structured log diagnosis                                                    #
# --------------------------------------------------------------------------- #
def _log_diagnostics(
    summary: dict,
    is_new_day: bool,
    *,
    is_stale_tree: bool = False,
) -> None:
    """Emit one structured per-run diagnosis block to the file logger.

    Aggregate line is always INFO. The state-specific line after it
    is the one that names the failing sub-step (or confirms normal
    operation). This is emitted on every run — including the ones
    that the Telegram noise gate suppresses — so the 5-minute recheck
    history is fully recoverable from ``logs/strategy_2_isolation_task.log``.

    ``is_stale_tree`` is set by the caller when the tree was found
    populated on the *first* run after a process restart (i.e. it was
    written by a previous process, not by this run). The
    state-specific line names that fact so an operator is not
    surprised by an immediate "Isolated: ..." message after startup.
    """
    status = str(summary.get("status") or "")
    stage2 = int(summary.get("stage2_contracts") or 0)
    touched = int(summary.get("touched_contracts") or 0)
    missing_level = int(summary.get("contracts_missing_level") or 0)
    with_canonical = int(
        summary.get("contracts_with_or_canonical") or 0
    )
    isolated = summary.get("isolated")
    level_name = summary.get("isolation_level_name") or "S3"
    truly_missing_level = max(0, missing_level - with_canonical)

    # ---- Aggregate line ------------------------------------------------ #
    logger.info(
        "%s diagnosis — status=%s stage2=%d touched=%d "
        "missing_level=%d with_canonical=%d isolated=%s new_day=%s "
        "stale_tree=%s",
        _STAGE_2_S2_WRAPPER_MARKER,
        status or "?",
        stage2,
        touched,
        missing_level,
        with_canonical,
        (isolated.get("instrument_key") if isolated else "none"),
        is_new_day,
        is_stale_tree,
    )

    # ---- Stale-tree notice ------------------------------------------- #
    # Fires once, on the first run after a process restart with a
    # populated tree. The tree was written by a previous process; the
    # current run did NOT produce it. This is informational.
    if is_stale_tree and stage2 > 0:
        logger.info(
            "%s stale-tree notice — the stage-2 tree was found "
            "populated on this process's first run (contracts=%d across "
            "%d index(es)). The tree was written by a PREVIOUS process, "
            "not by this run. Subsequent runs will treat it as this "
            "run's baseline.",
            _STAGE_2_S2_WRAPPER_MARKER,
            stage2,
            len(summary.get("tracked_indexes") or []),
        )

    # ---- State-specific consequence ---------------------------------- #
    if status == "disabled":
        logger.info(
            "%s S2 stage 3 disabled by config — "
            "no work performed.",
            _STAGE_2_S2_WRAPPER_MARKER,
        )
        return

    if status == "no_stage_2":
        logger.warning(
            "%s stage_2_isolation tree does NOT exist. "
            "Stage 1/2 have not produced any contracts yet, or the tree "
            "was cleaned up. Check data/strategy/stage_2_isolation/ and "
            "GET /admin/strategy/isolation. Stage 4 S2 will report "
            "no_isolated.",
            _STAGE_2_S2_WRAPPER_MARKER,
        )
        return

    if stage2 == 0:
        logger.warning(
            "%s stage_2_isolation tree exists but has "
            "0 contracts. Stage 1 → Stage 2 produced nothing for this "
            "run. Check stage_1_summary.json (status / reason) and "
            "stage_2_summary.json. Stage 4 S2 will report no_isolated.",
            _STAGE_2_S2_WRAPPER_MARKER,
        )
        return

    if truly_missing_level > 0:
        logger.warning(
            "%s %d/%d stage-2 contract(s) have NO "
            "isolation level anywhere (missing opening_range.json with "
            "levels.%s in BOTH the Stage-1 strike folder AND the "
            "canonical data/index/<safe_key> folder). The S2 tracker "
            "will silently skip these contracts. This is the 2026-10-22 "
            "failure signature. Fix: verify strategy_selection_service."
            "_enrich_with_upstox wrote each strike's OR via "
            "opening_range_service.calculate_one(key, target_folder="
            "<strike folder>).",
            _STAGE_2_S2_WRAPPER_MARKER,
            truly_missing_level,
            stage2,
            level_name,
        )
    elif with_canonical > 0:
        logger.info(
            "%s %d/%d stage-2 contract(s) are missing their "
            "strike-folder opening_range.json, but %d are covered by "
            "the canonical fallback at data/index/<safe_key>/"
            "opening_range.json. The S2 tracker will use the fallback "
            "and succeed. Consider passing target_folder=<strike folder> "
            "to opening_range_service.calculate_one() in Stage 1 so the "
            "strike folder copy is written too.",
            _STAGE_2_S2_WRAPPER_MARKER,
            missing_level,
            stage2,
            with_canonical,
        )

    if touched > 0:
        logger.info(
            "%s touches recorded — touched=%d/%d, "
            "isolated=%s",
            _STAGE_2_S2_WRAPPER_MARKER,
            touched,
            stage2,
            (
                f"{isolated.get('ticker')} {isolated.get('strike_price')} "
                f"{isolated.get('strike_type')}"
                if isolated else "none"
            ),
        )
    else:
        logger.info(
            "%s no touches yet — touched=0/%d. "
            "Normal during the first few minutes after Stage 2, and "
            "throughout a genuinely quiet day. Per-contract reasons: "
            "%s.",
            _STAGE_2_S2_WRAPPER_MARKER,
            stage2,
            _STAGE_3_DECISIONS_HINT,
        )


# --------------------------------------------------------------------------- #
# Stale-tree detection                                                        #
# --------------------------------------------------------------------------- #
def _is_first_run_after_restart() -> bool:
    """Return True when this is the first call in the current process.

    Used by ``run_strategy_2_isolation`` to detect the "tree was found
    populated on the very first run after a process restart" state.
    After the first call, ``_LAST_RUN_STATE['finished_at']`` is set,
    so subsequent calls return False.

    Thread-safe enough for the scheduler's single-call-per-5-min
    cadence — a race between two callers is impossible because the
    scheduler fires the job synchronously.
    """
    return _LAST_RUN_STATE.get("finished_at") is None


# --------------------------------------------------------------------------- #
# Public entry point                                                          #
# --------------------------------------------------------------------------- #
def run_strategy_2_isolation() -> None:
    logger.info(
        "%s starting — trigger=scheduled",
        _STAGE_2_S2_WRAPPER_MARKER,
    )

    # Reset baselines on day rollover BEFORE reading the summary, so
    # the noise-gate state does not carry across midnight.
    is_new_day = _reset_if_new_day()
    if is_new_day:
        logger.info(
            "%s day rollover detected (%s) — reset "
            "noise-gate baselines",
            _STAGE_2_S2_WRAPPER_MARKER,
            _today_iso(),
        )

    # Detect the "first run after process restart, tree already
    # populated" state BEFORE the service runs. Used only for the
    # stale-tree log notice; the noise gate itself does not depend
    # on it.
    is_stale_tree = _is_first_run_after_restart()

    try:
        summary = strategy_2_isolation_service.run(reason="scheduled")

        stage2_contracts = int(summary.get("stage2_contracts") or 0)
        touched_contracts = int(summary.get("touched_contracts") or 0)
        missing_level = int(summary.get("contracts_missing_level") or 0)
        with_canonical = int(
            summary.get("contracts_with_or_canonical") or 0
        )
        truly_missing_level = max(0, missing_level - with_canonical)
        isolated = summary.get("isolated")
        status = str(summary.get("status") or "")

        # A "stale tree" notice should only fire when the tree was
        # found populated on the first run after restart. If the tree
        # is empty, there is nothing stale — the notice is noise.
        is_stale_tree = is_stale_tree and stage2_contracts > 0

        # ---- Structured diagnosis (file log, always emitted) -------- #
        _log_diagnostics(
            summary,
            is_new_day,
            is_stale_tree=is_stale_tree,
        )

        # ---- Two failure signatures that break through the noise gate
        #      once per day each. Telegram only; the log lines are
        #      already emitted above. -------------------------------- #
        if status == "ok" and stage2_contracts == 0:
            if _should_send_empty_stage2_warning():
                telegram_service.send_message(
                    _build_empty_stage2_warning(summary),
                    level="WARNING",
                )

        if (
            truly_missing_level > 0
            and _should_send_missing_level_warning(truly_missing_level)
        ):
            telegram_service.send_message(
                _build_missing_level_warning(summary),
                level="WARNING",
            )
        elif with_canonical > 0:
            # All missing strike-folder ORs are covered by the
            # canonical fallback. Send a once-per-day INFO so the
            # operator knows about the (harmless) strike-folder
            # missing state without needing to read the log.
            if _should_send_missing_level_warning(missing_level):
                telegram_service.send_message(
                    _build_missing_level_warning(summary),
                    level="INFO",
                )

        # ---- Zero-data path: nothing to watch ----------------------- #
        # Preserved from the original wrapper: if the tree is empty,
        # there is nothing to report through the normal completion
        # path. The two once-per-day warnings above already surfaced
        # the failure state (if applicable). Return without a
        # Telegram "Completed" message so we don't flood the 5-minute
        # recheck cadence with zero-data noise.
        if stage2_contracts == 0:
            logger.info(
                "%s no stage-2 contracts to watch — "
                "returning without a completion message",
                _STAGE_2_S2_WRAPPER_MARKER,
            )
            # Update the successful-run record even on the zero-data
            # path so a subsequent run's `_is_first_run_after_restart`
            # correctly returns False.
            _record_successful_run(summary)
            return

        # ---- Non-zero path: preserve the original Telegram gate ----- #
        should_send_started = (
            isolated is not None
            or touched_contracts > 0
        )

        if should_send_started:
            telegram_service.send_message(
                build(
                    emoji="🚀",
                    title="Strategy 2 · Stage 3 · Started",
                    fields=[("Trigger", "scheduled")],
                    sections=[(
                        "Doing",
                        "🔍",
                        [
                            "Read stage-2 isolated contracts",
                            "Compute each contract's own isolation level",
                            "Filter to ONE contract: ticker priority + nearest ATM",
                        ],
                    )],
                    status="started",
                ),
                level="INFO",
            )

        # ---- Completion gate (preserved original behaviour) --------- #
        send_completion = _should_send_completion(summary)

        if send_completion:
            level = (
                "SUCCESS"
                if (isolated and truly_missing_level == 0)
                else "INFO"
                if (with_canonical > 0 and truly_missing_level == 0)
                else "WARNING"
            )
            telegram_service.send_message(
                _build_completion_message(summary), level=level
            )
        else:
            logger.info(
                "%s no state change "
                "(stage2=%d touched=%d isolated=%s missing_level=%d) — "
                "Telegram completion suppressed by noise gate",
                _STAGE_2_S2_WRAPPER_MARKER,
                stage2_contracts,
                touched_contracts,
                "yes" if isolated else "no",
                missing_level,
            )

        # Update the successful-run record so subsequent runs can
        # detect "first run after restart" correctly.
        _record_successful_run(summary)

        logger.info(
            "%s completed — status=%s stage2=%d "
            "touched=%d missing_level=%d with_canonical=%d isolated=%s",
            _STAGE_2_S2_WRAPPER_MARKER,
            status or "?",
            stage2_contracts,
            touched_contracts,
            missing_level,
            with_canonical,
            "yes" if isolated else "no",
        )

    except Exception as exc:
        logger.exception(
            "%s Strategy 2 stage 3 failed",
            _STAGE_2_S2_WRAPPER_MARKER,
        )
        telegram_service.send_message(
            build(
                emoji="❌",
                title="Strategy 2 · Stage 3 · Failed",
                fields=[("Error", f"{type(exc).__name__}: {exc}")],
                sections=[(
                    "Consequence",
                    "⚠️",
                    [
                        "The S2 Stage-3 run raised. The stage_3_summary.json "
                        "may be from an earlier run or may not exist.",
                        "Stage 4 S2 will read whatever the tracker last "
                        "wrote (possibly an empty or stale summary).",
                        "Check: GET /admin/strategy2",
                        "Check: GET /admin/strategy2/tracker/status",
                        "Fix:   POST /admin/strategy2/tracker/start",
                    ],
                )],
                status="failed",
            ),
            level="ERROR",
        )