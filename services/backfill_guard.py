# File: services/backfill_guard.py
"""Small helper to record / check whether a scheduled job ran today.

Used to decide whether a startup backfill should fire. Each job's
"last run" is stored as a tiny JSON file under ``meta_data/``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

_TZ = ZoneInfo("Asia/Kolkata")
_GUARD_DIR = Path(config.PROJECT_ROOT) / "meta_data"


def _guard_path(job_id: str) -> Path:
    _GUARD_DIR.mkdir(parents=True, exist_ok=True)
    return _GUARD_DIR / f"last_{job_id}.json"


def ran_today(job_id: str) -> bool:
    """Return True if ``job_id`` recorded a run for today (IST)."""
    path = _guard_path(job_id)
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    today = datetime.now(_TZ).date().isoformat()
    return str(payload.get("date") or "") == today


def mark_ran(job_id: str, **extra: Any) -> None:
    """Record that ``job_id`` ran today."""
    payload = {
        "date": datetime.now(_TZ).date().isoformat(),
        "ran_at": datetime.now(_TZ).isoformat(),
        **extra,
    }
    try:
        _guard_path(job_id).write_text(
            json.dumps(payload, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("backfill_guard: failed writing %s", job_id)