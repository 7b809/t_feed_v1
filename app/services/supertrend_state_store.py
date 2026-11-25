"""Atomic per-instrument, per-interval Supertrend snapshot persistence."""
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

    @staticmethod
    def _interval_suffix(interval: str) -> str:
        value = str(interval).strip().lower()
        if value in {"1", "1m", "01m"}:
            return ""
        if value.endswith("m"):
            value = value[:-1]
        if not value.isdigit() or int(value) < 1:
            raise ValueError(f"Invalid Supertrend interval: {interval!r}")
        return f"_{int(value)}m"

    def state_file(
        self, instrument_key: str, meta: dict[str, Any], interval: str = "1m"
    ) -> Path:
        suffix = self._interval_suffix(interval)
        strike_type = (meta.get("strike_type") or "").upper()
        underlying = (meta.get("underlying") or "").lower()
        filename = f"supertrend{suffix}.json"
        if strike_type == "INDEX" or not strike_type:
            folder = (meta.get("folder") or underlying or "index").strip().lower()
            return self.base_dir / "index" / folder / filename
        strike = meta.get("strike_price")
        folder = strike_type if strike is None else (
            f"{int(strike) if float(strike).is_integer() else strike}_{strike_type}"
        )
        return self.base_dir / underlying / folder / filename

    def load(
        self, instrument_key: str, meta: dict[str, Any], interval: str = "1m"
    ) -> dict[str, Any] | None:
        path = self.state_file(instrument_key, meta, interval)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read Supertrend state %s: %s", path, exc)
            return None

    def save(
        self, instrument_key: str, meta: dict[str, Any], record: dict[str, Any],
        interval: str = "1m",
    ) -> Path:
        path = self.state_file(instrument_key, meta, interval)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(record)
        payload.update({
            "instrument_key": instrument_key,
            "interval": self._interval_suffix(interval).lstrip("_") or "1m",
            "underlying": meta.get("underlying"),
            "strike_price": meta.get("strike_price"),
            "strike_type": meta.get("strike_type"),
            "expiry": meta.get("expiry"),
            "trading_symbol": meta.get("trading_symbol"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return path
