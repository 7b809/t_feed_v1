"""Atomic local persistence for simulated entries; never submits broker orders."""
from __future__ import annotations
import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any
from ..config import Settings

logger = logging.getLogger(__name__)
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")

class DummyOrderStore:
    def __init__(self, config: Settings) -> None:
        self.base_dir = Path(config.dummy_order_state_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    @staticmethod
    def _safe(value: str) -> str:
        return _SAFE.sub("_", value)

    def _path(self, isolation_id: str) -> Path:
        return self.base_dir / f"{self._safe(isolation_id)}.json"

    def _lock_for(self, isolation_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(isolation_id, threading.Lock())

    def load(self, isolation_id: str) -> dict[str, Any] | None:
        try:
            value = json.loads(self._path(isolation_id).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except FileNotFoundError:
            return None
        except Exception:
            logger.exception("Could not read dummy order state for %s", isolation_id)
            return None

    def save(self, isolation_id: str, state: dict[str, Any]) -> None:
        path = self._path(isolation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_for(isolation_id):
            fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(state, stream, indent=2, ensure_ascii=False, default=str)
                Path(tmp).replace(path)
            except Exception:
                Path(tmp).unlink(missing_ok=True)
                raise

    def delete(self, isolation_id: str) -> None:
        self._path(isolation_id).unlink(missing_ok=True)

    def list_all(self) -> list[dict[str, Any]]:
        result = []
        for path in sorted(self.base_dir.glob("*.json")):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(item, dict):
                    result.append(item)
            except Exception:
                logger.exception("Could not read dummy order file %s", path)
        return sorted(result, key=lambda item: item.get("updated_at") or "", reverse=True)

    def status(self) -> dict[str, Any]:
        states = self.list_all()
        return {
            "directory": str(self.base_dir),
            "isolation_count": len(states),
            "order_count": sum(sum(bool(v) for v in (s.get("orders") or {}).values()) for s in states),
        }
