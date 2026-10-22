# File: token_tasks/strategy_selection_task.py
"""Scheduled wrapper for Strategy 1 — Stage 1.

Nicely formatted Telegram messages with icons, dividers, sections.

Diagnostics (added for the 2026-10-22 observability patch set)
--------------------------------------------------------------
This module used ``logging.getLogger(__name__)``, which means every
log line from it was console-only — nothing reached
``logs/strategy_selection_task.log`` or
``logs_text/strategy_selection_task.txt``. Combined with the completion
message not surfacing Stage 1's per-strike OR-write and feed-subscribe
counters, an operator had no way to tell from the log stream whether:

    * the 30-day history fetch succeeded for each strike,
    * the Upstox instrument_key resolved for each strike,
    * the strike was subscribed to the live feed,
    * the strike's own opening_range.json was written into the
      Stage-1 strike folder (the file the S1/S2 trackers read).

On 2026-10-22 all four of those failed silently for the 68 new
strikes, and the only symptom was ``no_activations`` from Stage 4
thirty minutes later.

This wrapper now:

    * uses ``get_logger(__file__)`` so every line reaches the file
      handlers;
    * emits one ``[stage_1 wrapper] starting`` INFO line before the
      service runs, naming the trigger, the buffer mode, and the
      configured index list;
    * emits one aggregate ``[stage_1 wrapper] complete`` INFO line
      with every Stage-1 counter (strikes / contracts / resolved /
      subscribed / or_written) summed across indexes, so a healthy
      run is one grep away;
    * emits a structured ``[stage_1 wrapper] diagnosis`` block with
      one INFO or WARNING line per index depending on the outcome:

          - 0 contracts produced        → WARNING (Stage 1 failed)
          - resolved < contracts        → WARNING (Upstox search
                                           missed some strikes)
          - subscribed < resolved       → WARNING (live-feed
                                           subscription failed for
                                           some strikes)
          - or_written < resolved       → WARNING (strike's own OR
                                           was not written, so the
                                           trackers will read
                                           isolation_level=None)
          - all counts equal            → INFO (healthy)

    * carries the same counters into the Telegram completion message
      under a new "Pipeline health" section, so the same info is
      visible from the phone;
    * raises the Telegram level to WARNING whenever any counter
      diverges, so the Oct 22 shape is flagged on the phone instead
      of arriving as a benign "✅ 2/2 indexes ok" message;
    * flags whether the 09:15 candle came from the live engine or the
      REST fallback (``or_candle_source``) and the per-index OR retry
      count (``or_retry_attempts``), which are the two data-path
      signals that would have closed the Oct 22 investigation early.
"""

from __future__ import annotations

from core import config
from core.logger import get_logger
from services.strategy_selection_service import strategy_selection_service
from services.telegram_service import telegram_service
from services.telegram_format import build

logger = get_logger(__file__)


# --------------------------------------------------------------------------- #
# Counter helpers                                                             #
# --------------------------------------------------------------------------- #
def _totals(indexes: list[dict]) -> dict[str, int]:
    """Return aggregate counters across per-index Stage-1 summaries."""
    return {
        "strikes": sum(int(i.get("strike_count") or 0) for i in indexes),
        "contracts": sum(int(i.get("contract_count") or 0) for i in indexes),
        "resolved": sum(int(i.get("resolved_count") or 0) for i in indexes),
        "subscribed": sum(
            int(i.get("subscribed_count") or 0) for i in indexes
        ),
        "or_written": sum(
            int(i.get("or_written_count") or 0) for i in indexes
        ),
    }


def _count_or_sources(indexes: list[dict]) -> dict[str, int]:
    """Count how many indexes used each OR candle source.

    Typical values are ``"live_engine"`` (the 09:15 candle was
    finalized on time by the WebSocket feed) and
    ``"rest_intraday_fallback"`` (the live engine did not yet have the
    candle, so the REST endpoint was used). A non-zero
    ``rest_intraday_fallback`` count means the OR → Stage-1 race was
    hit; that is not an error, but it is worth seeing in the digest.
    """
    srcs: dict[str, int] = {}
    for i in indexes:
        src = str(i.get("or_candle_source") or "unknown")
        srcs[src] = srcs.get(src, 0) + 1
    return srcs


# --------------------------------------------------------------------------- #
# Structured log diagnosis                                                    #
# --------------------------------------------------------------------------- #
def _log_diagnostics(summary: dict, totals: dict[str, int]) -> None:
    """Emit one structured per-index + aggregate diagnosis block.

    A healthy run is one INFO line. Any counter divergence produces a
    targeted WARNING that names the failing sub-step of Stage 1, so an
    operator can tell from the log alone which of the four data-path
    steps broke:

        1. history fetch    (→ resolved_count)
        2. feed subscribe   (→ subscribed_count)
        3. per-strike OR    (→ or_written_count)
        4. buffer window    (→ contracts == 0)
    """
    indexes = summary.get("indexes") or []
    total_contracts = totals["contracts"]
    total_resolved = totals["resolved"]
    total_subscribed = totals["subscribed"]
    total_or_written = totals["or_written"]

    # ---- Aggregate line ------------------------------------------------ #
    logger.info(
        "[stage_1 wrapper] diagnosis — indexes=%d strikes=%d "
        "contracts=%d resolved=%d subscribed=%d or_written=%d",
        len(indexes),
        totals["strikes"],
        total_contracts,
        total_resolved,
        total_subscribed,
        total_or_written,
    )

    # ---- Four aggregate failure signals -------------------------------- #
    if total_contracts == 0:
        logger.warning(
            "[stage_1 wrapper] Stage 1 produced 0 contracts across all "
            "%d index(es). The pipeline cannot proceed — Stage 2 will "
            "skip with reason=stage_1_empty and Stage 4 will report "
            "no_activations. Check the per-index reasons in "
            "data/strategy/stage_1_initial/stage_1_summary.json. "
            "Common causes: Opening Range unavailable (late 09:15 "
            "candle), buffer window empty, or STRATEGY_ENABLED=false.",
            len(indexes),
        )
    else:
        if total_resolved < total_contracts:
            logger.warning(
                "[stage_1 wrapper] %d/%d contracts resolved to a real "
                "Upstox key — %d did not. Unresolved contracts cannot "
                "be subscribed to the live feed or traded. Check the "
                "Upstox search results for the unresolved strikes in "
                "each strike's strategy_meta.json "
                "(upstox_instrument_key should be non-empty).",
                total_resolved,
                total_contracts,
                total_contracts - total_resolved,
            )
        if total_subscribed < total_resolved:
            logger.warning(
                "[stage_1 wrapper] %d/%d resolved contracts were "
                "subscribed to the live feed — %d did not. The R2/S3 "
                "trackers read live_feed_service.get_latest_ltp() and "
                "will get None for these contracts. Check "
                "GET /live/status (feed.instrument_count vs "
                "/live/instruments count).",
                total_subscribed,
                total_resolved,
                total_resolved - total_subscribed,
            )
        if total_or_written < total_resolved:
            logger.warning(
                "[stage_1 wrapper] %d/%d resolved contracts have their "
                "own opening_range.json in the strike folder — %d do "
                "not. The S1/S2 trackers will read isolation_level=None "
                "for these contracts and Stage 4 will report "
                "no_activations. This is the 2026-10-22 failure "
                "signature. Verify opening_range_service.calculate_one("
                "key, target_folder=<strike folder>) is actually "
                "writing the file.",
                total_or_written,
                total_resolved,
                total_resolved - total_or_written,
            )

    # ---- Per-index breakdown ------------------------------------------- #
    for i in indexes:
        ticker = i.get("ticker") or "?"
        status = i.get("status") or "?"
        if status != "ok":
            reason = i.get("reason") or i.get("error") or "—"
            logger.warning(
                "[stage_1 wrapper] %s: status=%s reason=%s",
                ticker,
                status,
                reason,
            )
            continue

        contracts = int(i.get("contract_count") or 0)
        resolved = int(i.get("resolved_count") or 0)
        subscribed = int(i.get("subscribed_count") or 0)
        or_written = int(i.get("or_written_count") or 0)
        src = str(i.get("or_candle_source") or "?")
        retries = int(i.get("or_retry_attempts") or 0)

        logger.info(
            "[stage_1 wrapper] %s: strikes=%d contracts=%d resolved=%d "
            "subscribed=%d or_written=%d src=%s retries=%d",
            ticker,
            int(i.get("strike_count") or 0),
            contracts,
            resolved,
            subscribed,
            or_written,
            src,
            retries,
        )


# --------------------------------------------------------------------------- #
# Telegram completion message                                                 #
# --------------------------------------------------------------------------- #
def _build_completion_message(summary: dict) -> str:
    indexes = summary.get("indexes") or []
    ok = sum(1 for i in indexes if i.get("status") == "ok")
    skipped = sum(1 for i in indexes if i.get("status") == "skipped")
    errors = sum(1 for i in indexes if i.get("status") == "error")
    totals = _totals(indexes)
    srcs = _count_or_sources(indexes)

    # ---- Per-index bullets -------------------------------------------- #
    index_bullets: list[str] = []
    for i in indexes:
        ticker = i.get("ticker") or "?"
        status = i.get("status") or "?"
        if status == "ok":
            # Include the counters so a diverge-from-contracts count is
            # immediately visible in the message.
            index_bullets.append(
                f"✅ {ticker}: {i.get('strike_count')} strikes · "
                f"{i.get('contract_count')} contracts · "
                f"res={i.get('resolved_count')} · "
                f"sub={i.get('subscribed_count')} · "
                f"or={i.get('or_written_count')}"
            )
            # Flag non-live OR source on its own line so a late 09:15
            # candle is visible without opening the logs.
            src = str(i.get("or_candle_source") or "")
            retries = int(i.get("or_retry_attempts") or 0)
            if src and src != "live_engine":
                index_bullets.append(
                    f"   ↳ OR source: {src}"
                    + (f" (retries={retries})" if retries else "")
                )
        else:
            reason = i.get("reason") or i.get("error") or "—"
            emoji = "⚠️" if status == "skipped" else "❌"
            index_bullets.append(f"{emoji} {ticker}: {status} ({reason})")

    # ---- Pipeline-health bullets -------------------------------------- #
    health_bullets: list[str] = []
    warnings: list[str] = []

    if totals["contracts"] > 0:
        if totals["resolved"] < totals["contracts"]:
            warnings.append(
                f"⚠️ {totals['contracts'] - totals['resolved']} "
                f"contract(s) unresolved — no Upstox key"
            )
        if totals["subscribed"] < totals["resolved"]:
            warnings.append(
                f"⚠️ {totals['resolved'] - totals['subscribed']} "
                f"contract(s) not subscribed — no live LTP"
            )
        if totals["or_written"] < totals["resolved"]:
            warnings.append(
                f"⚠️ {totals['resolved'] - totals['or_written']} "
                f"contract(s) missing strike OR — trackers will skip"
            )
    else:
        warnings.append(
            "⚠️ 0 contracts produced — Stage 2 / Stage 4 will be empty"
        )

    health_bullets.append(
        f"Resolved:    {totals['resolved']} / {totals['contracts']}"
    )
    health_bullets.append(
        f"Subscribed:  {totals['subscribed']} / {totals['resolved']}"
    )
    health_bullets.append(
        f"OR written:  {totals['or_written']} / {totals['resolved']}"
    )
    if srcs:
        src_str = ", ".join(f"{k}={v}" for k, v in sorted(srcs.items()))
        health_bullets.append(f"OR source:   {src_str}")

    return build(
        emoji="📊",
        title="Strategy 1 · Stage 1 · Completed",
        fields=[
            ("Mode", summary.get("mode")),
            ("Catch-up", "yes" if summary.get("catchup") else "no"),
            ("Post-market", "yes" if summary.get("post_market") else "no"),
            ("Scheduled", summary.get("scheduled_time_ist")),
            ("Actual", summary.get("actual_time_ist")),
            ("Avg buffer", summary.get("avg_buffer_points")),
            ("Buffer mode", summary.get("buffer_mode")),
        ],
        sections=[
            (
                "Index results",
                "🎯",
                [
                    f"Total: {len(indexes)} · ✅ {ok} · ⚠️ {skipped} · ❌ {errors}",
                    *index_bullets,
                ],
            ),
            (
                "Aggregate",
                "📈",
                [
                    f"Strikes: {totals['strikes']}",
                    f"Contracts: {totals['contracts']}",
                ],
            ),
            (
                "Pipeline health",
                "🔧",
                [*warnings, *health_bullets],
            ),
        ],
        status=(
            "success"
            if (ok > 0 and not warnings)
            else ("warning" if ok > 0 else "warning")
        ),
        status_detail=(
            f"{ok}/{len(indexes)} indexes ok"
            + (f" · {len(warnings)} warning(s)" if warnings else "")
        ),
    )


# --------------------------------------------------------------------------- #
# Public entry point                                                          #
# --------------------------------------------------------------------------- #
def run_strategy_selection() -> None:
    # ---- Pre-run log line --------------------------------------------- #
    indexes_cfg = config.STRATEGY_INDEXES or []
    if not isinstance(indexes_cfg, list):
        indexes_cfg = []
    ticker_list = ", ".join(
        str(i.get("ticker") or "?")
        for i in indexes_cfg
        if isinstance(i, dict)
    ) or "?"

    logger.info(
        "[stage_1 wrapper] starting — trigger=scheduled mode=scheduled "
        "buffer_mode=%s indexes=%s",
        getattr(config, "STRATEGY_BUFFER_MODE", "?"),
        ticker_list,
    )

    telegram_service.send_message(
        build(
            emoji="🚀",
            title="Strategy 1 · Stage 1 · Started",
            fields=[
                ("Buffer mode", "typical"),
                ("Trigger", "scheduled"),
            ],
            sections=[(
                "Doing",
                "🔍",
                [
                    "Buffer filter on OR midpoint",
                    "Upstox history fetch (30 days)",
                    "Last-day-cross tail analysis",
                    "Live-feed subscription + per-strike OR write",
                ],
            )],
            status="started",
        ),
        level="INFO",
    )

    try:
        summary = strategy_selection_service.run(reason="scheduled")
        indexes = summary.get("indexes") or []
        ok = sum(1 for i in indexes if i.get("status") == "ok")
        totals = _totals(indexes)

        # ---- Post-run structured diagnosis --------------------------- #
        _log_diagnostics(summary, totals)

        # Telegram level: WARNING whenever any counter diverges, even
        # if every index reports status=ok. A "successful" run with
        # 68 contracts and 0 subscribed is not a success — it is the
        # 2026-10-22 shape and must reach the phone as a warning.
        has_warnings = (
            totals["contracts"] == 0
            or totals["resolved"] < totals["contracts"]
            or totals["subscribed"] < totals["resolved"]
            or totals["or_written"] < totals["resolved"]
        )
        level = (
            "WARNING" if has_warnings
            else ("SUCCESS" if ok > 0 else "WARNING")
        )

        telegram_service.send_message(
            _build_completion_message(summary), level=level
        )

        logger.info(
            "[stage_1 wrapper] completed — ok=%d/%d strikes=%d "
            "contracts=%d resolved=%d subscribed=%d or_written=%d",
            ok,
            len(indexes),
            totals["strikes"],
            totals["contracts"],
            totals["resolved"],
            totals["subscribed"],
            totals["or_written"],
        )
    except Exception as exc:
        logger.exception("[stage_1 wrapper] Strategy 1 stage 1 failed")
        telegram_service.send_message(
            build(
                emoji="❌",
                title="Strategy 1 · Stage 1 · Failed",
                fields=[("Error", f"{type(exc).__name__}: {exc}")],
                status="failed",
            ),
            level="ERROR",
        )