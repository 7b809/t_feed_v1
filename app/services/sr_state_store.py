# app/services/sr_state_store.py
"""
Persists per-instrument, per-config SR levels.

Layout (one file per config):
  storage/mega/index/<folder>/sr_levels_<left_bars>_<right_bars>.json
  storage/mega/<underlying>/<strike>_<type>/sr_levels_<left_bars>_<right_bars>.json

For config '15/3' the filename is sr_levels_15_3.json (same as before — the
config→filename mapping is unchanged; only the semantics of the two numbers
changed: <left_bars>/<right_bars>).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.services.sr_calculator import config_slug, parse_config

logger = logging.getLogger(__name__)


class SrStateStore:
    def __init__(self, config: Settings) -> None:
        self.config = config
        self.base_dir = config.destination_dir

    def sr_file(self, instrument_key: str, meta: dict[str, Any], config: str) -> Path:
        left_bars, right_bars = parse_config(config)
        strike_type = (meta.get("strike_type") or "").upper()
        underlying = (meta.get("underlying") or "").lower()
        filename = f"sr_levels_{left_bars}_{right_bars}.json"

        if strike_type == "INDEX" or not strike_type:
            folder = (
                meta.get("folder") or underlying or "index"
            ).strip().lower() or "index"
            return self.base_dir / "index" / folder / filename

        strike = meta.get("strike_price")
        if strike is None:
            return self.base_dir / underlying / strike_type / filename
        strike_str = str(int(strike)) if float(strike).is_integer() else str(strike)
        return self.base_dir / underlying / f"{strike_str}_{strike_type}" / filename

    def load(
        self, instrument_key: str, meta: dict[str, Any], config: str
    ) -> dict[str, Any] | None:
        path = self.sr_file(instrument_key, meta, config)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read SR file %s: %s", path, exc)
            return None

    def save(
        self,
        instrument_key: str,
        meta: dict[str, Any],
        config: str,
        record: dict[str, Any],
    ) -> Path:
        path = self.sr_file(instrument_key, meta, config)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(record)
        payload["instrument_key"] = instrument_key
        payload["config"] = config_slug(config)
        payload["underlying"] = meta.get("underlying")
        payload["strike_price"] = meta.get("strike_price")
        payload["strike_type"] = meta.get("strike_type")
        payload["expiry"] = meta.get("expiry")
        payload["trading_symbol"] = meta.get("trading_symbol")
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return path