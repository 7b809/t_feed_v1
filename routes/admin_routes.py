"""Admin endpoints + the unified refresh routine.

    POST /admin/hard-refresh          -> full refresh (unified routine)
    GET  /admin/backup-status         -> last backup extraction summary
    GET  /admin/cleanup-status        -> last folder-cleanup summary
    POST /admin/cleanup               -> run the folder cleanup standalone

The unified refresh routine ``_run_full_refresh_sync`` is the
**single source of truth** for every task that runs in all three
states (startup, daily refresh, hard refresh).

Task list (all three states):

    1.  Telegram start notification
    2.  Cleanup transient folders
    3.  Refresh token cache from MongoDB
    4.  Validate Upstox token
    5.  Backup extract from MongoDB
    6.  Reload instrument registry
    6b. Pre-register core + strategy indexes
    7.  Wire broadcast loop + cross handler
    8.  Reset today's live-cross state
    9.  EMA backfill seed (backup + intraday)
    10. Opening Range (OR) level calculation
    10b. Strategy 1 · Stage 1 (buffer filter + last-day-cross)
         + OR-retry guard: if every index skipped with
           ``or_unavailable:*``, force a full OR recalculation and
           re-run Stage 1 once.
    10c. Strategy 1 · Stage 2 (EMA-cross isolation)
         + skipped when Stage 1 produced zero contracts across all
           indexes (avoids zero-data "0 isolated" noise).
    10d. Strategy 1 · Stage 3 tracker start (R2 activation watcher)
    10e. Strategy 1 · Stage 3 catch-up scan (if past window, in catch-up window)
    10f. Strategy 1 · Stage 4 catch-up order (if past 09:18, in catch-up window)
    10g. Strategy 2 · Stage 3 tracker start (S3 touch watcher)
    10h. Strategy 2 · Stage 3 snapshot (isolation-level touch filter)
         + Telegram suppressed when Stage 2 has 0 contracts.
    10i. Strategy 2 · Stage 4 order (next-cross order, if isolated exists)
    11. Start / restart live feed
    12. Start scheduler (idempotent)
    13. Start Telegram bot (idempotent)
    14. Telegram completion notification
    15. Return structured result

Logging verbosity
-----------------
All bulk / per-instrument log lines are gated by
``config.STRATEGY_VERBOSE_LOG`` (env var ``STRATEGY_VERBOSE_LOG``,
default ``false``). When off, the pipeline logs only lifecycle
lines (START / COMPLETED / FAILED, per-index one-liners). Set it to
``true`` when debugging to see every per-instrument decision and
the full summary dicts.

Catch-up semantics
------------------
If the project starts (or daily/hard refresh fires) after a strategy
stage's scheduled time has already passed, and we are still inside the
catch-up window (09:15 – 23:59 IST), the stage is executed now and every
output is tagged with:

    execution_mode : startup | daily | manual | scheduled
    catchup        : true
    scheduled_time_ist / actual_time_ist

The cash-market close (15:30 IST) is reported separately as
``post_market=True`` — it does not block the catch-up, but it does
change the explanation recorded in the meta test files.

Strategy 2 notes
----------------
Strategy 2 reuses Stage 1 + Stage 2 of Strategy 1. Its own Stage 3
(S3-touch watcher) runs as a background thread all day, and its Stage 4
(next-cross order) fires when an isolated contract produces a fresh EMA
cross. Both are gated by ``STRATEGY_2_ENABLED``.

Strategy 2 Telegram suppression
-------------------------------
When Stage 2 has produced **zero** isolated contracts, the live
Strategy 2 Stage 3 snapshot in this refresh does not emit a Telegram
"Completed" message, because the only thing it could say is
"no contracts touched the isolation level yet" — that is the
zero-data message that used to fire every startup and every 5-minute
recheck. The scheduled wrapper
``token_tasks/strategy_2_isolation_task.py`` applies the same gate.

Strategy 1/Stage 2 zero-data gate
---------------------------------
When Stage 1 reports ``skipped`` for every index with a reason of
``or_unavailable:*``, Stage 2 is skipped with reason
``skipped_stage_1_empty`` and no Telegram completion is sent for
Stage 2 from this routine. This prevents the cascade
``Stage 1 skipped → Stage 2 0 isolated → Strategy 2 0 touched``
from producing three "empty" Telegram messages per refresh.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from core import config
from core.logger import get_logger
from services.backup_data_service import backup_data_service
from services.cleanup_service import cleanup_service
from services.instrument_registry import instrument_registry
from services.live_backfill_service import live_backfill_service
from services.live_broadcast import live_broadcast
from services.live_cross_store import live_cross_store
from services.live_ema_engine import live_ema_engine
from services.live_feed_service import (
    DEFAULT_CORE_INSTRUMENTS,
    live_feed_service,
)
from services.normalization import normalize_underlying
from services.opening_range_service import opening_range_service
from services.scheduler_service import scheduler_service
from services.strategy_isolation_service import strategy_isolation_service
from services.strategy_live_tracker_service import (
    strategy_live_tracker_service,
)
from services.strategy_order_service import strategy_order_service
from services.strategy_selection_service import strategy_selection_service
from services.strategy_2_isolation_service import strategy_2_isolation_service
from services.strategy_2_live_tracker_service import (
    strategy_2_live_tracker_service,
)
from services.daily_backup_service import daily_backup_service

from services.strategy_2_order_service import strategy_2_order_service
from services.telegram_service import telegram_service
from services.token_service import token_service
from token_tasks.telegram_token_bot import telegram_token_bot
from token_tasks.token_monitor import check_upstox_token_validity


logger = get_logger(__file__)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Decorative constants for Telegram messages                                  #
# --------------------------------------------------------------------------- #
_DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
_THIN = "────────────────────────────────"


# --------------------------------------------------------------------------- #
# Time / market-hours helpers                                                  #
# --------------------------------------------------------------------------- #
def _market_tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.MARKET_TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _now_market() -> datetime:
    return datetime.now(_market_tz())


def _now_market_time() -> dtime:
    return _now_market().time()


_CATCHUP_WINDOW_OPEN = dtime(9, 15)
_CATCHUP_WINDOW_CLOSE = dtime(23, 59)


def _scheduled_stage_1_time() -> dtime:
    base = (
        config.OPENING_RANGE_HOUR * 3600
        + config.OPENING_RANGE_MINUTE * 60
        + config.OPENING_RANGE_SECOND
    )
    total = (base + 20) % 86400
    return dtime(total // 3600, (total % 3600) // 60, total % 60)


def _scheduled_stage_2_time() -> dtime:
    base = (
        config.OPENING_RANGE_HOUR * 3600
        + config.OPENING_RANGE_MINUTE * 60
        + config.OPENING_RANGE_SECOND
    )
    total = (base + 50) % 86400
    return dtime(total // 3600, (total % 3600) // 60, total % 60)


def _scheduled_stage_4_time() -> dtime:
    return dtime(
        config.STRATEGY_ORDER_HOUR,
        config.STRATEGY_ORDER_MINUTE,
        0,
    )


def _in_market_hours() -> bool:
    return _CATCHUP_WINDOW_OPEN <= _now_market_time() <= _CATCHUP_WINDOW_CLOSE


def _post_market() -> bool:
    return _now_market_time() > dtime(15, 30)


def _catchup_snapshot() -> dict[str, Any]:
    now = _now_market_time()
    s1 = _scheduled_stage_1_time()
    s2 = _scheduled_stage_2_time()
    s4 = _scheduled_stage_4_time()
    return {
        "current_time_ist": now.isoformat(),
        "in_market_hours": _in_market_hours(),
        "post_market": _post_market(),
        "catchup_window": {
            "open": _CATCHUP_WINDOW_OPEN.isoformat(),
            "close": _CATCHUP_WINDOW_CLOSE.isoformat(),
        },
        "scheduled": {
            "stage_1": s1.isoformat(),
            "stage_2": s2.isoformat(),
            "stage_4": s4.isoformat(),
        },
        "past": {
            "stage_1": now >= s1,
            "stage_2": now >= s2,
            "stage_4": now >= s4,
        },
    }


def _should_run_stage_4_catchup() -> bool:
    return _in_market_hours() and _now_market_time() >= _scheduled_stage_4_time()


def _should_run_stage_3_catchup() -> bool:
    try:
        window_end = dtime(
            *[int(p) for p in str(config.STRATEGY_TRACKER_WINDOW_END).split(":")[:2]]
        )
    except Exception:
        window_end = dtime(9, 20)
    return _in_market_hours() and _now_market_time() > window_end


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _safe_folder(name: Any) -> str:
    text = str(name or "unknown").strip()
    for ch in ("|", "/", "\\", ":", "*", "?", '"', "<", ">", " "):
        text = text.replace(ch, "_")
    return text.strip("._") or "unknown"


def _ensure_core_and_strategy_indexes_registered() -> int:
    seen: set[str] = set()
    created = 0

    def _register(key: str, symbol: str | None = None) -> None:
        nonlocal created
        k = str(key or "").strip()
        if not k or k in seen:
            return
        seen.add(k)

        existed_before = instrument_registry.get(k) is not None
        underlying = normalize_underlying(symbol) or None

        entry = instrument_registry.register_adhoc(
            k,
            trading_symbol=symbol,
            underlying=underlying,
        )
        if not existed_before:
            created += 1
            logger.info(
                "[refresh] pre-registered index %s (symbol=%s, underlying=%s)",
                k,
                symbol,
                underlying,
            )

        if entry and not entry.get("ema_path"):
            canonical_dir = Path(config.LIVE_DATA_DIR) / "index" / _safe_folder(k)
            entry["ema_path"] = str(canonical_dir / "ema_crosses.json")
            entry["candles_path"] = str(canonical_dir / "candles.json")

    for key, symbol in DEFAULT_CORE_INSTRUMENTS:
        _register(key, symbol)

    for spec in config.STRATEGY_INDEXES or []:
        if not isinstance(spec, dict):
            continue
        _register(
            str(spec.get("instrument_key") or ""),
            str(spec.get("trading_symbol") or spec.get("index_name") or "") or None,
        )

    return created


def _wire_cross_handler() -> None:
    def on_cross(entry, cross):
        try:
            live_cross_store.save(entry, cross)
        except Exception:
            logger.exception(
                "Failed persisting live cross for %s",
                entry["instrument_key"],
            )
        try:
            live_broadcast.publish_sync("crosses", entry, {"cross": cross})
        except Exception:
            logger.exception("Failed broadcasting live cross")

    live_ema_engine.set_cross_handler(on_cross)


# --------------------------------------------------------------------------- #
# Compact summary helpers (used when STRATEGY_VERBOSE_LOG is False)           #
# --------------------------------------------------------------------------- #
def _summarise_stage3_catchup(summary: dict[str, Any]) -> str:
    """One-line summary of a stage-3 catch-up result."""
    if not isinstance(summary, dict):
        return "no summary"
    indexes = summary.get("indexes") or []
    parts: list[str] = []
    for idx in indexes:
        if not isinstance(idx, dict):
            continue
        parts.append(
            f"{idx.get('index_key') or idx.get('ticker') or '?'}: "
            f"status={idx.get('status')} "
            f"reason={idx.get('reason_code')} "
            f"activated={idx.get('contracts_activated', 0)}/"
            f"{idx.get('contracts_total', 0)}"
        )
    return (
        f"status={summary.get('status')} "
        f"activated_total={summary.get('activated_total', 0)} "
        f"indexes=[{' | '.join(parts)}]"
    )


def _summarise_strategy2_isolated(summary: dict[str, Any]) -> str:
    """Compact description of the Strategy 2 isolated pick (or 'none')."""
    isolated = (summary or {}).get("isolated") if isinstance(summary, dict) else None
    if not isolated:
        return "none"
    return (
        f"{isolated.get('ticker')} {isolated.get('strike_price')} "
        f"{isolated.get('strike_type')} @ "
        f"{isolated.get('isolation_level_name')}={isolated.get('isolation_level')}"
    )


def _stage1_all_skipped_or(summary: dict[str, Any]) -> bool:
    """Return True when every index in a Stage-1 summary was skipped
    because its Opening Range was unavailable.

    Matches the exact shape seen in the logs:

        NIFTY:  skipped (or_unavailable:not_calculated)
        SENSEX: skipped (or_unavailable:not_calculated)
    """
    if not isinstance(summary, dict):
        return False
    indexes = summary.get("indexes") or []
    if not indexes:
        return False
    for idx in indexes:
        if not isinstance(idx, dict):
            return False
        if idx.get("status") != "skipped":
            return False
        reason = str(idx.get("reason") or "")
        if not reason.startswith("or_unavailable:"):
            return False
    return True


def _count_stage1_contracts(summary: dict[str, Any]) -> int:
    """Sum contract_count across all indexes of a Stage-1 summary."""
    if not isinstance(summary, dict):
        return 0
    total = 0
    for idx in summary.get("indexes") or []:
        if not isinstance(idx, dict):
            continue
        try:
            total += int(idx.get("contract_count") or 0)
        except (TypeError, ValueError):
            continue
    return total


def _run_stage1_with_or_retry(
    *,
    reason: str,
    catchup_meta: dict[str, Any],
    verbose: bool,
) -> tuple[dict[str, Any], int, bool]:
    """Run Stage 1 and, on the OR-unavailable cascade, retry once.

    The per-instrument retry in
    ``strategy_selection_service._process_index`` normally recovers the
    index OR by forcing a single-instrument recalculation. This
    function is a second line of defence for the case where **every**
    index still skipped with ``or_unavailable:*`` — for example, if
    the REST fallback in ``opening_range_service`` also failed, or if
    the first pass raced the very first live tick. On that signal:

        1. Run ``opening_range_service.calculate_all()`` once more.
        2. Re-run Stage 1 once.

    Returns ``(summary, or_retry_attempted, or_retry_used)``:
        summary              : the final Stage-1 summary
        or_retry_attempted   : 1 if a retry was attempted, else 0
        or_retry_used        : True if the retried Stage 1 succeeded
    """
    strategy_summary = strategy_selection_service.run(
        reason=reason,
        mode=reason,
        catchup=bool(
            catchup_meta["past"]["stage_1"] and catchup_meta["in_market_hours"]
        ),
    )

    or_retry_attempted = 0
    or_retry_used = False

    if not _stage1_all_skipped_or(strategy_summary):
        return strategy_summary, or_retry_attempted, or_retry_used

    logger.warning(
        "[%s] strategy 1 stage 1: every index skipped with "
        "or_unavailable — forcing full OR recalc and re-running stage 1 once",
        reason,
    )
    or_retry_attempted = 1

    try:
        recalc = opening_range_service.calculate_all()
        if verbose:
            logger.info(
                "[%s] OR retry recalc summary: %s", reason, recalc
            )
        else:
            logger.info(
                "[%s] OR retry recalc — calculated=%s skipped=%s errors=%s",
                reason,
                recalc.get("calculated"),
                recalc.get("skipped"),
                recalc.get("errors"),
            )
    except Exception:
        logger.exception("[%s] OR retry recalc failed", reason)

    try:
        retried = strategy_selection_service.run(
            reason=reason,
            mode=reason,
            catchup=bool(
                catchup_meta["past"]["stage_1"]
                and catchup_meta["in_market_hours"]
            ),
        )
        if _stage1_all_skipped_or(retried):
            logger.warning(
                "[%s] stage 1 retry still OR-unavailable — leaving summary as skipped",
                reason,
            )
        else:
            or_retry_used = True
            strategy_summary = retried
            logger.info(
                "[%s] stage 1 retry succeeded after OR recalc — "
                "contracts=%d",
                reason,
                _count_stage1_contracts(retried),
            )
    except Exception:
        logger.exception("[%s] stage 1 retry failed", reason)

    return strategy_summary, or_retry_attempted, or_retry_used


# --------------------------------------------------------------------------- #
# Telegram formatting helpers                                                  #
# --------------------------------------------------------------------------- #
def _start_message(reason: str, catchup_meta: dict[str, Any]) -> str:
    return (
        f"{_DIV}\n"
        f"🚀 {reason.upper()} REFRESH STARTED\n"
        f"{_DIV}\n"
        f"🕒 Market time:   {_now_market().strftime('%H:%M:%S')} IST\n"
        f"🎯 Mode:          {reason}\n"
        f"🪟 Catch-up:      "
        f"{'yes' if catchup_meta['in_market_hours'] else 'no'}\n"
        f"🌙 Post-market:   "
        f"{'yes' if catchup_meta['post_market'] else 'no'}\n"
        f"⏭️ Past S1.S4:    "
        f"{catchup_meta['past']['stage_4']}\n"
        f"{_THIN}\n"
        f"Pipeline order:\n"
        f"  🧹 cleanup → 🔑 token → 📦 backup → 📊 registry\n"
        f"  → 🔧 wiring → 🎯 OR → 📊 S1.stage1 → 🔒 S1.stage2\n"
        f"  → 🎯 S1.stage3 → 💰 S1.stage4\n"
        f"  → 🎯 S2.stage3 → 💰 S2.stage4\n"
        f"  → 📡 feed → ⏰ scheduler → 🤖 telegram"
    )


def _completion_message(
    reason: str,
    catchup_meta: dict[str, Any],
    *,
    cleanup: dict[str, Any],
    token_refresh_ok: bool,
    token_valid: bool,
    token_status: str,
    backup: dict[str, Any],
    registry: dict[str, Any],
    backfill_summary: dict[str, Any],
    or_summary: dict[str, Any],
    strategy_summary: dict[str, Any],
    isolation_summary: dict[str, Any],
    tracker_started: bool,
    stage3_catchup: dict[str, Any],
    order_summary: dict[str, Any],
    strategy2_tracker_started: bool,
    strategy2_summary: dict[str, Any],
    strategy2_order_summary: dict[str, Any],
    feed_ok: bool,
    scheduler_ok: bool,
    telegram_ok: bool,
    status: str,
    stage1_or_retry_attempted: int = 0,
    stage1_or_retry_used: bool = False,
    stage2_skip_reason: str | None = None,
) -> str:
    s1_ok = sum(
        1
        for i in strategy_summary.get("indexes", [])
        if i.get("status") == "ok"
    )
    s1_total = len(strategy_summary.get("indexes", []))

    s2_isolated_line = _summarise_strategy2_isolated(strategy2_summary)

    # ---- Stage 1 OR-retry line --------------------------------------- #
    if stage1_or_retry_attempted:
        retry_note = (
            f"  (OR retry={'✓' if stage1_or_retry_used else '✗'})"
        )
    else:
        retry_note = ""

    # ---- Stage 2 status line ----------------------------------------- #
    if stage2_skip_reason:
        s2_line = (
            f"   🔒 Stage 2:    skipped — {stage2_skip_reason}\n"
        )
    else:
        s2_line = (
            f"   🔒 Stage 2:    {isolation_summary.get('isolated')}/"
            f"{isolation_summary.get('evaluated')} isolated  "
            f"pairs={isolation_summary.get('pairs_isolated')}\n"
        )

    # ---- Strategy 2 line --------------------------------------------- #
    if stage2_skip_reason:
        s2_strat_line = (
            f"   🎯 Stage 3:    tracker="
            f"{'✓' if strategy2_tracker_started else '✗'}  "
            f"skipped (no stage-2 contracts)\n"
            f"   🔒 Isolated:   none (stage-2 empty)\n"
        )
    else:
        s2_strat_line = (
            f"   🎯 Stage 3:    tracker="
            f"{'✓' if strategy2_tracker_started else '✗'}  "
            f"touched={strategy2_summary.get('touched_contracts', 0)}\n"
            f"   🔒 Isolated:   {s2_isolated_line}\n"
        )

    return (
        f"{_DIV}\n"
        f"🏁 {reason.upper()} REFRESH COMPLETED\n"
        f"{_DIV}\n"
        f"🕒 Market time:   {_now_market().strftime('%H:%M:%S')} IST\n"
        f"🌙 Post-market:   "
        f"{'yes' if catchup_meta['post_market'] else 'no'}\n"
        f"{_THIN}\n"
        f"🧹 Cleanup:       removed={len(cleanup.get('removed') or [])}  "
        f"preserved={len(cleanup.get('skipped') or [])}  "
        f"errors={len(cleanup.get('errors') or [])}\n"
        f"🔑 Token:         refresh={'✓' if token_refresh_ok else '✗'}  "
        f"valid={'✓' if token_valid else '✗'}  status={token_status}\n"
        f"📦 Backup:        status={backup.get('status')}  "
        f"files={backup.get('file_count')}\n"
        f"📊 Registry:      {registry.get('count')} instruments\n"
        f"📈 EMA seed:      {backfill_summary.get('seeded')}/"
        f"{backfill_summary.get('total')}  "
        f"crosses={backfill_summary.get('crosses')}\n"
        f"🎯 OR levels:     {or_summary.get('calculated')}/"
        f"{or_summary.get('total')}  "
        f"skipped={or_summary.get('skipped')}  "
        f"errors={or_summary.get('errors')}\n"
        f"{_THIN}\n"
        f"📊 STRATEGY 1\n"
        f"   📊 Stage 1:    {s1_ok}/{s1_total} indexes  "
        f"avg_buffer={strategy_summary.get('avg_buffer_points')}"
        f"{retry_note}\n"
        f"{s2_line}"
        f"   🎯 Stage 3:    tracker="
        f"{'✓' if tracker_started else '✗'}  "
        f"catch-up_activated={stage3_catchup.get('activated_total', 0)}\n"
        f"   💰 Stage 4:    catch-up="
        f"{order_summary.get('catchup')}  "
        f"placed={order_summary.get('placed', 0)}\n"
        f"{_THIN}\n"
        f"🔒 STRATEGY 2\n"
        f"{s2_strat_line}"
        f"   💰 Stage 4:    placed="
        f"{strategy2_order_summary.get('placed', 0)}\n"
        f"{_THIN}\n"
        f"📡 Live feed:     {'✓' if feed_ok else '✗'}\n"
        f"⏰ Scheduler:     {'✓' if scheduler_ok else '✗'}\n"
        f"🤖 Telegram bot:  {'✓' if telegram_ok else '✗'}\n"
        f"{_DIV}\n"
        f"✅ Status: {status}"
    )


# --------------------------------------------------------------------------- #
# Unified refresh                                                              #
# --------------------------------------------------------------------------- #
def _run_full_refresh_sync(
    reason: str = "manual",
    loop: "asyncio.AbstractEventLoop | None" = None,
) -> dict[str, Any]:
    """Execute every refresh task in the canonical order."""
    label = reason.title()
    catchup_meta = _catchup_snapshot()
    verbose = bool(getattr(config, "STRATEGY_VERBOSE_LOG", False))

    telegram_service.send_message(
        f"{label} Refresh Started",
        _start_message(reason, catchup_meta),
        level="STARTUP",
    )
    logger.info(
        "[%s] refresh start — in_market_hours=%s post_market=%s "
        "past_stage_4=%s current_time_ist=%s",
        reason,
        catchup_meta["in_market_hours"],
        catchup_meta["post_market"],
        catchup_meta["past"]["stage_4"],
        catchup_meta["current_time_ist"],
    )

    # ---- 1. Cleanup ---------------------------------------------------- #
    try:
        cleanup = cleanup_service.run(reason=reason)
        if verbose:
            logger.info("[%s] cleanup: %s", reason, cleanup)
        else:
            logger.info(
                "[%s] cleanup done — removed=%d preserved=%d errors=%d",
                reason,
                len(cleanup.get("removed") or []),
                len(cleanup.get("skipped") or []),
                len(cleanup.get("errors") or []),
            )
    except Exception as exc:
        logger.exception("[%s] cleanup failed", reason)
        cleanup = {
            "reason": reason,
            "removed": [],
            "skipped": [],
            "errors": [f"{type(exc).__name__}: {exc}"],
        }

    # ---- 2. Token cache refresh --------------------------------------- #
    token_refresh_ok = True
    try:
        token_service.refresh_tokens()
    except Exception:
        logger.exception("[%s] token refresh failed", reason)
        token_refresh_ok = False

    # ---- 3. Token validity check -------------------------------------- #
    token_valid = False
    token_status = "not_checked"
    try:
        token_result = check_upstox_token_validity(send_success_message=False)
        token_valid = bool(token_result.get("valid"))
        token_status = str(token_result.get("status", "unknown"))
    except Exception as exc:
        logger.exception("[%s] token validation failed", reason)
        token_status = f"error:{type(exc).__name__}"

    # ---- 4. Backup + registry ----------------------------------------- #
    backup = backup_data_service.refresh()
    registry = instrument_registry.load()

    try:
        added = _ensure_core_and_strategy_indexes_registered()
        registry = {
            "count": len(instrument_registry.all()),
            "data_dir": (registry or {}).get("data_dir"),
            "ad_hoc_added": added,
        }
        logger.info(
            "[%s] index pre-registration: +%d new ad-hoc entries, registry=%d",
            reason,
            added,
            registry["count"],
        )
    except Exception:
        logger.exception("[%s] index pre-registration failed", reason)

    # ---- 5. Wiring ---------------------------------------------------- #
    if loop is not None:
        try:
            live_broadcast.set_loop(loop)
            live_feed_service.set_loop(loop)
        except Exception:
            logger.exception("[%s] loop wiring failed", reason)
    try:
        _wire_cross_handler()
    except Exception:
        logger.exception("[%s] cross handler wiring failed", reason)

    # ---- 6. Reset cross state ----------------------------------------- #
    try:
        live_cross_store.reset_all()
    except Exception:
        logger.exception("[%s] live-cross reset failed", reason)

    # ---- 7. EMA backfill ---------------------------------------------- #
    backfill_summary: dict[str, Any] = {
        "seeded": 0,
        "crosses": 0,
        "skipped": 0,
        "errors": 0,
        "total": 0,
    }
    try:
        backfill_summary = live_backfill_service.seed_all()
    except Exception:
        logger.exception("[%s] EMA backfill failed", reason)

    # ---- 8. Opening Range --------------------------------------------- #
    or_summary: dict[str, Any] = {
        "status": "not_run",
        "total": 0,
        "calculated": 0,
        "skipped": 0,
        "errors": 0,
        "core_indexes_missing": 0,
    }
    try:
        or_summary = opening_range_service.calculate_all()
        # Diagnostic: how many of the strategy core indexes are missing?
        try:
            missing = 0
            for spec in config.STRATEGY_INDEXES or []:
                if not isinstance(spec, dict):
                    continue
                key = str(spec.get("instrument_key") or "").strip()
                if not key:
                    continue
                payload = opening_range_service.get_opening_range(key)
                if not payload.get("available") or not payload.get("is_today"):
                    missing += 1
            or_summary["core_indexes_missing"] = missing
        except Exception:
            logger.exception("[%s] OR core-index diagnostic failed", reason)
    except Exception:
        logger.exception("[%s] opening range calculation failed", reason)

    # ---- 8b. Strategy 1 · Stage 1 (with OR retry guard) --------------- #
    strategy_summary: dict[str, Any] = {
        "status": "not_run",
        "stage": "stage_1",
        "indexes": [],
        "avg_buffer_points": 0,
    }
    stage1_or_retry_attempted = 0
    stage1_or_retry_used = False
    try:
        (
            strategy_summary,
            stage1_or_retry_attempted,
            stage1_or_retry_used,
        ) = _run_stage1_with_or_retry(
            reason=reason,
            catchup_meta=catchup_meta,
            verbose=verbose,
        )
    except Exception:
        logger.exception("[%s] strategy 1 stage 1 failed", reason)

    # ---- 8c. Strategy 1 · Stage 2 ------------------------------------- #
    isolation_summary: dict[str, Any] = {
        "status": "not_run",
        "stage": "stage_2",
        "evaluated": 0,
        "isolated": 0,
        "pairs_isolated": 0,
        "skipped": 0,
        "errors": 0,
        "indexes": [],
    }
    stage2_skip_reason: str | None = None
    stage1_contracts = _count_stage1_contracts(strategy_summary)

    if stage1_contracts == 0:
        stage2_skip_reason = "stage_1_empty"
        logger.warning(
            "[%s] strategy 1 stage 2 skipped — stage 1 produced 0 contracts "
            "(or_retry_attempted=%d or_retry_used=%s)",
            reason,
            stage1_or_retry_attempted,
            stage1_or_retry_used,
        )
        isolation_summary = {
            "status": "skipped",
            "stage": "stage_2",
            "reason": stage2_skip_reason,
            "evaluated": 0,
            "isolated": 0,
            "pairs_isolated": 0,
            "skipped": 0,
            "errors": 0,
            "indexes": [],
        }
    else:
        try:
            isolation_summary = strategy_isolation_service.run(
                reason=reason,
                mode=reason,
                catchup=bool(
                    catchup_meta["past"]["stage_2"]
                    and catchup_meta["in_market_hours"]
                ),
            )
        except Exception:
            logger.exception("[%s] strategy 1 stage 2 failed", reason)

    # ---- 8d. Strategy 1 · Stage 3 tracker ----------------------------- #
    tracker_started = False
    try:
        tracker_started = bool(strategy_live_tracker_service.start())
        if verbose:
            logger.info(
                "[%s] strategy 1 stage 3 tracker started=%s status=%s",
                reason,
                tracker_started,
                strategy_live_tracker_service.status(),
            )
        else:
            logger.info(
                "[%s] strategy 1 stage 3 tracker started=%s",
                reason,
                tracker_started,
            )
    except Exception:
        logger.exception("[%s] strategy 1 stage 3 tracker start failed", reason)

    # ---- 8e. Strategy 1 · Stage 3 catch-up ---------------------------- #
    stage3_catchup: dict[str, Any] = {
        "status": "not_run",
        "reason": "before_window_or_outside_catchup_window",
        "catchup": False,
    }
    if _should_run_stage_3_catchup():
        try:
            logger.info("[%s] strategy 1 stage 3 catch-up START", reason)
            stage3_catchup = strategy_live_tracker_service.run_catchup(
                mode=reason,
                catchup=True,
            )

            if verbose:
                logger.info(
                    "[%s] strategy 1 stage 3 catch-up result: %s",
                    reason,
                    stage3_catchup,
                )
            else:
                logger.info(
                    "[%s] strategy 1 stage 3 catch-up COMPLETED — %s",
                    reason,
                    _summarise_stage3_catchup(stage3_catchup),
                )

            activated = int(stage3_catchup.get("activated_total") or 0)
            telegram_service.send_message(
                "Strategy 1 Stage 3 Catch-up Executed",
                (
                    f"{_THIN}\n"
                    f"🎯 STRATEGY 1 · STAGE 3 CATCH-UP\n"
                    f"{_THIN}\n"
                    f"🎯 Mode:          {reason}\n"
                    f"🪟 Catch-up:      yes\n"
                    f"🌙 Post-market:   {_post_market()}\n"
                    f"⏰ Window closed: {config.STRATEGY_TRACKER_WINDOW_END} IST\n"
                    f"📈 Activated:     {activated} contracts\n"
                    f"📊 Indexes:       "
                    f"{len(stage3_catchup.get('indexes') or [])}"
                ),
                level="SUCCESS" if activated else "INFO",
            )
        except Exception:
            logger.exception("[%s] strategy 1 stage 3 catch-up FAILED", reason)
            stage3_catchup = {
                "status": "error",
                "reason": "exception",
                "catchup": True,
            }
    else:
        logger.info(
            "[%s] strategy 1 stage 3 catch-up skipped "
            "(before window / outside catch-up)",
            reason,
        )

    # ---- 8f. Strategy 1 · Stage 4 catch-up (order) -------------------- #
    order_summary: dict[str, Any] = {
        "status": "not_run",
        "stage": "stage_4",
        "reason": "before_scheduled_time_or_outside_catchup_window",
        "catchup": False,
    }
    if _should_run_stage_4_catchup():
        try:
            post_market = _post_market()
            logger.info(
                "[%s] strategy 1 stage 4 catch-up START "
                "(post_market=%s, scheduled=%s IST)",
                reason,
                post_market,
                _scheduled_stage_4_time().strftime("%H:%M"),
            )
            order_summary = strategy_order_service.run(
                reason=reason,
                mode=reason,
                catchup=True,
            )
            placed = int(order_summary.get("placed") or 0)
            if verbose:
                logger.info(
                    "[%s] strategy 1 stage 4 catch-up result: %s",
                    reason,
                    order_summary,
                )
            else:
                logger.info(
                    "[%s] strategy 1 stage 4 catch-up COMPLETED — status=%s "
                    "candidates=%s picks=%s placed=%s",
                    reason,
                    order_summary.get("status"),
                    order_summary.get("candidates"),
                    order_summary.get("picks"),
                    placed,
                )

            post_market_line = (
                "\n🌙 Note: paper order built from cached LTP "
                "(no fresh ticks post-market)."
                if post_market
                else ""
            )
            telegram_service.send_message(
                "Strategy 1 Stage 4 Catch-up Executed",
                (
                    f"{_THIN}\n"
                    f"💰 STRATEGY 1 · STAGE 4 CATCH-UP\n"
                    f"{_THIN}\n"
                    f"🎯 Mode:          {reason}\n"
                    f"🪟 Catch-up:      yes\n"
                    f"🌙 Post-market:   {post_market}\n"
                    f"⏰ Scheduled:     "
                    f"{_scheduled_stage_4_time().strftime('%H:%M')} IST\n"
                    f"🕒 Actual:        "
                    f"{_now_market().strftime('%H:%M:%S')} IST\n"
                    f"📊 Candidates:    {order_summary.get('candidates')}\n"
                    f"🎯 Picks:         {order_summary.get('picks')}\n"
                    f"💰 Placed:        {placed}"
                    f"{post_market_line}"
                ),
                level="SUCCESS" if placed else "INFO",
            )
        except Exception:
            logger.exception("[%s] strategy 1 stage 4 catch-up FAILED", reason)
            order_summary = {
                "status": "error",
                "stage": "stage_4",
                "catchup": True,
                "reason": reason,
            }
    else:
        logger.info(
            "[%s] strategy 1 stage 4 catch-up skipped "
            "(before scheduled time / outside catch-up)",
            reason,
        )

    # ================================================================== #
    # STRATEGY 2                                                          #
    # ================================================================== #

    # ---- 8g. Strategy 2 · Stage 3 tracker start ----------------------- #
    strategy2_tracker_started = False
    try:
        if config.STRATEGY_2_ENABLED and config.STRATEGY_2_TRACKER_ENABLED:
            strategy2_tracker_started = bool(
                strategy_2_live_tracker_service.start()
            )
            if verbose:
                logger.info(
                    "[%s] strategy 2 stage 3 tracker started=%s status=%s",
                    reason,
                    strategy2_tracker_started,
                    strategy_2_live_tracker_service.status(),
                )
            else:
                logger.info(
                    "[%s] strategy 2 stage 3 tracker started=%s",
                    reason,
                    strategy2_tracker_started,
                )
        else:
            logger.info(
                "[%s] strategy 2 stage 3 tracker disabled by config", reason
            )
    except Exception:
        logger.exception("[%s] strategy 2 stage 3 tracker start failed", reason)

    # ---- 8h. Strategy 2 · Stage 3 snapshot ---------------------------- #
    strategy2_summary: dict[str, Any] = {
        "status": "not_run",
        "stage": "stage_3",
        "strategy": "strategy_2",
        "stage2_contracts": 0,
        "touched_contracts": 0,
        "isolated": None,
    }
    if config.STRATEGY_2_ENABLED:
        try:
            strategy2_summary = strategy_2_isolation_service.run(
                reason=reason,
                mode=reason,
                catchup=bool(catchup_meta["in_market_hours"]),
            )
            if verbose:
                logger.info(
                    "[%s] strategy 2 stage 3 result: %s",
                    reason,
                    strategy2_summary,
                )
            else:
                logger.info(
                    "[%s] strategy 2 stage 3 COMPLETE — level=%s "
                    "stage2=%d touched=%d isolated=%s",
                    reason,
                    strategy2_summary.get("isolation_level_name"),
                    strategy2_summary.get("stage2_contracts", 0),
                    strategy2_summary.get("touched_contracts", 0),
                    _summarise_strategy2_isolated(strategy2_summary),
                )
        except Exception:
            logger.exception("[%s] strategy 2 stage 3 failed", reason)

    # ---- 8i. Strategy 2 · Stage 4 (order) ----------------------------- #
    strategy2_order_summary: dict[str, Any] = {
        "status": "not_run",
        "stage": "stage_4",
        "strategy": "strategy_2",
        "placed": 0,
        "order": None,
    }
    s2_stage2_count = int(strategy2_summary.get("stage2_contracts") or 0)
    if (
        config.STRATEGY_2_ENABLED
        and s2_stage2_count > 0
        and strategy2_summary.get("isolated")
    ):
        try:
            logger.info("[%s] strategy 2 stage 4 START", reason)
            strategy2_order_summary = strategy_2_order_service.run(
                reason=reason,
                mode=reason,
                catchup=bool(catchup_meta["in_market_hours"]),
            )
            placed_s2 = int(strategy2_order_summary.get("placed") or 0)
            if verbose:
                logger.info(
                    "[%s] strategy 2 stage 4 result: %s",
                    reason,
                    strategy2_order_summary,
                )
            else:
                logger.info(
                    "[%s] strategy 2 stage 4 COMPLETE — status=%s placed=%d",
                    reason,
                    strategy2_order_summary.get("status"),
                    placed_s2,
                )

            if placed_s2:
                order = strategy2_order_summary.get("order") or {}
                telegram_service.send_message(
                    "Strategy 2 Order Placed",
                    (
                        f"{_THIN}\n"
                        f"💰 STRATEGY 2 · ORDER PLACED\n"
                        f"{_THIN}\n"
                        f"🎯 Mode:          {reason}\n"
                        f"🌙 Post-market:   {_post_market()}\n"
                        f"📊 Ticker:        {order.get('ticker')}\n"
                        f"📈 Strike:        {order.get('strike_price')}\n"
                        f"🏷️ Type:          {order.get('order_strike_type')}\n"
                        f"🎯 Direction:     "
                        f"{order.get('cross_type')} cross → "
                        f"{order.get('order_strike_type')}\n"
                        f"💵 Entry LTP:     {order.get('entry_ltp')} "
                        f"({order.get('entry_ltp_source')})\n"
                        f"🕒 Cross at:      {order.get('cross_minute')}\n"
                        f"🕒 S3 touch:      {order.get('s3_touch_minute')}\n"
                        f"🔑 Key:           {order.get('order_instrument_key')}"
                    ),
                    level="SUCCESS",
                )
        except Exception:
            logger.exception("[%s] strategy 2 stage 4 failed", reason)
            strategy2_order_summary = {
                "status": "error",
                "stage": "stage_4",
                "strategy": "strategy_2",
                "placed": 0,
                "order": None,
            }
    else:
        logger.info(
            "[%s] strategy 2 stage 4 skipped "
            "(disabled, no stage-2 contracts, or no isolated contract yet)",
            reason,
        )

    # ---- 9. Live feed ------------------------------------------------- #
    feed_ok = False
    try:
        feed_running = bool(live_feed_service.status().get("running"))
        if feed_running:
            feed_ok = live_feed_service.restart()
        else:
            feed_ok = live_feed_service.start()
    except Exception:
        logger.exception("[%s] live feed start/restart failed", reason)

    # ---- 10. Scheduler ------------------------------------------------ #
    scheduler_ok = True
    try:
        scheduler_service.start()
    except Exception:
        logger.exception("[%s] scheduler start failed", reason)
        scheduler_ok = False

    # ---- 11. Telegram bot --------------------------------------------- #
    telegram_ok = True
    try:
        telegram_token_bot.start()
    except Exception:
        logger.exception("[%s] telegram bot start failed", reason)
        telegram_ok = False

    result: dict[str, Any] = {
        "reason": reason,
        "mode": reason,
        "catchup": {
            "stage_1": bool(strategy_summary.get("catchup")),
            "stage_2": bool(isolation_summary.get("catchup")),
            "stage_3": bool(stage3_catchup.get("catchup")),
            "stage_4": bool(order_summary.get("catchup")),
        },
        "market_snapshot": catchup_meta,
        "cleanup": cleanup,
        "token_refresh_ok": token_refresh_ok,
        "token_valid": token_valid,
        "token_status": token_status,
        "backup": backup,
        "registry": registry,
        "backfill": backfill_summary,
        "opening_range": or_summary,
        "strategy": strategy_summary,
        "strategy_stage1_or_retry": {
            "attempted": stage1_or_retry_attempted,
            "used": stage1_or_retry_used,
        },
        "strategy_stage1_contracts": stage1_contracts,
        "isolation": isolation_summary,
        "stage2_skip_reason": stage2_skip_reason,
        "tracker_started": tracker_started,
        "stage3_catchup": stage3_catchup,
        "orders": order_summary,
        "strategy2_tracker_started": strategy2_tracker_started,
        "strategy2_stage3": strategy2_summary,
        "strategy2_stage4": strategy2_order_summary,
        "feed_restarted": feed_ok,
        "scheduler_started": scheduler_ok,
        "telegram_started": telegram_ok,
    }

    # ---- 12. Telegram completion ------------------------------------- #
    status = "SUCCESS" if backup.get("status") == "ok" else "WARNING"

    message = _completion_message(
        reason,
        catchup_meta,
        cleanup=cleanup,
        token_refresh_ok=token_refresh_ok,
        token_valid=token_valid,
        token_status=token_status,
        backup=backup,
        registry=registry,
        backfill_summary=backfill_summary,
        or_summary=or_summary,
        strategy_summary=strategy_summary,
        isolation_summary=isolation_summary,
        tracker_started=tracker_started,
        stage3_catchup=stage3_catchup,
        order_summary=order_summary,
        strategy2_tracker_started=strategy2_tracker_started,
        strategy2_summary=strategy2_summary,
        strategy2_order_summary=strategy2_order_summary,
        feed_ok=feed_ok,
        scheduler_ok=scheduler_ok,
        telegram_ok=telegram_ok,
        status=status,
        stage1_or_retry_attempted=stage1_or_retry_attempted,
        stage1_or_retry_used=stage1_or_retry_used,
        stage2_skip_reason=stage2_skip_reason,
    )
    telegram_service.send_message(
        (
            f"{label} Refresh Completed"
            if status == "SUCCESS"
            else f"{label} Refresh Finished"
        ),
        message,
        level=status,
    )
    logger.info("[%s] refresh COMPLETED — status=%s", reason, status)
    return result


# --------------------------------------------------------------------------- #
# HTTP routes                                                                  #
# --------------------------------------------------------------------------- #
@router.post("/admin/hard-refresh")
async def hard_refresh() -> dict[str, Any]:
    """Unified refresh triggered from the API."""
    return await asyncio.to_thread(_run_full_refresh_sync, "manual", None)


@router.get("/admin/backup-status")
def backup_status() -> dict[str, Any]:
    return backup_data_service.last_status()


@router.get("/admin/cleanup-status")
def cleanup_status() -> dict[str, Any]:
    return cleanup_service.last_status()


@router.post("/admin/cleanup")
async def run_cleanup() -> dict[str, Any]:
    return await asyncio.to_thread(cleanup_service.run, "manual")