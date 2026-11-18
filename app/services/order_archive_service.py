"""Write each order lifecycle event to local JSON and MongoDB independently."""
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
    result: dict[str, Any] = {"local_persisted": False, "mongo_persisted": False}
    local_path: Path | None = None

    # Attempt both stores independently: a local filesystem error must not skip MongoDB.
    try:
        local_path = _write_local(payload)
        result["local_persisted"] = True
        result["local_file"] = str(local_path)
    except Exception as exc:
        logger.exception("Failed to save order event to local JSON journal")
        result["local_error"] = f"{type(exc).__name__}: {exc}"

    try:
        append_order_event(payload)
        result["mongo_persisted"] = True
    except Exception as exc:
        logger.exception("MongoDB persistence failed for order event")
        result["mongo_error"] = f"{type(exc).__name__}: {exc}"

    # Rewrite the local event with per-store persistence outcomes when possible.
    if local_path is not None:
        payload.update(result)
        try:
            _write_local(payload)
        except Exception:
            logger.exception("Could not update local order event with persistence outcomes")
    return result
