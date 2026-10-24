# File: routes/admin_routes.py
"""Admin endpoints + the unified refresh routine.

Endpoints:
    POST /admin/hard-refresh          -> full refresh (unified routine)
    GET  /admin/backup-status         -> last backup extraction summary
    GET  /admin/cleanup-status        -> last folder-cleanup summary
    POST /admin/cleanup               -> run the folder cleanup standalone

Backfill on late startup
------------------------
If the app starts during market hours on a trading day and either of
today's scheduled jobs was missed, they are backfilled sequentially as
part of startup:

    * daily_refresh    — if no run recorded today (meta_data guard)
    * first_candle     — if first_candle.json is missing / stale for today

The pipeline order becomes:

    1.  Telegram start notification
    2.  Cleanup transient folders
    3.  Refresh token cache from MongoDB
    4.  Validate Upstox token
    5.  Backup extract from MongoDB
    6.  Reload instrument registry
    6b. Pre-register core indexes (Nifty 50, SENSEX)
    6c. BACKFILL: daily_refresh (if missed today)
    6d. Loop wiring
    6e. BACKFILL: first_candle (if missed today)
    7.  Live feed
    8.  Scheduler
    9.  Telegram bot
    10. Strategy state sanity check
    11. Telegram completion notification
    12. Return structured result

Strategy state sanity check
---------------------------
Before the completion message, ``_run_full_refresh_sync`` reads
``strategy_state_service.get_state()`` and logs the day's gate for
diagnostics. This is a read-only probe; it never mutates the state.
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
from services.backfill_guard import ran_today
from services.backup_data_service import backup_data_service
from services.cleanup_service import cleanup_service
from services.instrument_registry import instrument_registry
from services.live_broadcast import live_broadcast
from services.live_feed_service import (
    DEFAULT_CORE_INSTRUMENTS,
    live_feed_service,
)
from services.market_holiday_service import market_holiday_service
from services.normalization import normalize_underlying
from services.scheduler_service import scheduler_service
from services.telegram_service import telegram_service
from services.token_service import token_service
from token_tasks.telegram_token_bot import telegram_token_bot
from token_tasks.token_monitor import check_upstox_token_validity


logger = get_logger(__file__)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Decorative constants                                                        #
# --------------------------------------------------------------------------- #
_DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
_THIN = "────────────────────────────────"


# --------------------------------------------------------------------------- #
# Time / market-hours helpers                                                 #
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
_SESSION_OPEN = dtime(9, 15)
_SESSION_CLOSE = dtime(15, 30)


def _in_market_hours() -> bool:
    return _CATCHUP_WINDOW_OPEN <= _now_market_time() <= _CATCHUP_WINDOW_CLOSE


def _post_market() -> bool:
    return _now_market_time() > dtime(15, 30)


def _in_active_session() -> bool:
    """True when now is inside the live trading session Mon-Fri."""
    now = _now_market()
    if now.weekday() > 4:
        return False
    return _SESSION_OPEN <= now.time() <= _SESSION_CLOSE


def _is_trading_holiday_today() -> bool:
    """True when today is a market holiday (best-effort)."""
    try:
        payload = market_holiday_service.get_today()
        return str(payload.get("status") or "").lower() == "holiday"
    except Exception:
        logger.exception("[refresh] holiday check failed — assuming trading day")
        return False


def _catchup_snapshot() -> dict[str, Any]:
    now = _now_market_time()
    return {
        "current_time_ist": now.isoformat(),
        "in_market_hours": _in_market_hours(),
        "post_market": _post_market(),
        "in_active_session": _in_active_session(),
        "catchup_window": {
            "open": _CATCHUP_WINDOW_OPEN.isoformat(),
            "close": _CATCHUP_WINDOW_CLOSE.isoformat(),
        },
    }


# --------------------------------------------------------------------------- #
# Telegram bot state helper                                                   #
# --------------------------------------------------------------------------- #
def _telegram_bot_active(fallback: bool = False) -> bool:
    for attr in ("is_running", "polling", "enabled"):
        value = getattr(telegram_token_bot, attr, None)
        if isinstance(value, bool):
            return value
    return fallback


# --------------------------------------------------------------------------- #
# Core-index pre-registration                                                 #
# --------------------------------------------------------------------------- #
def _safe_folder_for(name: Any) -> str:
    text = str(name or "unknown").strip()
    for ch in ("|", "/", "\\", ":", "*", "?", '"', "<", ">", " "):
        text = text.replace(ch, "_")
    return text.strip("._") or "unknown"


def _ensure_core_indexes_registered() -> int:
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
            k, trading_symbol=symbol, underlying=underlying,
        )
        if not existed_before:
            created += 1
            logger.info(
                "[refresh] pre-registered index %s (symbol=%s, underlying=%s)",
                k, symbol, underlying,
            )

        if entry and not entry.get("ema_path"):
            canonical_dir = (
                Path(config.LIVE_DATA_DIR) / "index" / _safe_folder_for(k)
            )
            entry["ema_path"] = str(canonical_dir / "ema_crosses.json")
            entry["candles_path"] = str(canonical_dir / "candles.json")

    for key, symbol in DEFAULT_CORE_INSTRUMENTS:
        _register(key, symbol)

    return created


# --------------------------------------------------------------------------- #
# Backfill helpers                                                            #
# --------------------------------------------------------------------------- #
def _should_backfill_daily_refresh() -> tuple[bool, str]:
    """Decide if we should re-run the daily refresh as a startup backfill."""
    if not _in_active_session():
        return False, "outside_active_session"
    if _is_trading_holiday_today():
        return False, "market_holiday"
    if ran_today("daily_refresh"):
        return False, "already_ran_today"
    return True, "missed_today"


def _first_candle_file_for_first_instrument() -> Path | None:
    entries = instrument_registry.all()
    if not entries:
        return None
    from services.first_candle_service import _folder_for_entry
    try:
        return _folder_for_entry(entries[0]) / "first_candle.json"
    except Exception:
        return None


def _should_backfill_first_candle() -> tuple[bool, str]:
    """Decide if we should re-run first_candle as a startup backfill."""
    if not _in_active_session():
        return False, "outside_active_session"
    if _is_trading_holiday_today():
        return False, "market_holiday"
    if _now_market_time() < dtime(9, 16):
        return False, "before_first_candle_window"

    today = _now_market().date().isoformat()
    path = _first_candle_file_for_first_instrument()
    if path is None or not path.exists():
        return True, "missing_today"

    try:
        import json
        payload = json.loads(path.read_text(encoding="utf-8"))
        if str(payload.get("date") or "") != today:
            return True, "stale_date"
    except Exception:
        return True, "unparseable"

    return False, "already_computed_today"


# --------------------------------------------------------------------------- #
# Telegram formatting helpers                                                 #
# --------------------------------------------------------------------------- #
def _start_message(reason: str, catchup_meta: dict[str, Any]) -> str:
    return (
        f"{_DIV}\n"
        f"🚀 {reason.upper()} REFRESH STARTED\n"
        f"{_DIV}\n"
        f"🕒 Market time:   {_now_market().strftime('%H:%M:%S')} IST\n"
        f"🎯 Mode:          {reason}\n"
        f"🪟 In session:    "
        f"{'yes' if catchup_meta['in_active_session'] else 'no'}\n"
        f"🌙 Post-market:   "
        f"{'yes' if catchup_meta['post_market'] else 'no'}\n"
        f"{_THIN}\n"
        f"Pipeline order:\n"
        f"  🧹 cleanup → 🔑 token → 📦 backup → 📊 registry\n"
        f"  → 🔧 wiring → 📡 feed → ⏰ scheduler → 🤖 telegram"
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
    feed_ok: bool,
    scheduler_ok: bool,
    telegram_ok: bool,
    first_candle_summary: dict[str, Any] | None,
    status: str,
) -> str:
    fc_line = ""
    if first_candle_summary and not first_candle_summary.get("skipped"):
        fc_line = (
            f"🎯 OR levels:     {first_candle_summary.get('computed')}/"
            f"{first_candle_summary.get('total')}  "
            f"crosses={first_candle_summary.get('crosses_detected')}\n"
        )
    elif first_candle_summary and first_candle_summary.get("skipped"):
        fc_line = (
            f"⏸️ OR levels:     skipped ({first_candle_summary.get('skip_reason')})\n"
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
        f"{fc_line}"
        f"{_THIN}\n"
        f"📡 Live feed:     {'✓' if feed_ok else '✗'}\n"
        f"⏰ Scheduler:     {'✓' if scheduler_ok else '✗'}\n"
        f"🤖 Telegram bot:  {'✓' if telegram_ok else '✗'}\n"
        f"{_DIV}\n"
        f"✅ Status: {status}"
    )


# --------------------------------------------------------------------------- #
# Unified refresh                                                             #
# --------------------------------------------------------------------------- #
def _run_full_refresh_sync(
    reason: str = "manual",
    loop: "asyncio.AbstractEventLoop | None" = None,
) -> dict[str, Any]:
    """Execute every infrastructure refresh task in canonical order.

    On startup, additional backfill stages run when today's scheduled
    jobs were missed (late start during market hours):
        * daily_refresh  (if not yet run today)
        * first_candle   (if today's first_candle.json is missing)
    """
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
        "current_time_ist=%s",
        reason,
        catchup_meta["in_market_hours"],
        catchup_meta["post_market"],
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
            "reason": reason, "removed": [], "skipped": [],
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
        added = _ensure_core_indexes_registered()
        registry = {
            "count": len(instrument_registry.all()),
            "data_dir": (registry or {}).get("data_dir"),
            "ad_hoc_added": added,
        }
        logger.info(
            "[%s] index pre-registration: +%d new ad-hoc entries, registry=%d",
            reason, added, registry["count"],
        )
    except Exception:
        logger.exception("[%s] index pre-registration failed", reason)

    # ---- 4b. BACKFILL: daily_refresh (only on startup) ---------------- #
    backfill_daily: dict[str, Any] | None = None
    if reason == "startup":
        should, why = _should_backfill_daily_refresh()
        if should:
            logger.warning(
                "[startup] BACKFILL ACTIVATED — daily_refresh missed today (%s)",
                why,
            )
            try:
                from services.backfill_guard import mark_ran
                # Already inside the refresh pipeline — the daily_refresh
                # wrapper would recurse. So we just re-use the same result
                # and mark it as ran.
                mark_ran("daily_refresh", reason="startup_backfill", status="ok")
                backfill_daily = {
                    "activated": True,
                    "reason": why,
                    "note": "existing startup pipeline serves as the daily refresh for today",
                }
            except Exception:
                logger.exception("[startup] daily_refresh backfill marker failed")
        else:
            logger.info(
                "[startup] daily_refresh backfill skipped — %s", why,
            )
            backfill_daily = {"activated": False, "reason": why}

    # ---- 5. Loop wiring ---------------------------------------------- #
    if loop is not None:
        try:
            live_broadcast.set_loop(loop)
            live_feed_service.set_loop(loop)
        except Exception:
            logger.exception("[%s] loop wiring failed", reason)

    # ---- 6. Live feed ------------------------------------------------- #
    feed_ok = False
    try:
        feed_running = bool(live_feed_service.status().get("running"))
        if feed_running:
            feed_ok = live_feed_service.restart()
        else:
            feed_ok = live_feed_service.start()
    except Exception:
        logger.exception("[%s] live feed start/restart failed", reason)

    # ---- 6b. BACKFILL: first_candle (only on startup) ---------------- #
    first_candle_summary: dict[str, Any] | None = None
    if reason == "startup":
        should, why = _should_backfill_first_candle()
        if should:
            logger.warning(
                "[startup] BACKFILL ACTIVATED — first_candle missed today (%s)",
                why,
            )
            try:
                from services.first_candle_service import first_candle_service
                first_candle_summary = first_candle_service.run(reason="startup_backfill")
                first_candle_summary["backfill_reason"] = why
                logger.info(
                    "[startup] first_candle backfill: computed=%d crosses=%d "
                    "skipped=%d errors=%d elapsed=%ss",
                    first_candle_summary.get("computed", 0),
                    first_candle_summary.get("crosses_detected", 0),
                    first_candle_summary.get("instrument_skipped", 0),
                    first_candle_summary.get("errors", 0),
                    first_candle_summary.get("elapsed_seconds", 0.0),
                )
            except Exception:
                logger.exception("[startup] first_candle backfill failed")
        else:
            logger.info(
                "[startup] first_candle backfill skipped — %s", why,
            )

    # ---- 7. Scheduler ------------------------------------------------ #
    scheduler_ok = True
    try:
        scheduler_service.start()
    except Exception:
        logger.exception("[%s] scheduler start failed", reason)
        scheduler_ok = False

    # ---- 8. Telegram bot --------------------------------------------- #
    telegram_ok = False
    try:
        telegram_token_bot.start()
        telegram_ok = _telegram_bot_active(fallback=True)
    except Exception:
        logger.exception("[%s] telegram bot start failed", reason)
        telegram_ok = False

    if not telegram_ok:
        logger.warning(
            "[%s] telegram bot is NOT polling — start() returned but "
            "bot state is inactive (Windows prompt declined/timed out, "
            "or TELEGRAM_BOT_DISABLED set).",
            reason,
        )

    # ---- 9. Strategy state sanity (read-only) ----------------------- #
    # Log the day's strategy gate so a refresh (startup / daily /
    # manual) leaves a trace of which strategy is active, whether
    # Strategy 1 has fired, and what the NIFTY / SENSEX averages
    # were for the day. Read-only — never mutates the state.
    try:
        from services.strategy_state_service import strategy_state_service
        state = strategy_state_service.get_state()
        logger.info(
            "[%s] strategy_state — day=%s s1_eligible=%s s1_active=%s "
            "s1_fired=%s s2_active=%s nifty_avg=%s sensex_avg=%s",
            reason,
            state.get("day"),
            state.get("strategy1_eligible"),
            state.get("strategy1_active"),
            state.get("strategy1_fired"),
            state.get("strategy2_active"),
            state.get("nifty_avg"),
            state.get("sensex_avg"),
        )
    except Exception:
        logger.exception("[%s] strategy_state check failed", reason)

    result: dict[str, Any] = {
        "reason": reason,
        "mode": reason,
        "market_snapshot": catchup_meta,
        "cleanup": cleanup,
        "token_refresh_ok": token_refresh_ok,
        "token_valid": token_valid,
        "token_status": token_status,
        "backup": backup,
        "registry": registry,
        "backfill_daily_refresh": backfill_daily,
        "first_candle": first_candle_summary,
        "feed_restarted": feed_ok,
        "scheduler_started": scheduler_ok,
        "telegram_started": telegram_ok,
    }

    # ---- 10. Telegram completion ------------------------------------ #
    status = "SUCCESS" if backup.get("status") == "ok" else "WARNING"

    message = _completion_message(
        reason, catchup_meta,
        cleanup=cleanup,
        token_refresh_ok=token_refresh_ok,
        token_valid=token_valid,
        token_status=token_status,
        backup=backup,
        registry=registry,
        feed_ok=feed_ok,
        scheduler_ok=scheduler_ok,
        telegram_ok=telegram_ok,
        first_candle_summary=first_candle_summary,
        status=status,
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
# HTTP routes                                                                 #
# --------------------------------------------------------------------------- #
@router.post("/admin/hard-refresh")
async def hard_refresh() -> dict[str, Any]:
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