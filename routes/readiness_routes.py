"""Readiness + operator-runbook routes.

    GET  /admin/readiness               -> last readiness snapshot (or fresh)
    POST /admin/readiness               -> force a fresh readiness check
    GET  /admin/readiness/history       -> 7-day health-log history
    GET  /admin/ops/runbook             -> operator cheat sheet as JSON
    POST /admin/ops/warmup              -> backfill + registry only
    POST /admin/ops/smoke-test          -> 5-probe smoke test (no side effects)
    POST /admin/ops/resubscribe-feed    -> force live-feed reconnect
    POST /admin/ops/revalidate-token    -> force token re-check

All routes return HTTP 200 with a JSON body carrying ``status`` so
callers can drive automation off the response. No credentials or
tokens appear in any response body.
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
    """Return the tail of the 7-day health log (per-day JSON files)."""
    try:
        from pathlib import Path
        import json
        health_dir = Path(config.PROJECT_ROOT) / "data" / "health"
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
            {"time": "09:16", "action": "auto: OR cron fires"},
            {"time": "09:17", "action": "auto: OR + Stage 1 watchdog"},
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
    """Re-run the backfill + registry reload only (no cleanup, no strategy)."""
    def _run() -> dict[str, Any]:
        from services.backup_data_service import backup_data_service
        from services.instrument_registry import instrument_registry
        from services.live_backfill_service import live_backfill_service

        backup = backup_data_service.refresh()
        registry = instrument_registry.load()
        backfill = live_backfill_service.seed_all()

        return {
            "status": "ok",
            "backup": backup,
            "registry": registry,
            "backfill": backfill,
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