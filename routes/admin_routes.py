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
    6b. Pre-register core + strategy indexes (normalized underlying)
    7.  Wire broadcast loop + cross handler
    8.  Reset today's live-cross state
    9.  EMA backfill seed (backup + intraday)
    10. Opening Range (OR) level calculation
    10b. Strategy stage 1 (buffer filter + history + last-day-cross)
    10c. Strategy stage 2 (EMA-cross isolation)
    10d. Strategy stage 3 tracker start (R2 activation watcher)
    11. Start / restart live feed
    12. Start scheduler (idempotent)
    13. Start Telegram bot (idempotent)
    14. Telegram completion notification
    15. Return structured result
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

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
from services.strategy_selection_service import strategy_selection_service
from services.telegram_service import telegram_service
from services.token_service import token_service
from token_tasks.telegram_token_bot import telegram_token_bot
from token_tasks.token_monitor import check_upstox_token_validity

logger = get_logger(__file__)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _safe_folder(name: Any) -> str:
    """Folder-safe version of a human label."""
    text = str(name or "unknown").strip()
    for ch in ("|", "/", "\\", ":", "*", "?", '"', "<", ">", " "):
        text = text.replace(ch, "_")
    return text.strip("._") or "unknown"


def _ensure_core_and_strategy_indexes_registered() -> int:
    """Pre-register NIFTY 50, SENSEX, and every configured strategy
    index into the instrument registry.

    Runs after ``instrument_registry.load()`` and BEFORE the backfill /
    OR / strategy-selection steps so those services can find index
    entries.

    The underlying stored on the entry is normalized via
    ``services.normalization`` so "NIFTY 50" and "NIFTY" resolve to
    the same canonical ticker for future strike lookups.

    The call is idempotent and non-destructive: an existing entry is
    backfilled through ``register_adhoc``'s merge path rather than
    being replaced, and only genuinely new entries are counted in the
    returned number.

    Returns the number of *new* ad-hoc entries created.
    """
    seen: set[str] = set()
    created = 0

    def _register(key: str, symbol: str | None = None) -> None:
        nonlocal created
        k = str(key or "").strip()
        if not k or k in seen:
            return
        seen.add(k)

        existed_before = instrument_registry.get(k) is not None

        # Normalize the underlying so both "NIFTY 50" and "NIFTY"
        # resolve to the same canonical ticker.
        underlying = normalize_underlying(symbol) or None

        # ``register_adhoc`` is idempotent — for an existing key it
        # backfills missing fields on the entry instead of replacing
        # it. That is exactly what we want here: if a chart URL
        # registered the index earlier without an underlying, this
        # call fills it in.
        entry = instrument_registry.register_adhoc(
            k, trading_symbol=symbol, underlying=underlying,
        )
        if not existed_before:
            created += 1
            logger.info(
                "[refresh] pre-registered index %s (symbol=%s, underlying=%s)",
                k, symbol, underlying,
            )

        # Give the index a canonical home inside data/index/ so its
        # opening_range.json and live_crosses.json land next to the
        # other index artefacts instead of the generic _adhoc tree.
        if entry and not entry.get("ema_path"):
            canonical_dir = (
                Path(config.LIVE_DATA_DIR) / "index" / _safe_folder(k)
            )
            entry["ema_path"] = str(canonical_dir / "ema_crosses.json")
            entry["candles_path"] = str(canonical_dir / "candles.json")

    # 1. Core indices (always)
    for key, symbol in DEFAULT_CORE_INSTRUMENTS:
        _register(key, symbol)

    # 2. Configured strategy indices (defensive; may overlap with core)
    for spec in (config.STRATEGY_INDEXES or []):
        if not isinstance(spec, dict):
            continue
        _register(
            str(spec.get("instrument_key") or ""),
            str(
                spec.get("trading_symbol")
                or spec.get("index_name")
                or ""
            ) or None,
        )

    return created


# --------------------------------------------------------------------------- #
# Cross handler wiring                                                         #
# --------------------------------------------------------------------------- #
def _wire_cross_handler() -> None:
    """Install the cross persistence + broadcast handler on the engine."""

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
# Unified refresh                                                              #
# --------------------------------------------------------------------------- #
def _run_full_refresh_sync(
    reason: str = "manual",
    loop: "asyncio.AbstractEventLoop | None" = None,
) -> dict[str, Any]:
    """Execute every refresh task in the canonical order."""
    label = reason.title()
    telegram_service.send_message(
        f"{label} Refresh Started",
        (
            "Running unified refresh: cleanup → token → backup → "
            "registry → index registration → wiring → OR levels → "
            "stage_1 → stage_2 → stage_3 tracker → backfill → feed → "
            "scheduler → telegram."
        ),
        level="STARTUP",
    )

    # ---- 1. Cleanup transient folders ------------------------------------
    try:
        cleanup = cleanup_service.run(reason=reason)
        logger.info("[%s] cleanup: %s", reason, cleanup)
    except Exception as exc:
        logger.exception("[%s] cleanup failed", reason)
        cleanup = {
            "reason": reason,
            "removed": [],
            "skipped": [],
            "errors": [f"{type(exc).__name__}: {exc}"],
        }

    # ---- 2. Token cache refresh ------------------------------------------
    token_refresh_ok = True
    try:
        token_service.refresh_tokens()
    except Exception:
        logger.exception("[%s] token refresh failed", reason)
        token_refresh_ok = False

    # ---- 3. Token validity check -----------------------------------------
    token_valid = False
    token_status = "not_checked"
    try:
        token_result = check_upstox_token_validity(
            send_success_message=False
        )
        token_valid = bool(token_result.get("valid"))
        token_status = str(token_result.get("status", "unknown"))
    except Exception as exc:
        logger.exception("[%s] token validation failed", reason)
        token_status = f"error:{type(exc).__name__}"

    # ---- 4. Backup extract + registry reload -----------------------------
    backup = backup_data_service.refresh()
    registry = instrument_registry.load()

    # ---- 4b. Pre-register core + strategy indices ------------------------
    # Critical: without this, the backfill / OR / strategy services see
    # only the option contracts extracted from the backup and cannot
    # find NIFTY 50 / SENSEX, producing or_unavailable:not_registered.
    try:
        added = _ensure_core_and_strategy_indexes_registered()
        registry = {
            "count": len(instrument_registry.all()),
            "data_dir": (registry or {}).get("data_dir"),
            "ad_hoc_added": added,
        }
        logger.info(
            "[%s] index pre-registration: +%d new ad-hoc entries, "
            "registry=%d",
            reason, added, registry["count"],
        )
    except Exception:
        logger.exception("[%s] index pre-registration failed", reason)

    # ---- 5. Wire broadcast loop + cross handler --------------------------
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

    # ---- 6. Reset today's live-cross state -------------------------------
    try:
        live_cross_store.reset_all()
    except Exception:
        logger.exception("[%s] live-cross reset failed", reason)

    # ---- 7. EMA backfill seed --------------------------------------------
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

    # ---- 8. Opening Range calculation ------------------------------------
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

    # ---- 8b. Strategy stage 1 — buffer filter + history + last-day cross -
    strategy_summary: dict[str, Any] = {
        "status": "not_run",
        "stage": "stage_1",
        "indexes": [],
        "avg_buffer_points": 0,
    }
    try:
        strategy_summary = strategy_selection_service.run(reason=reason)
    except Exception:
        logger.exception("[%s] strategy stage 1 failed", reason)

    # ---- 8c. Strategy stage 2 — EMA-cross isolation ----------------------
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
        isolation_summary = strategy_isolation_service.run(reason=reason)
    except Exception:
        logger.exception("[%s] strategy stage 2 failed", reason)

    # ---- 8d. Strategy stage 3 tracker (R2 activation watcher) ------------
    # Long-lived daemon thread. It reads stage-2 folders once per day
    # and activates stage-3 folders when a tracked index LTP reaches
    # that index's R2 level inside the configured 09:15–09:20 window.
    # Starting it here (before the live feed) means the tracker is
    # already listening by the time the first 09:15 tick arrives.
    tracker_started = False
    try:
        tracker_started = bool(strategy_live_tracker_service.start())
        logger.info(
            "[%s] stage_3 tracker started=%s status=%s",
            reason, tracker_started,
            strategy_live_tracker_service.status(),
        )
    except Exception:
        logger.exception("[%s] stage_3 tracker start failed", reason)

    # ---- 9. Start / restart live feed ------------------------------------
    feed_ok = False
    try:
        feed_running = bool(live_feed_service.status().get("running"))
        if feed_running:
            feed_ok = live_feed_service.restart()
        else:
            feed_ok = live_feed_service.start()
    except Exception:
        logger.exception("[%s] live feed start/restart failed", reason)

    # ---- 10. Start scheduler (idempotent) --------------------------------
    scheduler_ok = True
    try:
        scheduler_service.start()
    except Exception:
        logger.exception("[%s] scheduler start failed", reason)
        scheduler_ok = False

    # ---- 11. Start Telegram token bot (idempotent) -----------------------
    telegram_ok = True
    try:
        telegram_token_bot.start()
    except Exception:
        logger.exception("[%s] telegram bot start failed", reason)
        telegram_ok = False

    result: dict[str, Any] = {
        "reason": reason,
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
        "feed_restarted": feed_ok,
        "scheduler_started": scheduler_ok,
        "telegram_started": telegram_ok,
    }

    # ---- 12. Telegram completion notification ----------------------------
    status = "SUCCESS" if backup.get("status") == "ok" else "WARNING"

    strategy_ok = sum(
        1 for i in strategy_summary.get("indexes", [])
        if i.get("status") == "ok"
    )
    strategy_total = len(strategy_summary.get("indexes", []))

    message = (
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
        f"pairs: {isolation_summary.get('pairs_isolated')}  "
        f"skipped: {isolation_summary.get('skipped')}  "
        f"errors: {isolation_summary.get('errors')}\n"
        f"Stage 3 tracker: {tracker_started}\n"
        f"Live feed running: {feed_ok}\n"
        f"Scheduler: {scheduler_ok}  Telegram bot: {telegram_ok}"
    )
    telegram_service.send_message(
        f"{label} Refresh Completed" if status == "SUCCESS"
        else f"{label} Refresh Finished",
        message,
        level=status,
    )
    return result


# --------------------------------------------------------------------------- #
# HTTP routes                                                                  #
# --------------------------------------------------------------------------- #
@router.post("/admin/hard-refresh")
async def hard_refresh() -> dict[str, Any]:
    """Unified refresh triggered from the API."""
    return await asyncio.to_thread(
        _run_full_refresh_sync, "manual", None
    )


@router.get("/admin/backup-status")
def backup_status() -> dict[str, Any]:
    return backup_data_service.last_status()


@router.get("/admin/cleanup-status")
def cleanup_status() -> dict[str, Any]:
    """Return the summary of the most recent folder-cleanup pass."""
    return cleanup_service.last_status()


@router.post("/admin/cleanup")
async def run_cleanup() -> dict[str, Any]:
    """Run the folder cleanup standalone (no backup / no feed restart)."""
    return await asyncio.to_thread(cleanup_service.run, "manual")