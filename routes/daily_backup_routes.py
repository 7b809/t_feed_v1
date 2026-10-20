"""Admin routes for the daily MEGA backup + progress state.

Endpoints
---------
    GET  /admin/daily-backup-status
        Last-run summary (in-memory) + the last N date documents
        (default 7, configurable via DAILY_BACKUP_STATE_DAYS).

    GET  /admin/daily-backup-history?days=7
        Only the date-keyed history (no last-run summary). Useful for
        lightweight polling.

    POST /admin/daily-backup-run
        Trigger the daily backup now (same routine as the 16:00 job).

Design
------
* The MEGA archive itself never passes through these routes — only
  the progress state stored in MongoDB under
  ``DAILY_BACKUP_STATE_COLLECTION`` (one doc per date, keyed by
  ``_id = "YYYY-MM-DD"``).
* MongoDB documents are normalised to plain JSON before returning so
  BSON ``_id`` / ``datetime`` values never break a FastAPI response.
* No credentials, no tokens, no file bytes, no archive paths — just
  the metadata that was already written to MongoDB by
  ``services.daily_backup_service``.
* Every endpoint returns HTTP 200 with a JSON body; the ``status``
  field inside each document carries the outcome
  (``uploaded`` / ``partial`` / ``failed`` / ``no_data`` / ...). The
  only HTTP error is a 400 for a malformed ``days`` query (enforced by
  FastAPI's ``Query(ge=1, le=30)``).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query

from core import config
from services.daily_backup_service import daily_backup_service

router = APIRouter(prefix="/admin", tags=["daily-backup"])

# --------------------------------------------------------------------------- #
# Serialisation helpers                                                       #
# --------------------------------------------------------------------------- #
def _safe_value(value: Any) -> Any:
    """Return a JSON-serialisable copy of ``value``.

    Handles ``datetime``, ``ObjectId``, nested lists, and nested dicts.
    Everything else is returned as-is so FastAPI's own JSON encoder
    can deal with it.
    """
    if isinstance(value, datetime):
        return value.isoformat()

    # ObjectId from bson — happens only if a caller added one explicitly.
    # ``pymongo`` is already a dependency, so the import is safe.
    try:
        from bson import ObjectId  # type: ignore
        if isinstance(value, ObjectId):
            return str(value)
    except Exception:
        pass

    if isinstance(value, dict):
        return {str(k): _safe_value(v) for k, v in value.items()}

    if isinstance(value, (list, tuple)):
        return [_safe_value(v) for v in value]

    return value

def _safe_status(status: dict[str, Any] | None) -> dict[str, Any]:
    """Normalise an in-memory summary / state doc for JSON output."""
    if not isinstance(status, dict):
        return {}
    return _safe_value(status)

# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #
@router.get("/daily-backup-status")
def daily_backup_status(
    days: int = Query(
        default=config.DAILY_BACKUP_STATE_DAYS,
        ge=1,
        le=30,
        description="Number of past dates to include (newest first).",
    ),
) -> dict[str, Any]:
    """Return the last run summary + the last ``days`` date documents.

    Response shape::

        {
          "config": {
            "enabled":            true,
            "mega_enabled":       true,
            "mega_email_set":     true,      # boolean, never the value
            "mega_password_set":  true,      # boolean, never the value
            "root_folder":        "market_engine_backup",
            "folders":            ["data"],
            "state_db":           "UPSTOX_APP_V2",
            "state_collection":   "daily_backup_state",
            "schedule":           "16:00",
            "days_returned":      7
          },
          "last_run": { ...last summary dict... },
          "history": [
            {
              "_id":             "2026-10-20",
              "date":            "2026-10-20",
              "status":          "uploaded" | "partial" | "failed" | "no_data",
              "files": [
                {
                  "folder":       "data",
                  "filename":     "data_2026-10-20.zip",
                  "size_bytes":   8806042,
                  "size_human":   "8.4 MB",
                  "mega_link":    "https://mega.nz/file/...",
                  "uploaded":     true,
                  "error":        null,
                  "remote_path":  "market_engine_backup/data/data_2026-10-20.zip",
                  "file_count":   3412
                }
              ],
              "total_size_bytes": 8806042,
              "total_size_human": "8.4 MB",
              "updated_at":       "2026-10-20T10:30:14.933000+00:00",
              "updated_at_ist":   "2026-10-20 16:00:14 IST",
              "error":            null
            },
            ...
          ],
          "generated_at": "2026-10-20T10:30:20.000000+00:00"
        }
    """
    return {
        "config": {
            "enabled": config.DAILY_BACKUP_ENABLED,
            "mega_enabled": config.MEGA_ENABLED,
            # Only booleans — never echo the credentials themselves.
            "mega_email_set": bool((config.MEGA_EMAIL or "").strip()),
            "mega_password_set": bool((config.MEGA_PASSWORD or "").strip()),
            "root_folder": config.MEGA_BACKUP_ROOT,
            "folders": list(config.DAILY_BACKUP_FOLDERS),
            "state_db": config.DAILY_BACKUP_MONGO_DB,
            "state_collection": config.DAILY_BACKUP_STATE_COLLECTION,
            "schedule": (
                f"{config.DAILY_BACKUP_HOUR:02d}:"
                f"{config.DAILY_BACKUP_MINUTE:02d}"
                f" {config.MARKET_TIMEZONE}"
            ),
            "days_returned": days,
        },
        "last_run": _safe_status(daily_backup_service.last_status()),
        "history": [
            _safe_status(doc)
            for doc in daily_backup_service.get_recent_history(days)
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

@router.get("/daily-backup-history")
def daily_backup_history(
    days: int = Query(
        default=config.DAILY_BACKUP_STATE_DAYS,
        ge=1,
        le=30,
        description="Number of past dates to return (newest first).",
    ),
) -> dict[str, Any]:
    """Return only the date-keyed history (no last-run summary).

    Use this for lightweight polling when the caller only cares about
    the day-by-day upload status, not the last in-memory run summary.
    """
    history = [
        _safe_status(doc)
        for doc in daily_backup_service.get_recent_history(days)
    ]
    return {
        "count": len(history),
        "history": history,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

@router.post("/daily-backup-run")
async def daily_backup_run_now() -> dict[str, Any]:
    """Trigger the daily backup now (same routine as the 16:00 job).

    Runs in a worker thread so the event loop stays responsive while
    the MEGA upload (which can take a minute or two) is in flight.

    Returns the same summary dict that the scheduled job would have
    produced, plus ``trigger="manual"``.
    """
    summary = await asyncio.to_thread(
        daily_backup_service.run, "manual"
    )
    result = _safe_status(summary)
    result["trigger"] = "manual"
    return result