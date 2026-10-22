# File: token_tasks/strategy_order_task.py
"""Scheduled wrapper for Strategy 1 — Stage 4 (order placement).

Completion-message branching
----------------------------
The service returns one of three top-level ``status`` values on a
non-order run, and each is now surfaced differently on Telegram:

    status = "ok"              -> normal completion message (order
                                  placed or no strike met criteria)
    status = "no_activations"  -> dedicated message, further split by
                                  the new ``stage_3_summary_state``
                                  field (see "Diagnostics" below)
    status = "disabled"        -> dedicated INFO message (one of
                                  STRATEGY_ENABLED / STRATEGY_ORDER_ENABLED
                                  is false).

Diagnostics (added for the 2026-10-22 observability patch set)
--------------------------------------------------------------
The wrapper used ``logging.getLogger(__name__)``, which means every
log line from it was console-only — nothing reached
``logs/strategy_order_task.log`` or
``logs_text/strategy_order_task.txt``. Combined with the wrapper
collapsing three genuinely different ``no_activations`` states into a
single warning message, an operator could not tell from the phone
which of these had happened:

    * Stage 3 never wrote ``stage_3_summary.json`` at all
      → stage_3_summary_state == "missing"
      → operator action: check tracker status / start it

    * Stage 3 wrote yesterday's file but not today's
      → stage_3_summary_state == "stale_day"
      → operator action: verify the cron chain ran

    * Stage 3 ran today, wrote the summary, and legitimately found
      no contract whose own 09:15–09:20 window high reached its
      own R2
      → stage_3_summary_state == "empty_activations"
      → operator action: **none** — this is a normal quiet day

This wrapper now:

    * uses ``get_logger(__file__)`` so every line reaches the file
      handlers;
    * emits one ``[stage_4 wrapper] starting`` INFO line before the
      service runs, naming the trigger and mode;
    * emits one ``[stage_4 wrapper] diagnosis`` aggregate INFO line
      with every Stage-4 counter (status / state / candidates /
      picks / placed / with_ltp / without_ltp);
    * emits a state-specific INFO or WARNING line naming the exact
      consequence and the correct next endpoint to check:

          missing              → WARNING + /admin/strategy/tracker/status
          stale_day            → WARNING + the two dates
          empty_activations    → INFO + "quiet day, no action"
          unknown              → WARNING + raw state value

    * emits a live-mode WARNING when any placed order has
      ``entry_ltp = None`` — the second half of the 2026-10-22
      failure signature — naming the count and the checklist
      (``GET /live/status``);
    * carries the same counters into the Telegram completion message
      under a "Pipeline health" section;
    * derives the Telegram level from data health, not from
      ``placed > 0`` alone:

          SUCCESS  — placed > 0 AND every order has an entry LTP
                     AND the Stage-3 state is clean
          INFO     — status=disabled, or state=empty_activations
                     (normal quiet day)
          WARNING  — every other non-error path
          ERROR    — the service raised

      This means the Oct 22 shape (Stage 3 ran, isolated 68 contracts,
      but the picked contract had no live LTP because it was never
      subscribed) surfaces as a **WARNING** with the correct
      diagnostic, not as a benign "✅ placed=1" message.

Additional hardening (this revision)
------------------------------------
Building on the 2026-10-22 patch, this revision adds:

    * **A ``stage_3_decisions_hint`` field in the "no activations"
      Telegram branches.** The Stage-3 decisions file
      (``stage_3_decisions.json``) carries the per-contract
      ``reason_code`` for every skipped contract. When Stage 4
      reports ``no_activations`` the phone message now names that
      file explicitly so the operator can jump straight to it. This
      was previously only mentioned in the log stream.

    * **A "candidates>0 but picks==0" Telegram branch.** When the
      Stage-3 summary had activations (``candidates > 0``) but every
      activation's ``ticker`` field was empty (a data-quality edge
      case that the previous "ok but placed=0" branch treated as
      WARNING but did not explain), the phone message now names the
      exact cause and points at ``stage_3_summary.activated[*].ticker``.
      This state was previously only visible in the log.

    * **A "Stage-3 corrupted" Telegram branch.** When Stage 3 wrote
      a summary whose ``activated`` list is non-empty but whose
      records are missing the required ``ticker`` field, the
      ``_build_completion_message`` path (which reads
      ``orders[0].ticker``) would render ``Ticker: None``. The new
      branch recognises this shape and emits a clear WARNING with
      the actual counter ``candidates > 0 but picks == 0``.

    * **Tolerant completion-message rendering.** ``orders[0]`` is
      passed through a shape-tolerant reader that never returns
      ``None`` for a present field. This is defensive against a
      future field rename on the order service.

    * **A ``_STAGE_4_WRAPPER_MARKER`` constant** used as the grep
      anchor for operators. Every log line from this module now
      starts with ``[stage_4 wrapper]``, and this marker is the
      single pattern an operator can search on to pull every
      Stage-4 wrapper line — including the pre-run starting line,
      the diagnosis aggregate, the state-specific verdict, and the
      post-run complete line — out of the daily log.

Telegram message shape
----------------------
The completion message is now one of five shapes:

    * **Success** — placed > 0, all entries have LTP (live mode).
    * **Warning — placed, no LTP (live)** — placed > 0 but
      without_ltp > 0 in live mode.
    * **Warning — partial** — placed > 0 but catch-up produced no
      LTP (expected, but flagged).
    * **Warning — no activations (missing/stale/unknown)** —
      Stage-3 state is not clean.
    * **Info — quiet day (empty_activations)** — Stage 3 ran
      correctly, no contract reached its own R2.

Every message carries the same fields (Mode / Catch-up /
Post-market / Scheduled / Actual) so the operator can always
orient themselves at a glance.
"""

from __future__ import annotations

from typing import Any

from core.logger import get_logger
from services.strategy_order_service import strategy_order_service
from services.telegram_service import telegram_service
from services.telegram_format import build

logger = get_logger(__file__)

# Grep anchor. Every log line from this module starts with this
# prefix, so an operator can pull every Stage-4 wrapper line with a
# single ``grep '[stage_4 wrapper]'``.
_STAGE_4_WRAPPER_MARKER = "[stage_4 wrapper]"

# Path hint used in several Telegram messages. Never an absolute path
# — the pipeline root is the standard project layout, so this stays
# consistent across deployments.
_STAGE_3_DECISIONS_HINT = (
    "data/strategy/stage_3_r2_activated/stage_3_decisions.json"
)
_STAGE_3_SUMMARY_HINT = (
    "data/strategy/stage_3_r2_activated/stage_3_summary.json"
)


# --------------------------------------------------------------------------- #
# Shape-tolerant readers                                                      #
# --------------------------------------------------------------------------- #
def _read_field(record: dict[str, Any] | None, *names: str) -> Any:
    """Return the first non-empty value of the named fields.

    Reads only the top level. Used to render order fields tolerantly
    so a future field rename on the order service does not produce
    ``None`` in the Telegram message. Returns ``None`` if no named
    field exists.
    """
    if not isinstance(record, dict):
        return None
    for name in names:
        v = record.get(name)
        if v not in (None, ""):
            return v
    return None


def _order_missing_ticker(orders: list[dict[str, Any]]) -> bool:
    """Return True when any order record lacks a non-empty ticker.

    Used to detect the "Stage-3 wrote activations but every ticker is
    empty" edge case, which the normal completion path renders as
    ``Ticker: None`` without explanation.
    """
    for o in orders:
        if not isinstance(o, dict):
            return True
        ticker = o.get("ticker")
        if ticker in (None, ""):
            return True
    return False


# --------------------------------------------------------------------------- #
# Telegram message builders                                                   #
# --------------------------------------------------------------------------- #
def _build_no_activations_message(summary: dict) -> str:
    """Dedicated message when Stage 3 produced no activations today.

    Branches on ``stage_3_summary_state`` so the operator sees the
    correct next action per state:

        missing           — WARNING, action required
        stale_day         — WARNING, action required
        empty_activations — INFO, normal quiet day, no action
        anything else     — WARNING, unknown state (defensive)
    """
    state = str(summary.get("stage_3_summary_state") or "")
    state_day = summary.get("stage_3_summary_day")

    common_fields = [
        ("Mode", summary.get("mode")),
        ("Catch-up", "yes" if summary.get("catchup") else "no"),
        ("Post-market", "yes" if summary.get("post_market") else "no"),
        ("Scheduled", summary.get("scheduled_time_ist")),
        ("Actual", summary.get("actual_time_ist")),
        ("Stage-3 state", state or "?"),
    ]

    # ---- Branch A: Stage 3 never wrote the summary ----------------- #
    if state == "missing":
        return build(
            emoji="⚠️",
            title="Strategy 1 · Stage 4 · No Activations · Stage 3 Missing",
            fields=common_fields,
            sections=[(
                "Why",
                "🔍",
                [
                    "stage_3_summary.json does not exist.",
                    "Stage 3 never ran for today, or its write failed.",
                    "This is NOT a quiet day — Stage 3 is broken.",
                    "Check: GET /admin/strategy/tracker/status",
                    "Check: GET /admin/strategy/stage3",
                    "Fix:   POST /admin/strategy/tracker/start",
                    "If the app was launched before 09:00, the "
                    "market-hours gate may have skipped the tracker "
                    "start; the post-Stage-2 hook should have run it.",
                ],
            )],
            status="warning",
            status_detail="stage-3 summary missing",
        )

    # ---- Branch B: summary exists but from a previous day ---------- #
    if state == "stale_day":
        return build(
            emoji="⚠️",
            title="Strategy 1 · Stage 4 · No Activations · Stale Summary",
            fields=common_fields + [
                ("Summary day", state_day or "?"),
            ],
            sections=[(
                "Why",
                "🔍",
                [
                    "stage_3_summary.json exists but is from an earlier day.",
                    "The cron chain did not rewrite it today.",
                    "Check: GET /admin/strategy/stage3",
                    "Check: GET /admin/strategy/tracker/status",
                    "Fix:   POST /admin/strategy/tracker/start",
                ],
            )],
            status="warning",
            status_detail="stage-3 summary is stale",
        )

    # ---- Branch C: Stage 3 ran, 0 contracts reached their own R2 --- #
    if state == "empty_activations":
        return build(
            emoji="📭",
            title="Strategy 1 · Stage 4 · Quiet Day",
            fields=common_fields,
            sections=[(
                "Why",
                "ℹ️",
                [
                    "Stage 3 ran correctly and wrote today's summary.",
                    "No contract's own 09:15–09:20 window high reached "
                    "its own R2.",
                    "This is a normal quiet-day outcome — no action "
                    "required.",
                    f"Per-contract reasons: {_STAGE_3_DECISIONS_HINT}",
                ],
            )],
            status="info",
            status_detail="no contract reached its own R2 (normal quiet day)",
        )

    # ---- Branch D: unknown state (defensive) ----------------------- #
    return build(
        emoji="⚠️",
        title="Strategy 1 · Stage 4 · No Activations",
        fields=common_fields,
        sections=[(
            "Why",
            "🔍",
            [
                f"stage_3_summary_state={state or '<empty>'} — "
                f"unrecognised.",
                "Check: GET /admin/strategy/stage3",
                f"Check: {_STAGE_3_SUMMARY_HINT}",
                f"Per-contract reasons: {_STAGE_3_DECISIONS_HINT}",
            ],
        )],
        status="warning",
        status_detail=f"unknown stage-3 state ({state or '?'})",
    )


def _build_disabled_message(summary: dict) -> str:
    """Dedicated message when Stage 4 is disabled by config."""
    return build(
        emoji="🚫",
        title="Strategy 1 · Stage 4 · Disabled",
        fields=[
            ("Mode", summary.get("mode")),
            ("Scheduled", summary.get("scheduled_time_ist")),
            ("Actual", summary.get("actual_time_ist")),
            (
                "Reason",
                "STRATEGY_ENABLED or STRATEGY_ORDER_ENABLED is false",
            ),
        ],
        status="info",
    )


def _build_no_ticker_message(summary: dict) -> str:
    """Dedicated message when candidates > 0 but picks == 0.

    This is the "activations were written but every activation's
    ``ticker`` field is empty" edge case. The normal completion path
    would render ``Ticker: None`` without explaining why. This branch
    names the exact field to check.
    """
    candidates = int(summary.get("candidates") or 0)
    picks = int(summary.get("picks") or 0)

    return build(
        emoji="⚠️",
        title="Strategy 1 · Stage 4 · Activations Had No Ticker",
        fields=[
            ("Mode", summary.get("mode")),
            ("Catch-up", "yes" if summary.get("catchup") else "no"),
            ("Scheduled", summary.get("scheduled_time_ist")),
            ("Actual", summary.get("actual_time_ist")),
            ("Candidates", candidates),
            ("Picks", picks),
        ],
        sections=[(
            "Why",
            "🔍",
            [
                f"Stage 3 produced {candidates} activation(s), but every "
                f"activation's 'ticker' field was empty — Stage 4 could "
                f"not group them by ticker, so 'picks' came out as 0.",
                "This is a data-quality failure in Stage 3's output, "
                "not a quiet day.",
                f"Check: {_STAGE_3_SUMMARY_HINT}",
                "Each entry under 'activated' should carry a non-empty "
                "'ticker' (e.g. 'NIFTY' or 'SENSEX').",
                f"Per-contract reasons: {_STAGE_3_DECISIONS_HINT}",
            ],
        )],
        status="warning",
        status_detail=f"candidates={candidates} but picks=0 (no ticker)",
    )


def _build_completion_message(summary: dict) -> str:
    """Build the Telegram message for a non-disabled Stage-4 run.

    Branches on ``summary["status"]``:
      * ``no_activations`` -> state-aware (see _build_no_activations_message)
      * ``disabled``       -> dedicated info (config off)
      * ``ok`` with
          - candidates > 0 and picks == 0
                             -> dedicated "no ticker" message
          - otherwise        -> normal completion message with a
                                "Pipeline health" section carrying
                                entry-LTP counters
    """
    status = summary.get("status")

    # ---- Branch 1: Stage 3 produced nothing (state-aware) ---------- #
    if status == "no_activations":
        return _build_no_activations_message(summary)

    # ---- Branch 2: Stage 4 disabled by config ---------------------- #
    if status == "disabled":
        return _build_disabled_message(summary)

    # ---- Branch 3: activations existed but nothing was picked ------ #
    # This is the "every activation's ticker is empty" edge case.
    candidates = int(summary.get("candidates") or 0)
    picks = int(summary.get("picks") or 0)
    orders = summary.get("orders") or []

    if candidates > 0 and picks == 0:
        return _build_no_ticker_message(summary)

    # ---- Branch 4: normal Stage-4 path ----------------------------- #
    placed = int(summary.get("placed") or 0)
    with_ltp = int(summary.get("orders_with_ltp") or 0)
    without_ltp = int(summary.get("orders_without_ltp") or 0)
    catchup = bool(summary.get("catchup"))

    # Per-order fields for the placed order (first is the winner).
    if orders:
        o = orders[0]
        order_fields = [
            ("Ticker", _read_field(o, "ticker")),
            ("Strike", _read_field(o, "strike_price")),
            ("Type", _read_field(o, "strike_type")),
            ("Side", _read_field(o, "side")),
            ("Qty", _read_field(o, "quantity")),
            ("Entry LTP", _read_field(o, "entry_ltp")),
            ("Entry source", _read_field(o, "entry_ltp_source")),
            ("R2", _read_field(o, "r2_value")),
            ("Touched at", _read_field(o, "index_touched_at")),
            (
                "Key",
                _read_field(o, "upstox_instrument_key", "instrument_key"),
            ),
        ]
    else:
        order_fields = [
            ("Result", "no strike met selection criteria"),
        ]

    # ---- Pipeline-health bullets ---------------------------------- #
    health_bullets: list[str] = []
    warnings: list[str] = []

    if placed > 0:
        health_bullets.append(f"Orders with LTP:    {with_ltp} / {placed}")
        health_bullets.append(f"Orders without LTP: {without_ltp} / {placed}")
        if without_ltp > 0 and not catchup:
            warnings.append(
                f"⚠️ {without_ltp}/{placed} order(s) have no live LTP — "
                f"picked contracts may not be subscribed to the feed. "
                f"Check: GET /live/status"
            )
        elif without_ltp > 0 and catchup:
            health_bullets.append(
                "↳ No LTP in catch-up is expected (documented "
                "no-fallback rule)"
            )
    else:
        # activations existed but nothing was picked — the edge case
        # that this branch can still hit if candidates == 0 but
        # picks > 0 (shouldn't happen, but defensive).
        warnings.append(
            "⚠️ 0 orders placed — check stage_3_summary.activated "
            "entries (each should carry a non-empty 'ticker')."
        )

    return build(
        emoji="💰",
        title="Strategy 1 · Stage 4 · Completed",
        fields=[
            ("Mode", summary.get("mode")),
            ("Catch-up", "yes" if catchup else "no"),
            ("Post-market", "yes" if summary.get("post_market") else "no"),
            ("Scheduled", summary.get("scheduled_time_ist")),
            ("Actual", summary.get("actual_time_ist")),
        ] + order_fields,
        sections=[
            (
                "Counters",
                "🔢",
                [
                    f"Candidates: {summary.get('candidates')}",
                    f"Picks: {summary.get('picks')}",
                    f"Placed: {placed}",
                ],
            ),
            ("Pipeline health", "🔧", [*warnings, *health_bullets]),
        ],
        status=(
            "success"
            if (placed > 0 and not warnings)
            else "warning"
        ),
        status_detail=(
            f"placed={placed}"
            + (f" · {len(warnings)} warning(s)" if warnings else "")
        ),
    )


# --------------------------------------------------------------------------- #
# Structured log diagnosis                                                    #
# --------------------------------------------------------------------------- #
def _log_diagnostics(summary: dict) -> None:
    """Emit one structured per-run diagnosis block.

    A healthy run is one aggregate INFO line plus one state INFO.
    Any data-path failure produces a targeted WARNING that names the
    failing sub-step (Stage-3 not written / stale / no-LTP-in-live).

    The aggregate line carries every counter Stage 4 returns, so a
    single grep of ``[stage_4 wrapper] diagnosis`` is a complete
    snapshot of the run — matching exactly what the Telegram message
    shows.
    """
    status = str(summary.get("status") or "")
    state = str(summary.get("stage_3_summary_state") or "")
    state_day = summary.get("stage_3_summary_day")
    candidates = int(summary.get("candidates") or 0)
    picks = int(summary.get("picks") or 0)
    placed = int(summary.get("placed") or 0)
    with_ltp = int(summary.get("orders_with_ltp") or 0)
    without_ltp = int(summary.get("orders_without_ltp") or 0)
    catchup = bool(summary.get("catchup"))

    # ---- Aggregate line ------------------------------------------------ #
    logger.info(
        "%s diagnosis — status=%s state=%s candidates=%d "
        "picks=%d placed=%d with_ltp=%d without_ltp=%d",
        _STAGE_4_WRAPPER_MARKER,
        status or "?",
        state or "?",
        candidates,
        picks,
        placed,
        with_ltp,
        without_ltp,
    )

    # ---- State-specific consequence ---------------------------------- #
    if status == "no_activations":
        if state == "missing":
            logger.warning(
                "%s stage_3_summary.json does NOT exist. "
                "Stage 3 never ran for today, or its write failed. "
                "Check: GET /admin/strategy/tracker/status and "
                "GET /admin/strategy/stage3.",
                _STAGE_4_WRAPPER_MARKER,
            )
        elif state == "stale_day":
            logger.warning(
                "%s stage_3_summary.json is from %s "
                "(today is %s). The file was not rewritten today. "
                "Check: GET /admin/strategy/stage3.",
                _STAGE_4_WRAPPER_MARKER,
                state_day or "?",
                summary.get("day") or "?",
            )
        elif state == "empty_activations":
            logger.info(
                "%s quiet day — Stage 3 wrote today's "
                "summary (%s) but no contract's own 09:15–09:20 window "
                "high reached its own R2. No action required. "
                "Per-contract reasons: %s.",
                _STAGE_4_WRAPPER_MARKER,
                summary.get("day") or "?",
                _STAGE_3_DECISIONS_HINT,
            )
        else:
            logger.warning(
                "%s status=no_activations but "
                "stage_3_summary_state=%r — unrecognised state. "
                "Check %s manually.",
                _STAGE_4_WRAPPER_MARKER,
                state,
                _STAGE_3_SUMMARY_HINT,
            )
        return

    if status == "disabled":
        logger.info(
            "%s stage 4 disabled by config — no orders placed.",
            _STAGE_4_WRAPPER_MARKER,
        )
        return

    if status == "ok":
        # ---- Success-path diagnostics --------------------------------- #
        if placed == 0 and candidates > 0:
            logger.warning(
                "%s status=ok but 0 orders placed with "
                "%d candidate(s) — every activation had an empty or "
                "unusable ticker. Check stage_3_summary.activated "
                "entries (each should carry a non-empty 'ticker'). "
                "File: %s.",
                _STAGE_4_WRAPPER_MARKER,
                candidates,
                _STAGE_3_SUMMARY_HINT,
            )
        if placed > 0:
            if without_ltp > 0 and not catchup:
                logger.warning(
                    "%s %d/%d order(s) placed without an "
                    "entry LTP in live mode. The picked contract has no "
                    "tick on the feed. Check GET /live/status "
                    "(feed.instrument_count vs /live/instruments count). "
                    "If the contract is missing, it was never "
                    "subscribed — see strategy_selection_service."
                    "_register_strike.",
                    _STAGE_4_WRAPPER_MARKER,
                    without_ltp,
                    placed,
                )
            elif without_ltp > 0 and catchup:
                logger.info(
                    "%s %d/%d order(s) placed without an "
                    "entry LTP in catch-up mode — the documented "
                    "no-fallback rule applies (no in-window intraday "
                    "candle at or before the scheduled minute).",
                    _STAGE_4_WRAPPER_MARKER,
                    without_ltp,
                    placed,
                )
            else:
                logger.info(
                    "%s all %d placed order(s) have an "
                    "entry LTP (with_ltp=%d).",
                    _STAGE_4_WRAPPER_MARKER,
                    placed,
                    with_ltp,
                )
        return

    # Any other status (should not occur, but defensive)
    logger.warning(
        "%s unknown status=%r — no specific diagnosis.",
        _STAGE_4_WRAPPER_MARKER,
        status,
    )


# --------------------------------------------------------------------------- #
# Public entry point                                                          #
# --------------------------------------------------------------------------- #
def run_strategy_order() -> None:
    # ---- Pre-run log line --------------------------------------------- #
    logger.info(
        "%s starting — trigger=scheduled mode=scheduled",
        _STAGE_4_WRAPPER_MARKER,
    )

    telegram_service.send_message(
        build(
            emoji="💰",
            title="Strategy 1 · Stage 4 · Started",
            sections=[(
                "Doing",
                "🔍",
                [
                    "Read stage-3 activations",
                    "Pick nearest-to-ATM per index",
                    "Apply FIFO by R2 touch",
                    "Place paper order",
                ],
            )],
            status="started",
        ),
        level="INFO",
    )

    try:
        summary = strategy_order_service.run(reason="scheduled")
        status = str(summary.get("status") or "")
        state = str(summary.get("stage_3_summary_state") or "")
        placed = int(summary.get("placed") or 0)
        with_ltp = int(summary.get("orders_with_ltp") or 0)
        without_ltp = int(summary.get("orders_without_ltp") or 0)
        candidates = int(summary.get("candidates") or 0)
        picks = int(summary.get("picks") or 0)
        catchup = bool(summary.get("catchup"))

        # ---- Structured diagnosis (mirrors what Telegram shows) ----- #
        _log_diagnostics(summary)

        # ---- Telegram level: derive from data health, not just count # #
        # Rules (in order of precedence):
        #   ERROR   — handled below in the except block
        #   WARNING — state in {missing, stale_day, unknown}, or
        #             placed > 0 with without_ltp > 0 in live mode, or
        #             candidates > 0 and picks == 0 (no-ticker), or
        #             placed == 0 while status == "ok"
        #   INFO    — status == "disabled", or
        #             state == "empty_activations" (quiet day)
        #   SUCCESS — everything else (placed > 0, all entries have LTP)
        if status == "no_activations":
            if state == "empty_activations":
                telegram_level = "INFO"
            else:
                telegram_level = "WARNING"
        elif status == "disabled":
            telegram_level = "INFO"
        elif status == "ok":
            if candidates > 0 and picks == 0:
                # Activations existed but no ticker — dedicated warning.
                telegram_level = "WARNING"
            elif placed == 0:
                telegram_level = "WARNING"
            elif without_ltp > 0 and not catchup:
                telegram_level = "WARNING"
            else:
                telegram_level = "SUCCESS"
        else:
            telegram_level = "WARNING"

        telegram_service.send_message(
            _build_completion_message(summary), level=telegram_level
        )

        # ---- Post-run aggregate log line ---------------------------- #
        logger.info(
            "%s completed — status=%s state=%s "
            "candidates=%d picks=%d placed=%d with_ltp=%d without_ltp=%d",
            _STAGE_4_WRAPPER_MARKER,
            status or "?",
            state or "?",
            candidates,
            picks,
            placed,
            with_ltp,
            without_ltp,
        )
    except Exception as exc:
        logger.exception(
            "%s Strategy 1 stage 4 failed",
            _STAGE_4_WRAPPER_MARKER,
        )
        telegram_service.send_message(
            build(
                emoji="❌",
                title="Strategy 1 · Stage 4 · Failed",
                fields=[("Error", f"{type(exc).__name__}: {exc}")],
                sections=[(
                    "Consequence",
                    "⚠️",
                    [
                        "The Stage-4 run raised. The stage_4_summary.json "
                        "may be from an earlier run or may not exist.",
                        "Check: GET /admin/strategy/stage4",
                        "Fix:   POST /admin/strategy/order/run",
                    ],
                )],
                status="failed",
            ),
            level="ERROR",
        )