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
    10b. Strategy stage 1 (buffer filter + last-day-cross)
    10c. Strategy stage 2 (EMA-cross isolation)
    10d. Strategy stage 3 tracker start (R2 activation watcher)
    10e. Strategy stage 3 catch-up scan (if past window, in catch-up window)
    10f. Strategy stage 4 catch-up order (if past 09:18, in catch-up window)
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
from services.telegram_service import telegram_service
from services.token_service import token_service
from token_tasks.telegram_token_bot import telegram_token_bot
from token_tasks.token_monitor import check_upstox_token_validity

logger = get_logger(__file__)

router = APIRouter()


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
        (
            f"Running unified refresh (mode={reason}).\n"
            f"Cleanup → token → backup → registry → wiring → OR → "
            f"stage_1 → stage_2 → stage_3 → stage_4 → backfill → feed → "
            f"scheduler → telegram.\n"
            f"In catch-up window: {catchup_meta['in_market_hours']}\n"
            f"Post-market (after 15:30): {catchup_meta['post_market']}\n"
            f"Past stage_4 time: {catchup_meta['past']['stage_4']}"
        ),
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
    }
    try:
        or_summary = opening_range_service.calculate_all()
    except Exception:
        logger.exception("[%s] opening range calculation failed", reason)

    # ---- 8b. Stage 1 -------------------------------------------------- #
    strategy_summary: dict[str, Any] = {
        "status": "not_run",
        "stage": "stage_1",
        "indexes": [],
        "avg_buffer_points": 0,
    }
    try:
        strategy_summary = strategy_selection_service.run(
            reason=reason,
            mode=reason,
            catchup=bool(
                catchup_meta["past"]["stage_1"] and catchup_meta["in_market_hours"]
            ),
        )
    except Exception:
        logger.exception("[%s] strategy stage 1 failed", reason)

    # ---- 8c. Stage 2 -------------------------------------------------- #
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
    try:
        isolation_summary = strategy_isolation_service.run(
            reason=reason,
            mode=reason,
            catchup=bool(
                catchup_meta["past"]["stage_2"] and catchup_meta["in_market_hours"]
            ),
        )
    except Exception:
        logger.exception("[%s] strategy stage 2 failed", reason)

    # ---- 8d. Stage 3 tracker ----------------------------------------- #
    tracker_started = False
    try:
        tracker_started = bool(strategy_live_tracker_service.start())
        if verbose:
            logger.info(
                "[%s] stage_3 tracker started=%s status=%s",
                reason,
                tracker_started,
                strategy_live_tracker_service.status(),
            )
        else:
            logger.info(
                "[%s] stage_3 tracker started=%s",
                reason,
                tracker_started,
            )
    except Exception:
        logger.exception("[%s] stage_3 tracker start failed", reason)

    # ---- 8e. Stage 3 catch-up ---------------------------------------- #
    stage3_catchup: dict[str, Any] = {
        "status": "not_run",
        "reason": "before_window_or_outside_catchup_window",
        "catchup": False,
    }
    if _should_run_stage_3_catchup():
        try:
            logger.info("[%s] stage_3 catch-up START", reason)
            stage3_catchup = strategy_live_tracker_service.run_catchup(
                mode=reason,
                catchup=True,
            )

            # Verbose mode: dump the full result dict.
            # Non-verbose: one compact summary line.
            if verbose:
                logger.info(
                    "[%s] stage_3 catch-up result: %s",
                    reason,
                    stage3_catchup,
                )
            else:
                logger.info(
                    "[%s] stage_3 catch-up COMPLETED — %s",
                    reason,
                    _summarise_stage3_catchup(stage3_catchup),
                )

            activated = int(stage3_catchup.get("activated_total") or 0)
            telegram_service.send_message(
                "Strategy Stage 3 Catch-up Executed",
                (
                    f"Mode: {reason}\n"
                    f"Catch-up: yes\n"
                    f"Post-market: {_post_market()}\n"
                    f"Window closed at "
                    f"{config.STRATEGY_TRACKER_WINDOW_END} IST\n"
                    f"Contracts activated: {activated}\n"
                    f"Indexes evaluated: "
                    f"{len(stage3_catchup.get('indexes') or [])}"
                ),
                level="SUCCESS" if activated else "INFO",
            )
        except Exception:
            logger.exception("[%s] stage_3 catch-up FAILED", reason)
            stage3_catchup = {
                "status": "error",
                "reason": "exception",
                "catchup": True,
            }
    else:
        logger.info(
            "[%s] stage_3 catch-up skipped (before window / outside catch-up)",
            reason,
        )

    # ---- 8f. Stage 4 catch-up (order) -------------------------------- #
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
                "[%s] stage_4 catch-up START (post_market=%s, scheduled=%s IST)",
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
                    "[%s] stage_4 catch-up result: %s",
                    reason,
                    order_summary,
                )
            else:
                logger.info(
                    "[%s] stage_4 catch-up COMPLETED — status=%s "
                    "candidates=%s picks=%s placed=%s",
                    reason,
                    order_summary.get("status"),
                    order_summary.get("candidates"),
                    order_summary.get("picks"),
                    placed,
                )

            post_market_line = (
                "Note: paper order built from the last cached LTP "
                "(no fresh ticks post-market).\n"
                if post_market
                else ""
            )
            telegram_service.send_message(
                "Strategy Stage 4 Catch-up Executed",
                (
                    f"Mode: {reason}\n"
                    f"Catch-up: yes\n"
                    f"Post-market: {post_market}\n"
                    f"Scheduled: "
                    f"{_scheduled_stage_4_time().strftime('%H:%M')} IST\n"
                    f"Actual: {_now_market().strftime('%H:%M:%S')} IST\n"
                    f"Candidates: {order_summary.get('candidates')}\n"
                    f"Picks: {order_summary.get('picks')}\n"
                    f"Placed: {placed}\n"
                    + post_market_line
                ),
                level="SUCCESS" if placed else "INFO",
            )
        except Exception:
            logger.exception("[%s] stage_4 catch-up FAILED", reason)
            order_summary = {
                "status": "error",
                "stage": "stage_4",
                "catchup": True,
                "reason": reason,
            }
    else:
        logger.info(
            "[%s] stage_4 catch-up skipped (before scheduled time / "
            "outside catch-up)",
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
        "isolation": isolation_summary,
        "tracker_started": tracker_started,
        "stage3_catchup": stage3_catchup,
        "orders": order_summary,
        "feed_restarted": feed_ok,
        "scheduler_started": scheduler_ok,
        "telegram_started": telegram_ok,
    }

    # ---- 12. Telegram completion ------------------------------------- #
    status = "SUCCESS" if backup.get("status") == "ok" else "WARNING"

    strategy_ok = sum(
        1 for i in strategy_summary.get("indexes", []) if i.get("status") == "ok"
    )
    strategy_total = len(strategy_summary.get("indexes", []))

    message = (
        f"Mode: {reason}\n"
        f"Post-market: {catchup_meta['post_market']}\n"
        f"Cleanup removed: {len(cleanup.get('removed') or [])}  "
        f"preserved: {len(cleanup.get('skipped') or [])}  "
        f"errors: {len(cleanup.get('errors') or [])}\n"
        f"Token refresh: {token_refresh_ok}  valid: {token_valid}  "
        f"status: {token_status}\n"
        f"Backup status: {backup.get('status')}  "
        f"files: {backup.get('file_count')}\n"
        f"Instruments loaded: {registry.get('count')}\n"
        f"EMA seeded: {backfill_summary.get('seeded')}/"
        f"{backfill_summary.get('total')}  "
        f"crosses: {backfill_summary.get('crosses')}\n"
        f"OR levels: {or_summary.get('calculated')}/"
        f"{or_summary.get('total')}  "
        f"skipped: {or_summary.get('skipped')}  "
        f"errors: {or_summary.get('errors')}\n"
        f"Stage 1 indexes: {strategy_ok}/{strategy_total}  "
        f"avg_buffer: {strategy_summary.get('avg_buffer_points')}\n"
        f"Stage 2 isolated: {isolation_summary.get('isolated')}/"
        f"{isolation_summary.get('evaluated')}  "
        f"pairs: {isolation_summary.get('pairs_isolated')}\n"
        f"Stage 3 tracker: {tracker_started}  "
        f"catch-up activated: {stage3_catchup.get('activated_total', 0)}\n"
        f"Stage 4 catch-up: {order_summary.get('catchup')}  "
        f"placed: {order_summary.get('placed', 0)}\n"
        f"Live feed running: {feed_ok}\n"
        f"Scheduler: {scheduler_ok}  Telegram bot: {telegram_ok}"
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