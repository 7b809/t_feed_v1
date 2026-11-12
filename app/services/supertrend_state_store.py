"""Atomic per-instrument Supertrend snapshot persistence (same tree layout as SR)."""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from app.config import Settings

logger = logging.getLogger(__name__)

class SupertrendStateStore:
    def __init__(self, config: Settings) -> None:
        self.config, self.base_dir = config, config.destination_dir

    def state_file(self, instrument_key: str, meta: dict[str, Any]) -> Path:
        strike_type = (meta.get("strike_type") or "").upper()
        underlying = (meta.get("underlying") or "").lower()
        if strike_type == "INDEX" or not strike_type:
            folder = (meta.get("folder") or underlying or "index").strip().lower()
            return self.base_dir / "index" / folder / "supertrend.json"
        strike = meta.get("strike_price")
        folder = strike_type if strike is None else (
            f"{int(strike) if float(strike).is_integer() else strike}_{strike_type}"
        )
        return self.base_dir / underlying / folder / "supertrend.json"

    def load(self, instrument_key: str, meta: dict[str, Any]) -> dict[str, Any] | None:
        path = self.state_file(instrument_key, meta)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read Supertrend state %s: %s", path, exc)
            return None

    def save(self, instrument_key: str, meta: dict[str, Any], record: dict[str, Any]) -> Path:
        path = self.state_file(instrument_key, meta)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(record)
        payload.update({
            "instrument_key": instrument_key, "underlying": meta.get("underlying"),
            "strike_price": meta.get("strike_price"), "strike_type": meta.get("strike_type"),
            "expiry": meta.get("expiry"), "trading_symbol": meta.get("trading_symbol"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return path
