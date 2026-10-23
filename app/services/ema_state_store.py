# app\services\ema_state_store.py
#
# Persists Job-2 runtime state so restarts never lose EMA continuity.
#
# Files written:
#   storage/mega/_ema_state/<safe_key>.json         -> {ema_fast, ema_slow, last_ts, last_close}
#   storage/mega/index/<underlying>/ema_crosses.json   -> index instruments (nifty, sensex)
#   storage/mega/<underlying>/<strike>_<type>/ema_crosses.json -> options

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class EmaStateStore:
    def __init__(self, config: Settings):
        self.config = config
        self.base_dir = config.destination_dir
        self.state_dir = config.ema_state_dir

    # ------------------------------------------------------------------ #
    # Path helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _safe(key: str) -> str:
        return _SAFE.sub("_", key)

    def state_file(self, instrument_key: str) -> Path:
        return self.state_dir / f"{self._safe(instrument_key)}.json"

    def crosses_file(self, instrument_key: str, meta: dict[str, Any]) -> Path:
        strike_type = (meta.get("strike_type") or "").upper()
        underlying = (meta.get("underlying") or "").lower()
        if strike_type == "INDEX" or not strike_type:
            return self.base_dir / "index" / underlying / "ema_crosses.json"
        strike = meta.get("strike_price")
        if strike is None:
            return self.base_dir / underlying / f"{strike_type}" / "ema_crosses.json"
        strike_str = str(int(strike)) if float(strike).is_integer() else str(strike)
        return self.base_dir / underlying / f"{strike_str}_{strike_type}" / "ema_crosses.json"

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #
    def load_state(self, instrument_key: str) -> dict[str, Any] | None:
        path = self.state_file(instrument_key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read EMA state %s: %s", path, exc)
            return None

    def save_state(self, instrument_key: str, state: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = dict(state)
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        path = self.state_file(instrument_key)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)

    # ------------------------------------------------------------------ #
    # Crosses
    # ------------------------------------------------------------------ #
    def load_crosses(self, instrument_key: str, meta: dict[str, Any]) -> dict[str, Any] | None:
        path = self.crosses_file(instrument_key, meta)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read EMA crosses %s: %s", path, exc)
            return None

    def save_crosses(
        self,
        instrument_key: str,
        meta: dict[str, Any],
        candles_processed: int,
        crosses: list[dict],
    ) -> None:
        path = self.crosses_file(instrument_key, meta)
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = self.load_crosses(instrument_key, meta) or {}
        validation = previous.get("validation", {
            "enabled": True,
            "min_score": 3,
            "filters": {
                "trend": True, "separation": True, "slope": True,
                "volume": True, "body": True, "distance": True,
            },
        })
        capped = crosses[-self.config.ema_candle_count :]
        payload = {
            "instrument_key": instrument_key,
            "underlying": meta.get("underlying"),
            "strike_price": meta.get("strike_price"),
            "strike_type": meta.get("strike_type"),
            "expiry": meta.get("expiry"),
            "trading_symbol": meta.get("trading_symbol"),
            "fast_period": self.config.ema_fast_period,
            "slow_period": self.config.ema_slow_period,
            "candle_count": candles_processed,
            "cross_count": len(capped),
            "valid_cross_count": len(capped),
            "validation": validation,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "crosses": capped,
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)