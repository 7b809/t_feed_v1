# app/services/supertrend_state_store.py
"""Atomic today-only Supertrend persistence for isolated and opposite legs.

Layout:
  {SUPERTREND_STATE_DIR}/{leg}__{strike}_{option}_{instrument_key_safe}/
      today_snapshot.json
      today_live_supertrend.json

The opening snapshot is frozen. Confirmed signal frames advance the live
file; heartbeat frames update last_heartbeat without changing signal history.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ..config import Settings

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
SNAPSHOT_FILENAME = "today_snapshot.json"
LIVE_FILENAME = "today_live_supertrend.json"


def _safe(value: Any) -> str:
    return _SAFE_RE.sub("_", str(value))


def _strike_option(payload: dict[str, Any]) -> str:
    meta = payload.get("meta")
    if isinstance(meta, dict):
        strike, opt = meta.get("strike_price"), meta.get("strike_type")
        if strike is not None and opt:
            try:
                number = float(strike)
                strike_text = str(int(number)) if number.is_integer() else str(number).replace(".", "_")
                return f"{strike_text}_{str(opt).lower()}"
            except (TypeError, ValueError):
                pass
    symbol = payload.get("trading_symbol")
    if isinstance(symbol, str):
        parts = symbol.split()
        for index, token in enumerate(parts):
            if token.upper() in {"CE", "PE"} and index:
                try:
                    number = float(parts[index - 1])
                    strike_text = str(int(number)) if number.is_integer() else str(number).replace(".", "_")
                    return f"{strike_text}_{token.lower()}"
                except ValueError:
                    continue
    return ""


class SupertrendStateStore:
    def __init__(self, config: Settings) -> None:
        self.base_dir = Path(config.supertrend_state_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _now() -> str:
        return datetime.now(IST).isoformat()

    def _instrument_dir(self, leg_role: str, instrument_key: str, strike_option: str) -> Path:
        suffix = f"{strike_option}_" if strike_option else ""
        return self.base_dir / f"{_safe(leg_role)}__{suffix}{_safe(instrument_key)}"

    def _paths(self, payload: dict[str, Any], instrument_key: str, leg_role: str):
        strike = _strike_option(payload)
        if not strike:
            # Signal/heartbeat frames may omit meta. Reuse the matching
            # directory created by the initial snapshot.
            matches = list(self.base_dir.glob(f"{_safe(leg_role)}__*_{_safe(instrument_key)}"))
            if matches:
                folder = matches[0]
            else:
                folder = self._instrument_dir(leg_role, instrument_key, "")
        else:
            folder = self._instrument_dir(leg_role, instrument_key, strike)
        return folder / SNAPSHOT_FILENAME, folder / LIVE_FILENAME

    @staticmethod
    def _read(path: Path) -> Optional[dict[str, Any]]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except FileNotFoundError:
            return None
        except Exception:
            logger.exception("Could not read Supertrend state %s", path)
            return None

    @staticmethod
    def _write(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with open(fd, "w", encoding="utf-8", closefd=True) as stream:
                json.dump(payload, stream, indent=2, ensure_ascii=False, default=str)
            Path(tmp).replace(path)
        except Exception:
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                pass
            raise

    def save_snapshot(self, payload: dict[str, Any], instrument_key: str,
                      leg_role: str, isolation_id: str) -> None:
        snapshot_path, _ = self._paths(payload, instrument_key, leg_role)
        doc = dict(payload)
        doc.update({
            "instrument_key": instrument_key, "leg_role": leg_role,
            "isolation_id": isolation_id, "indicator": "supertrend",
            "last_event": "snapshot", "updated_at": self._now(),
            "signal_count": len(doc.get("signals") or []),
        })
        try:
            self._write(snapshot_path, doc)
            logger.info("Supertrend snapshot saved leg=%s key=%s signals=%d path=%s",
                        leg_role, instrument_key, doc["signal_count"], snapshot_path)
        except Exception:
            logger.exception("Failed to save Supertrend snapshot for %s", instrument_key)

    def save_signal(self, payload: dict[str, Any], instrument_key: str,
                    leg_role: str, isolation_id: str) -> None:
        snapshot_path, live_path = self._paths(payload, instrument_key, leg_role)
        current = self._read(live_path) or self._read(snapshot_path) or {}
        signals = list(current.get("signals") or [])
        signal = {key: payload.get(key) for key in
                  ("signal", "price", "confirmed_time", "confirmed_time_price")}
        if not signal.get("signal") or not signal.get("confirmed_time"):
            logger.warning("Ignoring malformed Supertrend signal for %s", instrument_key)
            return
        identity = (signal["signal"], signal["confirmed_time"])
        if not any((item.get("signal"), item.get("confirmed_time")) == identity for item in signals):
            signals.append(signal)
        current.update({
            "instrument_key": instrument_key, "leg_role": leg_role,
            "isolation_id": isolation_id, "indicator": "supertrend",
            "trading_symbol": payload.get("trading_symbol") or current.get("trading_symbol"),
            "data_scope": payload.get("data_scope") or current.get("data_scope"),
            "today_ist_date": payload.get("today_ist_date") or current.get("today_ist_date"),
            "signals": signals, "signal_count": len(signals),
            "last_event": "supertrend_signal", "last_signal": signal,
            "updated_at": self._now(),
        })
        try:
            self._write(live_path, current)
        except Exception:
            logger.exception("Failed to save Supertrend signal for %s", instrument_key)

    def update_heartbeat(self, payload: dict[str, Any], instrument_key: str,
                         leg_role: str, isolation_id: str) -> None:
        snapshot_path, live_path = self._paths(payload, instrument_key, leg_role)
        heartbeat = {key: payload.get(key) for key in
                     ("trend", "supertrend", "atr", "last_ts", "last_close",
                      "signal_count_today", "cycle", "today_ist_date", "data_scope")}
        heartbeat["received_at"] = self._now()
        found = False
        for path in (snapshot_path, live_path):
            doc = self._read(path)
            if doc is None:
                continue
            doc["last_heartbeat"] = heartbeat
            try:
                self._write(path, doc)
                found = True
            except Exception:
                logger.exception("Failed to update Supertrend heartbeat %s", path)
        if not found:
            minimal = {
                "instrument_key": instrument_key, "leg_role": leg_role,
                "isolation_id": isolation_id, "indicator": "supertrend",
                "trading_symbol": payload.get("trading_symbol"),
                "data_scope": payload.get("data_scope"),
                "today_ist_date": payload.get("today_ist_date"),
                "signals": [], "signal_count": 0, "last_event": "heartbeat",
                "updated_at": self._now(), "last_heartbeat": heartbeat,
            }
            try:
                self._write(live_path, minimal)
            except Exception:
                logger.exception("Failed to seed Supertrend heartbeat for %s", instrument_key)

    def get_latest(self, instrument_key: str, leg_role: Optional[str] = None) -> Optional[dict[str, Any]]:
        key = _safe(instrument_key)
        pattern = f"{_safe(leg_role)}__*_{key}" if leg_role else f"*__*_{key}"
        best = None
        for folder in self.base_dir.glob(pattern):
            if not folder.is_dir():
                continue
            for filename in (SNAPSHOT_FILENAME, LIVE_FILENAME):
                doc = self._read(folder / filename)
                if not doc or doc.get("instrument_key") != instrument_key:
                    continue
                if best is None or (doc.get("updated_at") or "") > (best.get("updated_at") or ""):
                    best = doc
        return best

    def delete_for_instrument_keys(self, keys: list[str]) -> int:
        deleted = 0
        for key in keys:
            pattern = f"*_{_safe(key)}"
            for folder in list(self.base_dir.glob(pattern)):
                if folder.is_dir():
                    try:
                        deleted += sum(1 for item in folder.rglob("*") if item.is_file())
                        shutil.rmtree(folder)
                    except Exception:
                        logger.exception("Failed to purge Supertrend folder %s", folder)
        logger.info("Supertrend state purge keys=%s files_deleted=%d", keys, deleted)
        return deleted
