"""Write order lifecycle events to a local JSON journal and MongoDB."""
from __future__ import annotations
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from app.core.config import settings
from app.db.mongo import append_order_event, now_ist

logger = logging.getLogger(__name__)

def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if hasattr(value, "to_dict"):
        try:
            return _safe(value.to_dict())
        except Exception:
            pass
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return _safe(vars(value))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)

def _json_default(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)

def _write_local(event: dict) -> Path:
    date_key = now_ist().strftime("%Y-%m-%d")
    folder = settings.DATA_DIR / date_key
    folder.mkdir(parents=True, exist_ok=True)
    status = re.sub(r"[^a-z0-9_-]+", "_", str(event.get("status", "event")).lower())
    event_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(event.get("event_id", "unknown")))
    path = folder / f"{status}_{event_id}.json"
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(_safe(event), indent=2, default=_json_default), encoding="utf-8")
    os.replace(temp_path, path)
    return path

def archive_event(event: dict) -> dict[str, Any]:
    payload = _safe(dict(event))
    local_now = now_ist()
    payload.setdefault("created_time", local_now.isoformat())
    payload["last_modified_time"] = local_now.isoformat()

    # Write locally first: this journal survives a MongoDB outage.
    local_path = _write_local(payload)
    result: dict[str, Any] = {
        "local_file": str(local_path),
        "mongo_persisted": False,
    }
    try:
        append_order_event(payload)
        result["mongo_persisted"] = True
    except Exception as exc:
        logger.exception("Order event is saved locally but MongoDB persistence failed")
        result["mongo_error"] = f"{type(exc).__name__}: {exc}"
        payload["mongo_persisted"] = False
        payload["mongo_error"] = result["mongo_error"]
        _write_local(payload)
    return result
