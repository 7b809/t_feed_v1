# app/services/live_feed_state_store.py
"""Atomic raw live-market message persistence, partitioned by instrument."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Settings

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def _safe(value: str) -> str:
    return _SAFE_RE.sub("_", value)


def _file_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


class LiveFeedStateStore:
    """Append raw provider payloads to daily JSONL and maintain latest.json."""

    def __init__(self, config: Settings) -> None:
        self.base_dir = Path(config.live_feed_state_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _instrument_dir(self, instrument_key: str) -> Path:
        return self.base_dir / _safe(instrument_key)

    @staticmethod
    def _received_at() -> datetime:
        return datetime.now(IST)

    def save_message(
        self,
        instrument_key: str,
        data: dict[str, Any],
        mode: str,
        isolation_ids: list[str],
        roles: list[str],
    ) -> None:
        received = self._received_at()
        record = {
            "instrument_key": instrument_key,
            "isolation_ids": sorted(set(isolation_ids)),
            "roles": sorted(set(roles)),
            "mode": mode,
            "received_at": received.isoformat(),
            "data": data,
        }
        folder = self._instrument_dir(instrument_key)
        folder.mkdir(parents=True, exist_ok=True)
        daily_file = folder / f"{received:%Y-%m-%d}.jsonl"
        latest_file = folder / "latest.json"
        lock = _file_lock(daily_file)
        try:
            line = json.dumps(
                record, ensure_ascii=False, default=str, separators=(",", ":")
            )
            with lock:
                with daily_file.open("a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
                fd, temp_name = tempfile.mkstemp(
                    prefix="latest.", suffix=".tmp", dir=str(folder)
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        json.dump(
                            record, stream, indent=2, ensure_ascii=False, default=str
                        )
                    Path(temp_name).replace(latest_file)
                except Exception:
                    Path(temp_name).unlink(missing_ok=True)
                    raise
        except Exception:
            logger.exception(
                "Live-feed persistence failed key=%s directory=%s",
                instrument_key, folder,
            )

    def latest(self, instrument_key: str) -> dict[str, Any] | None:
        path = self._instrument_dir(instrument_key) / "latest.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
        except FileNotFoundError:
            return None
        except Exception:
            logger.exception("Failed to read latest live-feed record %s", path)
            return None

    def status(self) -> dict[str, Any]:
        instruments: dict[str, Any] = {}
        if not self.base_dir.exists():
            return {"directory": str(self.base_dir), "instrument_count": 0, "instruments": instruments}
        for folder in sorted(p for p in self.base_dir.iterdir() if p.is_dir()):
            latest = self.latest_from_folder(folder)
            if latest:
                key = latest.get("instrument_key", folder.name)
                instruments[key] = {
                    "last_received_at": latest.get("received_at"),
                    "mode": latest.get("mode"),
                    "roles": latest.get("roles", []),
                    "isolation_ids": latest.get("isolation_ids", []),
                }
        return {
            "directory": str(self.base_dir),
            "instrument_count": len(instruments),
            "instruments": instruments,
        }

    @staticmethod
    def latest_from_folder(folder: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads((folder / "latest.json").read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
