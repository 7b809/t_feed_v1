"""Durable local archive for the active isolation and its two option legs."""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core.logger import get_logger

logger = get_logger(__file__)
IST = ZoneInfo("Asia/Kolkata")


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "unknown")).strip("._") or "unknown"


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class IsolatedArchiveService:
    """Writes a complete isolation snapshot atomically to data/isolated."""

    def __init__(self, root: str | None = None):
        self.root = Path(root or os.getenv("ISOLATED_DATA_DIR", "data/isolated"))

    def save_isolation(self, record: dict) -> str:
        trade_date = str(record.get("trade_date_ist") or datetime.now(IST).date())
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / "isolated_instrument.json"

        isolated = {
            "role": "isolated",
            "instrument_key": record.get("instrument_key"),
            "trading_symbol": record.get("trading_symbol"),
            "exchange_token": record.get("exchange_token"),
            "name": record.get("name"),
            "short_name": record.get("short_name"),
            "strike": record.get("strike"),
            "option_type": record.get("option_type"),
            "expiry": record.get("expiry"),
            "lot_size": record.get("lot_size"),
            "tick_size": record.get("tick_size"),
            "exchange": record.get("exchange"),
            "segment": record.get("segment"),
            "ltp_at_isolation": record.get("isolated_option_ltp"),
            "ltp_source": record.get("isolated_option_ltp_source"),
        }
        opposite = dict(record.get("opposite_leg") or {})
        opposite["role"] = "opposite"
        document = {
            "schema_version": 1,
            "trade_date_ist": trade_date,
            "isolation_id": f"{trade_date}__{record.get('instrument_key') or 'unknown'}",
            "status": record.get("status", "ACTIVE"),
            "bias": record.get("bias"),
            "trigger": {
                "index": record.get("trigger_index"),
                "canonical": record.get("trigger_index_canonical"),
                "side": record.get("trigger_side"),
                "level_name": record.get("trigger_level_name"),
                "level": record.get("trigger_level"),
                "index_ltp": record.get("trigger_value"),
            },
            "timings": {
                "touch_time_ist": record.get("isolation_touch_time"),
                "created_at_ist": record.get("isolation_created_at"),
                "execution_mode_decided_at": record.get("execution_mode_decided_at"),
                "archived_at_ist": datetime.now(IST).isoformat(),
            },
            "execution": {
                "mode": record.get("standard_execution_mode"),
                "early_extreme_reversal": record.get("early_extreme_reversal"),
                "reason": record.get("execution_mode_reason"),
            },
            "market_context": {
                "nifty_ltp": record.get("nifty_ltp_at_isolation"),
                "nifty_ltp_source": record.get("nifty_ltp_source"),
                "strike": record.get("strike_computed"),
                "expiry": record.get("expiry"),
            },
            "instruments": {"isolated": isolated, "opposite": opposite},
            "source_record": record,
        }
        fd, temp_name = tempfile.mkstemp(prefix="isolated_", suffix=".tmp", dir=str(self.root))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, ensure_ascii=False, default=_json_default)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        logger.info("Isolation archive saved: %s", target)
        return str(target)

    def load_current(self) -> dict | None:
        path = self.root / "isolated_instrument.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Could not load isolation archive %s", path)
            return None
