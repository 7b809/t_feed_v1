# File: routes/readiness_routes.py
"""Readiness + operator-runbook routes.

    GET  /admin/readiness               -> last readiness snapshot (or fresh)
    POST /admin/readiness               -> force a fresh readiness check
    GET  /admin/readiness/history       -> 7-day health-log history
    GET  /admin/ops/runbook             -> operator cheat sheet as JSON
    POST /admin/ops/warmup              -> backup + registry only
    POST /admin/ops/smoke-test          -> 5-probe smoke test (no side effects)
    POST /admin/ops/resubscribe-feed    -> force live-feed reconnect
    POST /admin/ops/revalidate-token    -> force token re-check
    POST /admin/ops/first-candle        -> manual first-candle trigger
    GET  /admin/ops/first-candle-status -> last first-candle run summary

Strategy visibility (read-only)
-------------------------------
    GET  /admin/ops/strategy-state          -> today's strategy gate
    GET  /admin/ops/level-touches           -> today's OR-level touches
    GET  /admin/ops/strategy1-selection     -> today's S1 selection record
    GET  /admin/ops/strategy2-isolation     -> today's S2 isolation records
    POST /admin/ops/strategy2-rescan        -> re-run R3/S3 pairing on
                                               existing touches

These endpoints are read-only (except rescan) and expose only the
metadata that strategy_state_service / level_touch_tracker_service /
strategy1_service / strategy2_service already persist under
``meta_data/``. No credentials or tokens appear in any response body.

All routes return HTTP 200 with a JSON body carrying ``status`` so
callers can drive automation off the response.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from core import config
from core.logger import get_logger
from services.readiness_service import (
    readiness_service,
    remediation_for,
)

logger = get_logger(__file__)

router = APIRouter(prefix="/admin", tags=["readiness"])


# --------------------------------------------------------------------------- #
# Readiness                                                                   #
# --------------------------------------------------------------------------- #
@router.get("/readiness")
def get_readiness() -> dict[str, Any]:
    """Return the most recent readiness snapshot (compute one if none)."""
    snap = readiness_service.last_snapshot()
    if snap is None:
        snap = readiness_service.check()
    return snap


@router.post("/readiness")
async def post_readiness() -> dict[str, Any]:
    """Force a fresh readiness check (no side effects)."""
    return await asyncio.to_thread(readiness_service.check)


@router.get("/readiness/history")
def get_readiness_history(limit: int = 50) -> dict[str, Any]:
    """Return the tail of the 7-day health log (per-day JSON files).

    The health log lives under ``meta_data/health/`` — written by
    ``services.readiness_service._append_health_log()``. This route
    previously read from ``data/health/`` (a stale path) and always
    returned an empty list; fixed to match the writer.
    """
    try:
        from pathlib import Path
        import json
        health_dir = Path(config.PROJECT_ROOT) / "meta_data" / "health"
        if not health_dir.exists():
            return {"count": 0, "entries": []}
        files = sorted(health_dir.glob("readiness_*.json"), reverse=True)
        entries: list[dict[str, Any]] = []
        for path in files[: 7]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    entries.extend(data)
            except Exception:
                continue
        entries = entries[-int(limit) :]
        return {
            "count": len(entries),
            "entries": entries,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        logger.exception("[readiness] history read failed")
        return {"count": 0, "entries": [], "error": "history_unavailable"}


# --------------------------------------------------------------------------- #
# Operator runbook                                                            #
# --------------------------------------------------------------------------- #
@router.get("/ops/runbook")
def get_runbook() -> dict[str, Any]:
    """Return the operator's incident-response cheat sheet as JSON."""
    return {
        "pre_open_checklist": [
            {"time": "08:30", "action": "auto: daily refresh"},
            {"time": "08:45", "action": "auto: readiness digest"},
            {"time": "09:05", "action": "auto: pre-flight recheck"},
            {"time": "09:10", "action": "manual: verify GREEN in Telegram"},
            {"time": "09:15", "action": "cash market opens"},
        ],
        "if_red": {
            "token_valid":         "send /save_token in Telegram",
            "token_age_minutes":   "run /token_check in Telegram",
            "registry_count":      "POST /admin/ops/warmup",
            "data_dir":            "POST /admin/ops/warmup",
            "mongo_reachable":     "check Atlas status page",
            "live_feed_running":   "POST /admin/ops/resubscribe-feed",
            "scheduler_jobs_count":"restart the application",
            "clock_drift_seconds": "sudo ntpdate -s pool.ntp.org",
            "disk_free_gb":        "clear data/ or logs/ old files",
            "order_toggle":        "POST /admin/orders/runtime-enable",
        },
        "remediation_hints": {
            k: remediation_for(k)
            for k in (
                "token_valid",
                "registry_count",
                "live_feed_running",
                "mongo_reachable",
                "disk_free_gb",
                "clock_drift_seconds",
            )
        },
        "escalation": (
            "If still RED at 09:10 → check logs and call on-call."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------- #
# Manual remediation                                                          #
# --------------------------------------------------------------------------- #
@router.post("/ops/warmup")
async def warmup() -> dict[str, Any]:
    """Re-extract backup + reload registry (no cleanup, no strategy).

    The EMA backfill seed call was removed when the strategy layer was
    stripped from the codebase. The endpoint now only refreshes the
    two infrastructure inputs that later stages depend on.
    """
    def _run() -> dict[str, Any]:
        from services.backup_data_service import backup_data_service
        from services.instrument_registry import instrument_registry

        backup = backup_data_service.refresh()
        registry = instrument_registry.load()

        return {
            "status": "ok",
            "backup": backup,
            "registry": registry,
            "ran_at": datetime.now(timezone.utc).isoformat(),
        }

    return await asyncio.to_thread(_run)


@router.post("/ops/smoke-test")
async def smoke_test() -> dict[str, Any]:
    """Run the 5-probe smoke test without side effects."""
    return await asyncio.to_thread(readiness_service.smoke_test)


@router.post("/ops/resubscribe-feed")
async def resubscribe_feed() -> dict[str, Any]:
    """Force live-feed reconnect (does not run a full refresh)."""
    def _run() -> dict[str, Any]:
        from services.live_feed_service import live_feed_service
        ok = live_feed_service.restart()
        return {
            "status": "ok" if ok else "failed",
            "restarted": ok,
            "feed_status": live_feed_service.status(),
            "ran_at": datetime.now(timezone.utc).isoformat(),
        }

    return await asyncio.to_thread(_run)


@router.post("/ops/revalidate-token")
async def revalidate_token() -> dict[str, Any]:
    """Force token re-check + Telegram alert."""
    def _run() -> dict[str, Any]:
        from token_tasks.token_monitor import check_upstox_token_validity
        result = check_upstox_token_validity(send_success_message=True)
        return {
            "status": result.get("status"),
            "valid": result.get("valid"),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    return await asyncio.to_thread(_run)


@router.post("/ops/first-candle")
async def trigger_first_candle() -> dict[str, Any]:
    """Manually trigger the first-candle OR + EMA-cross calculation."""
    def _run() -> dict[str, Any]:
        from services.first_candle_service import first_candle_service
        return first_candle_service.run(reason="manual")
    return await asyncio.to_thread(_run)


@router.get("/ops/first-candle-status")
def first_candle_status() -> dict[str, Any]:
    """Return the last first-candle run summary."""
    from services.first_candle_service import first_candle_service
    return first_candle_service.last_status()


# --------------------------------------------------------------------------- #
# Strategy visibility (read-only, plus a manual rescan)                       #
# --------------------------------------------------------------------------- #
def _market_day_iso() -> str:
    """Return today's date string in the market timezone."""
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(config.MARKET_TIMEZONE)
    except Exception:
        tz = ZoneInfo("Asia/Kolkata")
    return datetime.now(tz).date().isoformat()


@router.get("/ops/strategy-state")
def get_strategy_state() -> dict[str, Any]:
    """Return the day's strategy gate state.

    Fields include:
        day, strategy1_eligible, strategy1_active, strategy1_fired,
        strategy1_fired_at, strategy1_target_key, strategy1_source,
        strategy2_active, strategy2_activated_at,
        nifty_avg, sensex_avg, cross_true_count, computed_at
    """
    from services.strategy_state_service import strategy_state_service
    return strategy_state_service.get_state()


@router.get("/ops/level-touches")
def get_level_touches() -> dict[str, Any]:
    """Return today's recorded OR-level touches per instrument.

    Shape:
        {
          "day": "YYYY-MM-DD",
          "instrument_count": int,        # instruments with cached OR levels
          "touched_instrument_count": int,
          "touches": {
             "<instrument_key>": {
                "R1": {"ltp": float, "ts": "...", "level_value": float},
                "R2": {...}, "S2": {...}, "R3": {...}, "S3": {...}, ...
             },
             ...
          }
        }
    """
    from services.level_touch_tracker_service import (
        level_touch_tracker_service,
    )
    return level_touch_tracker_service.get_day_snapshot()


@router.get("/ops/strategy1-selection")
def get_strategy1_selection() -> dict[str, Any]:
    """Return today's Strategy 1 selection record, if any.

    The record is written by ``services.strategy1_service._persist``
    after an order attempt (success or failure). A missing file means
    Strategy 1 has not fired today.
    """
    import json
    from pathlib import Path

    day = _market_day_iso()
    path = Path(config.PROJECT_ROOT) / "meta_data" / f"strategy1_selection_{day}.json"
    if not path.exists():
        return {"day": day, "selection": None, "found": False}
    try:
        return {
            "day": day,
            "selection": json.loads(path.read_text(encoding="utf-8")),
            "found": True,
        }
    except Exception:
        logger.exception("[readiness] strategy1 selection read failed")
        return {
            "day": day, "selection": None, "found": False,
            "error": "unreadable",
        }


@router.get("/ops/strategy2-isolation")
def get_strategy2_isolation() -> dict[str, Any]:
    """Return today's Strategy 2 isolation records, if any.

    Each record captures the paired R3 and S3 hits (instrument keys,
    strike, type, timestamps, LTPs) that triggered an
    "ISOLATION SELECTED" Telegram message.
    """
    import json
    from pathlib import Path

    day = _market_day_iso()
    path = Path(config.PROJECT_ROOT) / "meta_data" / f"strategy2_isolation_{day}.json"
    if not path.exists():
        return {"day": day, "records": [], "found": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload.get("records") if isinstance(payload, dict) else []
        return {
            "day": day,
            "records": records or [],
            "found": True,
        }
    except Exception:
        logger.exception("[readiness] strategy2 isolation read failed")
        return {
            "day": day, "records": [], "found": False,
            "error": "unreadable",
        }


@router.post("/ops/strategy2-rescan")
async def post_strategy2_rescan() -> dict[str, Any]:
    """Manually re-scan existing touches for Strategy 2 pairing.

    Useful when Strategy 2 activated later in the day (e.g. after the
    Strategy 1 11:00 cutoff) and the operator wants to force a scan
    of the touches that were recorded earlier in the session without
    waiting for the next live tick.
    """
    def _run() -> dict[str, Any]:
        from services.strategy2_service import strategy2_service
        strategy2_service.rescan_existing_touches()
        return {
            "status": "ok",
            "ran_at": datetime.now(timezone.utc).isoformat(),
        }
    return await asyncio.to_thread(_run)