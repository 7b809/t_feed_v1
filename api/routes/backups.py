"""MEGA daily data-backup status and manual trigger endpoints."""
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Query
from core.config import (
    DAILY_DATA_BACKUP_COLLECTION, DAILY_DATA_BACKUP_ENABLED,
    DAILY_DATA_BACKUP_HOUR, DAILY_DATA_BACKUP_MINUTE, DAILY_DATA_BACKUP_DAYS,
    DAILY_DATA_BACKUP_DIR, DATA_DIR, MEGA_BACKUP_ROOT, MEGA_EMAIL, MEGA_PASSWORD, MEGA_ENABLED,
)
from core.logger import get_logger
from core.mongo_client import get_mongo_db
from services.mega_backup_scheduler import get_backup_service, get_mega_backup_scheduler

logger = get_logger(__file__)
router = APIRouter(prefix="/api/v1/backups", tags=["backups"])


def _json_safe(document: dict | None):
    if not document:
        return None
    result = dict(document)
    result.pop("_id", None)
    for key, value in list(result.items()):
        if hasattr(value, "isoformat"):
            result[key] = value.isoformat()
    return result


@router.get("/mega/status")
def mega_backup_status():
    """Return backup configuration, next run and latest backup result."""
    scheduler = get_mega_backup_scheduler()
    next_run = None
    if scheduler is not None and scheduler.running:
        job = scheduler.get_job("daily_sandbox_mega_backup")
        if job and job.next_run_time:
            next_run = job.next_run_time.isoformat()
    latest = None
    try:
        latest = _json_safe(
            get_mongo_db()[DAILY_DATA_BACKUP_COLLECTION].find_one({}, sort=[("_id", -1)])
        )
    except Exception:
        logger.exception("Could not read latest MEGA backup state")
    service = get_backup_service()
    return {
        "enabled": bool(DAILY_DATA_BACKUP_ENABLED and MEGA_ENABLED),
        "credentials_configured": bool(MEGA_EMAIL and MEGA_PASSWORD),
        "schedule": {
            "days": DAILY_DATA_BACKUP_DAYS,
            "time": f"{DAILY_DATA_BACKUP_HOUR:02d}:{DAILY_DATA_BACKUP_MINUTE:02d}",
            "timezone": "Asia/Kolkata",
            "next_run": next_run,
        },
        "source": str(DATA_DIR),
        "staging": str(DAILY_DATA_BACKUP_DIR),
        "mega_folder": MEGA_BACKUP_ROOT,
        "scheduler_running": bool(scheduler and scheduler.running),
        "service_ready": service is not None,
        "latest": latest,
    }


@router.get("/mega/history")
def mega_backup_history(limit: int = Query(default=30, ge=1, le=365)):
    """Return the most recent date-keyed MEGA backup status records."""
    try:
        documents = get_mongo_db()[DAILY_DATA_BACKUP_COLLECTION].find({}).sort("_id", -1).limit(limit)
        results = [_json_safe(row) for row in documents]
        return {"count": len(results), "backups": results}
    except Exception as exc:
        logger.exception("Could not read MEGA backup history")
        raise HTTPException(status_code=503, detail=f"Backup history unavailable: {type(exc).__name__}")


@router.post("/mega/run")
def run_mega_backup_now():
    """Trigger a manual full data/ archive and upload."""
    if not DAILY_DATA_BACKUP_ENABLED or not MEGA_ENABLED:
        raise HTTPException(status_code=409, detail="MEGA daily backup is disabled.")
    service = get_backup_service()
    if service is None:
        raise HTTPException(status_code=503, detail="MEGA backup service is not initialised. Check credentials and service logs.")
    result = service.run_daily_backup(reason="manual")
    if result.get("status") != "uploaded":
        raise HTTPException(status_code=502, detail=result)
    return result
