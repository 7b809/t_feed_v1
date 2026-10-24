# routes/admin_routes.py
"""Admin endpoints + the unified refresh routine.

    POST /admin/hard-refresh          -> full refresh (unified routine)
    GET  /admin/backup-status         -> last backup extraction summary
    GET  /admin/cleanup-status        -> last folder-cleanup summary
    POST /admin/cleanup               -> run the folder cleanup standalone

The unified refresh routine ``_run_full_refresh_sync`` is the
**single source of truth** for every task that runs in all three
states (startup, daily refresh, hard refresh).

Task list (all three states) — INFRASTRUCTURE ONLY:

    1.  Telegram start notification
    2.  Cleanup transient folders
    3.  Refresh token cache from MongoDB
    4.  Validate Upstox token
    5.  Backup extract from MongoDB
    6.  Reload instrument registry
    6b. Pre-register core indexes (Nifty 50, SENSEX)
    7.  Wire broadcast loop
    8.  Start / restart live feed
    9.  Start scheduler (idempotent)
    10. Start Telegram bot (idempotent)
    11. Telegram completion notification
    12. Return structured result

Strategy stages (EMA backfill seed, Opening Range calculation,
live-cross handler wiring) and ProChart have been removed from this
codebase entirely. The pipeline does not call them, and no admin
endpoint exposes them — the refresh is purely infrastructure /
token / feed / scheduler.
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
from services.live_broadcast import live_broadcast
from services.live_feed_service import (
    DEFAULT_CORE_INSTRUMENTS,
    live_feed_service,
)
from services.normalization import normalize_underlying
from services.scheduler_service import scheduler_service
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


def _in_market_hours() -> bool:
    return _CATCHUP_WINDOW_OPEN <= _now_market_time() <= _CATCHUP_WINDOW_CLOSE


def _post_market() -> bool:
    return _now_market_time() > dtime(15, 30)


def _catchup_snapshot() -> dict[str, Any]:
    now = _now_market_time()
    return {
        "current_time_ist": now.isoformat(),
        "in_market_hours": _in_market_hours(),
        "post_market": _post_market(),
        "catchup_window": {
            "open": _CATCHUP_WINDOW_OPEN.isoformat(),
            "close": _CATCHUP_WINDOW_CLOSE.isoformat(),
        },
    }


# --------------------------------------------------------------------------- #
# Telegram bot state helper                                                    #
# --------------------------------------------------------------------------- #
def _telegram_bot_active(fallback: bool = False) -> bool:
    """Return the bot's **actual** running/polling state.

    Reads ``telegram_token_bot.is_running`` (a truthful flag set only
    once polling begins), falling back to ``polling`` / ``enabled`` on
    older builds.
    """
    for attr in ("is_running", "polling", "enabled"):
        value = getattr(telegram_token_bot, attr, None)
        if isinstance(value, bool):
            return value
    return fallback


# --------------------------------------------------------------------------- #
# Core-index pre-registration                                                  #
# --------------------------------------------------------------------------- #
def _ensure_core_indexes_registered() -> int:
    """Pre-register NIFTY 50 and SENSEX so ``/live/*`` works out of the box."""
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
            # NOTE: ema_path / candles_path fields are still populated so
            # any downstream /live/* route that expects a valid path keeps
            # working. No engine consumes the file contents anymore, but
            # the registry field is harmless to keep and makes /live/snapshot
            # consistently return a path even for newly registered indexes.
            canonical_dir = (
                Path(config.LIVE_DATA_DIR) / "index" / _safe_folder_for(k)
            )
            entry["ema_path"] = str(canonical_dir / "ema_crosses.json")
            entry["candles_path"] = str(canonical_dir / "candles.json")

    for key, symbol in DEFAULT_CORE_INSTRUMENTS:
        _register(key, symbol)

    return created


def _safe_folder_for(name: Any) -> str:
    """Filesystem-safe representation of an instrument key.

    Kept local to ``_ensure_core_indexes_registered`` because the only
    remaining consumer of ``ema_path`` / ``candles_path`` is the
    registry itself (the strategy code that used ``_safe_folder`` was
    deleted).
    """
    text = str(name or "unknown").strip()
    for ch in ("|", "/", "\\", ":", "*", "?", '"', "<", ">", " "):
        text = text.replace(ch, "_")
    return text.strip("._") or "unknown"


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
    status: str,
) -> str:
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
        f"{_THIN}\n"
        f"📡 Live feed:     {'✓' if feed_ok else '✗'}\n"
        f"⏰ Scheduler:     {'✓' if scheduler_ok else '✗'}\n"
        f"🤖 Telegram bot:  {'✓' if telegram_ok else '✗'}\n"
        f"{_DIV}\n"
        f"✅ Status: {status}"
    )


# --------------------------------------------------------------------------- #
# Unified refresh (infrastructure only)                                       #
# --------------------------------------------------------------------------- #
def _run_full_refresh_sync(
    reason: str = "manual",
    loop: "asyncio.AbstractEventLoop | None" = None,
) -> dict[str, Any]:
    """Execute every infrastructure refresh task in canonical order.

    Strategy stages (EMA backfill seed, Opening Range calculation,
    live-cross handler wiring) and ProChart have been removed — this
    pipeline is infrastructure-only.
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
        added = _ensure_core_indexes_registered()
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

    # ---- 5. Loop wiring ---------------------------------------------- #
    # The live-cross handler was removed with the strategy layer, so
    # only the broadcast loop wiring remains here.
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
        "feed_restarted": feed_ok,
        "scheduler_started": scheduler_ok,
        "telegram_started": telegram_ok,
    }

    # ---- 9. Telegram completion ------------------------------------- #
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
        feed_ok=feed_ok,
        scheduler_ok=scheduler_ok,
        telegram_ok=telegram_ok,
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