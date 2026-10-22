# File: token_tasks/strategy_2_order_task.py
"""Scheduled wrapper for Strategy 2 — Stage 4.

Message branching
-----------------
The S2 Stage-4 service returns one of four top-level ``status``
values, and — following the 2026-10-22 observability patch — a set
of diagnostic fields that let this wrapper distinguish six
genuinely different sub-states. Each is surfaced with its own
Telegram message and its own log line:

    status = "disabled"
        → INFO.  STRATEGY_2_ENABLED or STRATEGY_2_ORDER_ENABLED is
          false. No work was done.

    status = "no_isolated", stage_3_summary_state = "missing"
        → WARNING.  Stage 3 never wrote stage_3_summary.json.
          Operator action: GET /admin/strategy2,
          GET /admin/strategy2/tracker/status.

    status = "no_isolated", stage_3_summary_state = "no_isolated"
        → INFO.  Normal "no contract has touched its S3 level yet"
          case. Per-contract reasons are in
          data/strategy2/stage_3_isolation_level/stage_3_summary.json.

    status = "no_isolated", stage_3_summary_state = "no_touch_minute"
        → WARNING.  Isolated record present but malformed (no
          touch_minute). Manual inspection required.

    status = "no_cross_after_touch", candle_stats.ema_computable = true
        → INFO.  The EMA engine has enough candles for the picked
          contract but no cross has fired since the S3 touch yet.
          This is the normal "waiting" case on a quiet day.

    status = "no_cross_after_touch", candle_stats.ema_computable = false
        → WARNING.  The EMA engine does not have enough candles for
          the picked contract. The contract is probably not
          subscribed to the live feed, or live_backfill did not
          include it. This is the 2026-10-22 failure shape on the
          S2 side.

    status = "ok", sibling_resolved = true, entry_ltp_available = true
        → SUCCESS.  Normal order placement.

    status = "ok", sibling_resolved = false OR entry_ltp_available = false
        → WARNING.  An order was placed, but either the bearish
          sibling could not be resolved (Stage 1 never resolved that
          strike's opposite side) or the entry price is unusable
          (cross close unparseable AND sibling LTP missing).

Diagnostics (added for the 2026-10-22 observability patch set)
--------------------------------------------------------------
This wrapper used ``logging.getLogger(__name__)``, which means every
log line from it was console-only — nothing reached
``logs/strategy_2_order_task.log`` or
``logs_text/strategy_2_order_task.txt``. Combined with the wrapper
collapsing six distinct states into a single success/warning split,
the S2 Stage-4 pipeline was effectively un-diagnosable from the log
stream alone.

This wrapper now:

    * uses ``get_logger(__file__)`` so every line reaches the file
      handlers;
    * emits one ``[stage_4_s2 wrapper] starting`` INFO line before
      the service runs, naming the trigger and mode;
    * emits one ``[stage_4_s2 wrapper] diagnosis`` aggregate INFO
      line per run with every counter the service returns
      (``status / state / placed / sibling_resolved /
      entry_ltp_available / candle_stats``);
    * emits a state-specific INFO or WARNING line after the
      aggregate naming the correct next action per sub-state (see
      the six states above);
    * carries the same counters into the Telegram completion
      message under a "Pipeline health" section;
    * derives the Telegram level from data health, not just from
      ``placed > 0``:

          SUCCESS  — placed > 0 AND sibling_resolved AND
                     entry_ltp_available AND state is clean
          INFO     — disabled, quiet-no-touch, or warm-no-cross
          WARNING  — every other non-error path
          ERROR    — the service raised

    * names the exact 2026-10-22 failure shapes on both sides:

          missing Stage-3 summary        → stage never ran
          engine not warmed up           → not subscribed to feed
          sibling not resolved           → Stage 1 missed one side
          entry LTP unavailable          → no cross close, no sibling
"""

from __future__ import annotations

from core.logger import get_logger
from services.strategy_2_order_service import strategy_2_order_service
from services.telegram_service import telegram_service
from services.telegram_format import build

logger = get_logger(__file__)


# --------------------------------------------------------------------------- #
# Sub-state classification                                                    #
# --------------------------------------------------------------------------- #
def _classify_run(summary: dict) -> str:
    """Return a short sub-state string for the current run.

    The service's top-level ``status`` is the primary key, but the
    diagnostic fields it returns let this wrapper distinguish six
    genuinely different situations that each require a different
    operator action (or none).
    """
    status = str(summary.get("status") or "")
    state = str(summary.get("stage_3_summary_state") or "")
    candle_stats = summary.get("candle_stats") or {}

    if status == "disabled":
        return "disabled"

    if status == "no_isolated":
        if state == "missing":
            return "no_isolated__stage3_missing"
        if state == "no_touch_minute":
            return "no_isolated__malformed"
        return "no_isolated__waiting"

    if status == "no_cross_after_touch":
        if candle_stats.get("ema_computable"):
            return "no_cross__engine_warm"
        return "no_cross__engine_cold"

    if status == "ok":
        sibling_ok = summary.get("sibling_resolved")
        ltp_ok = summary.get("entry_ltp_available")
        if sibling_ok is False:
            return "ok__sibling_missing"
        if ltp_ok is False:
            return "ok__entry_ltp_missing"
        return "ok"

    return "unknown"


# --------------------------------------------------------------------------- #
# Telegram message builders                                                   #
# --------------------------------------------------------------------------- #
def _common_fields(summary: dict) -> list[tuple[str, str]]:
    """Fields shown on every message so the run is identifiable."""
    return [
        ("Mode", str(summary.get("mode") or "?")),
        ("Catch-up", "yes" if summary.get("catchup") else "no"),
        ("Post-market", "yes" if summary.get("post_market") else "no"),
    ]


def _build_disabled_message(summary: dict) -> str:
    return build(
        emoji="🚫",
        title="Strategy 2 · Stage 4 · Disabled",
        fields=_common_fields(summary) + [
            (
                "Reason",
                "STRATEGY_2_ENABLED or STRATEGY_2_ORDER_ENABLED is false",
            ),
        ],
        status="info",
    )


def _build_no_isolated_stage3_missing_message(summary: dict) -> str:
    return build(
        emoji="⚠️",
        title="Strategy 2 · Stage 4 · Stage 3 Missing",
        fields=_common_fields(summary) + [
            ("Stage-3 state", "missing"),
        ],
        sections=[(
            "Why",
            "🔍",
            [
                "stage_3_summary.json does not exist.",
                "Stage 3 never ran for today, or its write failed.",
                "This is NOT a quiet day — Stage 3 is broken.",
                "Check: GET /admin/strategy2",
                "Check: GET /admin/strategy2/tracker/status",
                "Fix:   POST /admin/strategy2/tracker/start",
            ],
        )],
        status="warning",
        status_detail="stage-3 summary missing",
    )


def _build_no_isolated_waiting_message(summary: dict) -> str:
    return build(
        emoji="📭",
        title="Strategy 2 · Stage 4 · No Isolated Contract Yet",
        fields=_common_fields(summary) + [
            ("Stage-3 state", "no_isolated"),
        ],
        sections=[(
            "Why",
            "ℹ️",
            [
                "Stage 3 ran correctly but no contract has touched "
                "its isolation level yet.",
                "This is the normal 'waiting' case on a quiet day.",
                "Per-contract reasons: data/strategy2/"
                "stage_3_isolation_level/stage_3_summary.json.contracts",
            ],
        )],
        status="info",
        status_detail="no contract has touched its isolation level",
    )


def _build_no_isolated_malformed_message(summary: dict) -> str:
    return build(
        emoji="⚠️",
        title="Strategy 2 · Stage 4 · Malformed Stage-3 Record",
        fields=_common_fields(summary) + [
            ("Stage-3 state", "no_touch_minute"),
        ],
        sections=[(
            "Why",
            "🔍",
            [
                "stage_3_summary.json has an isolated record but its "
                "'touch_minute' is empty.",
                "This is a malformed record — likely a manual edit or "
                "an older schema.",
                "Check: data/strategy2/stage_3_isolation_level/"
                "stage_3_summary.json",
            ],
        )],
        status="warning",
        status_detail="isolated record missing touch_minute",
    )


def _build_no_cross_warm_message(summary: dict) -> str:
    stats = summary.get("candle_stats") or {}
    isolated = summary.get("isolated") or {}
    return build(
        emoji="⏳",
        title="Strategy 2 · Stage 4 · Waiting for Cross",
        fields=_common_fields(summary) + [
            ("Isolated", f"{isolated.get('ticker')} {isolated.get('strike_price')} {isolated.get('strike_type')}"),
            ("S3 touch", str(isolated.get("touch_minute") or "?")),
            ("Candles scanned", str(stats.get("total_candles") or 0)),
            ("After touch", str(stats.get("after_touch") or 0)),
            ("Last candle", str(stats.get("last_candle_minute") or "?")),
        ],
        sections=[(
            "Why",
            "ℹ️",
            [
                "The EMA engine has enough candles for the picked "
                "contract, but no cross has fired since the S3 touch.",
                "This is the normal 'waiting' state on a quiet day.",
            ],
        )],
        status="info",
        status_detail="engine warmed up, no cross yet",
    )


def _build_no_cross_cold_message(summary: dict) -> str:
    stats = summary.get("candle_stats") or {}
    isolated = summary.get("isolated") or {}
    return build(
        emoji="⚠️",
        title="Strategy 2 · Stage 4 · EMA Engine Not Warmed Up",
        fields=_common_fields(summary) + [
            ("Isolated", f"{isolated.get('ticker')} {isolated.get('strike_price')} {isolated.get('strike_type')}"),
            ("S3 touch", str(isolated.get("touch_minute") or "?")),
            ("Usable candles", str(stats.get("usable_candles") or 0)),
            ("Required", str(stats.get("ema_required") or 0)),
            ("Last candle", str(stats.get("last_candle_minute") or "?")),
        ],
        sections=[(
            "Why",
            "🔍",
            [
                "The EMA engine does not have enough candles for the "
                "picked contract to compute EMA.",
                "The contract is probably not subscribed to the live "
                "feed, or live_backfill did not include it.",
                "Check: GET /live/status "
                "(feed.instrument_count vs /live/instruments count).",
                "Fix: check strategy_selection_service._register_strike "
                "(the contract should be subscribed right after its "
                "Upstox key is resolved).",
            ],
        )],
        status="warning",
        status_detail="engine lacks candles for the picked contract",
    )


def _build_ok_message(summary: dict) -> str:
    order = summary.get("order") or {}
    isolated = summary.get("isolated") or {}
    cross = summary.get("cross") or {}
    stats = summary.get("candle_stats") or {}
    sibling_ok = summary.get("sibling_resolved")
    ltp_ok = summary.get("entry_ltp_available")

    fields = _common_fields(summary) + [
        ("Ticker", str(isolated.get("ticker") or "?")),
        ("Original type", str(isolated.get("strike_type") or "?")),
        ("Strike", str(isolated.get("strike_price") or "?")),
        ("S3 touch", str(isolated.get("touch_minute") or "?")),
        ("Cross type", str(cross.get("type") or "?")),
        ("Cross at", str(cross.get("minute") or "?")),
        ("Order type", str(order.get("order_strike_type") or "?")),
        ("Side", f"{order.get('side')} × {order.get('quantity')}"),
        ("Entry LTP", f"{order.get('entry_ltp')} ({order.get('entry_ltp_source')})"),
        ("Key", str(order.get("order_instrument_key") or "?")),
    ]

    # ---- Pipeline-health bullets ---------------------------------- #
    health_bullets: list[str] = [
        f"Sibling resolved:    {'yes' if sibling_ok else 'no' if sibling_ok is False else 'n/a'}",
        f"Entry LTP available:  {'yes' if ltp_ok else 'no' if ltp_ok is False else 'n/a'}",
    ]
    if stats:
        health_bullets.append(
            f"Candles scanned:      {stats.get('total_candles', 0)} "
            f"(after touch: {stats.get('after_touch', 0)})"
        )

    warnings: list[str] = []
    if sibling_ok is False:
        warnings.append(
            "⚠️ Bearish cross could not resolve the opposite-type "
            "contract — Stage 1 may not have resolved that strike's "
            "PE side."
        )
    if ltp_ok is False:
        warnings.append(
            "⚠️ Entry LTP is null — cross close was unparseable AND "
            "sibling LTP at the cross minute was unavailable."
        )

    return build(
        emoji="💰",
        title="Strategy 2 · Stage 4 · Completed",
        fields=fields,
        sections=[
            ("Result", "🎯", ["Order placed (paper)."]),
            ("Pipeline health", "🔧", [*warnings, *health_bullets]),
        ],
        status="success" if not warnings else "warning",
        status_detail=(
            f"placed={summary.get('placed', 0)}"
            + (f" · {len(warnings)} warning(s)" if warnings else "")
        ),
    )


def _build_completion_message(summary: dict) -> str:
    """Dispatch to the correct message builder based on the sub-state."""
    sub_state = _classify_run(summary)

    if sub_state == "disabled":
        return _build_disabled_message(summary)
    if sub_state == "no_isolated__stage3_missing":
        return _build_no_isolated_stage3_missing_message(summary)
    if sub_state == "no_isolated__waiting":
        return _build_no_isolated_waiting_message(summary)
    if sub_state == "no_isolated__malformed":
        return _build_no_isolated_malformed_message(summary)
    if sub_state == "no_cross__engine_warm":
        return _build_no_cross_warm_message(summary)
    if sub_state == "no_cross__engine_cold":
        return _build_no_cross_cold_message(summary)
    if sub_state == "ok":
        return _build_ok_message(summary)

    # Defensive fallback for an unrecognised status / sub-state.
    return build(
        emoji="⚠️",
        title="Strategy 2 · Stage 4 · Unexpected Status",
        fields=_common_fields(summary) + [
            ("Status", str(summary.get("status") or "?")),
            ("Stage-3 state", str(summary.get("stage_3_summary_state") or "?")),
        ],
        sections=[(
            "Why",
            "🔍",
            [
                "The service returned a status this wrapper does not "
                "recognise.",
                "Check the service logs for details.",
            ],
        )],
        status="warning",
        status_detail="unexpected status",
    )


# --------------------------------------------------------------------------- #
# Structured log diagnosis                                                    #
# --------------------------------------------------------------------------- #
def _log_diagnostics(summary: dict) -> None:
    """Emit one structured per-run diagnosis block to the file logger.

    Aggregate line is always INFO. The state-specific line after it is
    the one that names the failing sub-step (or confirms normal
    operation). Mirrors what the Telegram message shows, so an
    operator can grep the log file and get the same picture they saw
    on the phone.
    """
    status = str(summary.get("status") or "")
    state = str(summary.get("stage_3_summary_state") or "")
    placed = int(summary.get("placed") or 0)
    sibling_ok = summary.get("sibling_resolved")
    ltp_ok = summary.get("entry_ltp_available")
    stats = summary.get("candle_stats") or {}
    isolated = summary.get("isolated") or {}

    # ---- Aggregate line ------------------------------------------------ #
    logger.info(
        "[stage_4_s2 wrapper] diagnosis — status=%s state=%s placed=%d "
        "sibling_resolved=%s entry_ltp_available=%s "
        "candles_total=%s candles_usable=%s ema_computable=%s",
        status or "?",
        state or "?",
        placed,
        str(sibling_ok),
        str(ltp_ok),
        stats.get("total_candles", "?"),
        stats.get("usable_candles", "?"),
        stats.get("ema_computable", "?"),
    )

    # ---- State-specific consequence ---------------------------------- #
    if status == "disabled":
        logger.info(
            "[stage_4_s2 wrapper] S2 stage 4 disabled by config — "
            "no work performed."
        )
        return

    if status == "no_isolated":
        if state == "missing":
            logger.warning(
                "[stage_4_s2 wrapper] stage_3_summary.json does NOT "
                "exist. Stage 3 never ran for today, or its write "
                "failed. Check GET /admin/strategy2 and "
                "GET /admin/strategy2/tracker/status."
            )
        elif state == "no_touch_minute":
            logger.warning(
                "[stage_4_s2 wrapper] stage_3_summary.json has an "
                "isolated record but its 'touch_minute' is empty — "
                "malformed record. Check data/strategy2/"
                "stage_3_isolation_level/stage_3_summary.json."
            )
        else:
            logger.info(
                "[stage_4_s2 wrapper] no isolated contract from Stage 3 "
                "yet (state=%s). Normal 'waiting' case — no contract "
                "has touched its isolation level. Per-contract reasons: "
                "data/strategy2/stage_3_isolation_level/"
                "stage_3_summary.json.contracts",
                state or "?",
            )
        return

    if status == "no_cross_after_touch":
        if stats.get("ema_computable"):
            logger.info(
                "[stage_4_s2 wrapper] engine warm, no cross after touch. "
                "Isolated=%s %s %s, touch=%s, total_candles=%d, "
                "after_touch=%d, last=%s.",
                isolated.get("ticker"),
                isolated.get("strike_price"),
                isolated.get("strike_type"),
                isolated.get("touch_minute") or "?",
                stats.get("total_candles", 0),
                stats.get("after_touch", 0),
                stats.get("last_candle_minute") or "?",
            )
        else:
            logger.warning(
                "[stage_4_s2 wrapper] engine NOT warmed up for the "
                "picked contract. usable=%s required=%s last=%s. "
                "The contract is probably not subscribed to the live "
                "feed, or live_backfill did not include it. "
                "Check GET /live/status. Fix: check "
                "strategy_selection_service._register_strike.",
                stats.get("usable_candles", "?"),
                stats.get("ema_required", "?"),
                stats.get("last_candle_minute") or "?",
            )
        return

    if status == "ok":
        if sibling_ok is False:
            logger.warning(
                "[stage_4_s2 wrapper] order placed but sibling_resolved="
                "false — bearish cross could not resolve the "
                "opposite-type contract. Stage 1 may not have resolved "
                "that strike's PE side. Check "
                "data/strategy/stage_1_initial/<TICKER>/<INDEX>/"
                "<strike>_PE/strategy_meta.json "
                "(upstox_instrument_key should be non-empty)."
            )
        if ltp_ok is False:
            logger.warning(
                "[stage_4_s2 wrapper] order placed but entry_ltp_available"
                "=false — cross close was unparseable AND sibling LTP "
                "at the cross minute was unavailable. Downstream "
                "accounting will see entry_ltp=null."
            )
        if sibling_ok is not False and ltp_ok is not False:
            logger.info(
                "[stage_4_s2 wrapper] order placed cleanly — ticker=%s "
                "strike=%s type=%s cross=%s @ %s entry_ltp=%s",
                isolated.get("ticker"),
                isolated.get("strike_price"),
                isolated.get("order_strike_type")
                or isolated.get("strike_type"),
                (summary.get("cross") or {}).get("type"),
                (summary.get("cross") or {}).get("minute"),
                (summary.get("order") or {}).get("entry_ltp"),
            )
        return

    logger.warning(
        "[stage_4_s2 wrapper] unknown status=%r — no specific "
        "diagnosis available.",
        status,
    )


# --------------------------------------------------------------------------- #
# Telegram level derivation                                                   #
# --------------------------------------------------------------------------- #
def _derive_telegram_level(summary: dict) -> str:
    """Return the Telegram ``level`` for the current run.

    Rules (precedence order):
        INFO    — disabled, quiet-no-touch, or warm-no-cross
        SUCCESS — placed > 0 and every health check passed
        WARNING — everything else
    """
    sub_state = _classify_run(summary)

    if sub_state == "disabled":
        return "INFO"
    if sub_state == "no_isolated__waiting":
        return "INFO"
    if sub_state == "no_cross__engine_warm":
        return "INFO"
    if sub_state == "ok":
        return "SUCCESS"
    return "WARNING"


# --------------------------------------------------------------------------- #
# Public entry point                                                          #
# --------------------------------------------------------------------------- #
def run_strategy_2_order() -> None:
    # ---- Pre-run log line --------------------------------------------- #
    logger.info(
        "[stage_4_s2 wrapper] starting — trigger=scheduled mode=scheduled"
    )

    telegram_service.send_message(
        build(
            emoji="🚀",
            title="Strategy 2 · Stage 4 · Started",
            sections=[(
                "Doing",
                "🔍",
                [
                    "Read stage-3 isolated contract",
                    "Find first EMA cross after S3 touch",
                    "Bullish -> same type · Bearish -> opposite type",
                    "Place paper order at cross price",
                ],
            )],
            status="started",
        ),
        level="INFO",
    )

    try:
        summary = strategy_2_order_service.run(reason="scheduled")

        # ---- Structured diagnosis (mirrors what Telegram shows) ----- #
        _log_diagnostics(summary)

        # ---- Telegram message + level ------------------------------- #
        telegram_level = _derive_telegram_level(summary)
        telegram_service.send_message(
            _build_completion_message(summary), level=telegram_level
        )

        # ---- Post-run aggregate log line ---------------------------- #
        logger.info(
            "[stage_4_s2 wrapper] completed — status=%s state=%s "
            "placed=%d sibling_resolved=%s entry_ltp_available=%s",
            str(summary.get("status") or "?"),
            str(summary.get("stage_3_summary_state") or "?"),
            int(summary.get("placed") or 0),
            str(summary.get("sibling_resolved")),
            str(summary.get("entry_ltp_available")),
        )
    except Exception as exc:
        logger.exception("[stage_4_s2 wrapper] Strategy 2 stage 4 failed")
        telegram_service.send_message(
            build(
                emoji="❌",
                title="Strategy 2 · Stage 4 · Failed",
                fields=[("Error", f"{type(exc).__name__}: {exc}")],
                status="failed",
            ),
            level="ERROR",
        )