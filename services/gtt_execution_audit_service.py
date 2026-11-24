"""Append-only daily execution prompt/request/response audit."""
from __future__ import annotations
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from core.config import LIVE_GTT_AUDIT_COLLECTION, LIVE_GTT_AUDIT_DIR, LIVE_GTT_AUDIT_MAX_EVENTS
from core.logger import get_logger
from core.mongo_client import get_mongo_db

logger = get_logger(__file__)
IST = ZoneInfo("Asia/Kolkata")
_LOCK = threading.RLock()


def _safe(value):
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()
                if not any(secret in str(k).lower() for secret in ("access_token", "refresh_token", "auth_token", "authorization", "api_key", "password", "secret", "bearer"))}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, str) and value.lower().startswith("bearer "):
        return "[REDACTED]"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def build_action_prompt(action: str, context: dict) -> str:
    """A JSON-only deterministic action contract; not an LLM trading decision."""
    return json.dumps({
        "task": "Record and validate one Upstox GTT execution action",
        "required_output": "Exactly one JSON object; no Markdown or prose",
        "action": action,
        "context": _safe(context),
        "constraints": [
            "Do not invent instrument keys, quantities, prices or broker identifiers",
            "A GTT submission is not proof of an exchange fill",
            "Never blindly retry an ambiguous broker write",
            "Preserve strategy-calculated values exactly",
            "Report unknown or incomplete broker states explicitly",
        ],
        "response_schema": {
            "action": "string", "accepted": "boolean",
            "broker_identifiers": "object", "status": "string", "errors": "array",
        },
    }, ensure_ascii=False, indent=2, default=str)


class GTTExecutionAuditService:
    def __init__(self):
        self.root = Path(LIVE_GTT_AUDIT_DIR).expanduser()

    def append(self, action, isolation_id, prompt, request, response, status, metadata=None):
        now = datetime.now(IST)
        day = now.date().isoformat()
        event = {
            "event_id": uuid.uuid4().hex,
            "timestamp_ist": now.isoformat(),
            "trade_date_ist": day,
            "action": action,
            "isolation_id": isolation_id,
            "prompt_origin": "algocli_template" if "Use the " in str(prompt) else "deterministic_execution_contract",
            "prompt": prompt,
            "request": _safe(request or {}),
            "response_kind": "broker_or_internal_json",
            "response": _safe(response or {}),
            "status": status,
            "metadata": _safe(metadata or {}),
        }
        self._append_local(day, event)
        try:
            get_mongo_db()[LIVE_GTT_AUDIT_COLLECTION].update_one(
                {"_id": day},
                {
                    "$setOnInsert": {"trade_date_ist": day, "created_at_ist": now.isoformat(), "schema_version": 1},
                    "$push": {"events": {"$each": [event], "$slice": -max(1, int(LIVE_GTT_AUDIT_MAX_EVENTS))}},
                    "$set": {"updated_at_ist": now.isoformat()},
                },
                upsert=True,
            )
            event["mongo_persisted"] = True
        except Exception:
            event["mongo_persisted"] = False
            logger.exception("GTT audit Mongo append failed; local copy retained | day=%s action=%s", day, action)
        return event

    def _append_local(self, day, event):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{day}.json"
        with _LOCK:
            try:
                document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except (OSError, json.JSONDecodeError):
                document = {}
            document.setdefault("schema_version", 1)
            document.setdefault("trade_date_ist", day)
            events = document.setdefault("events", [])
            events.append(event)
            document["updated_at_ist"] = datetime.now(IST).isoformat()
            fd, temp = tempfile.mkstemp(prefix="gtt-audit-", suffix=".tmp", dir=str(self.root))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(document, handle, ensure_ascii=False, indent=2, default=str)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
