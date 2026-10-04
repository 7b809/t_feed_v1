"""
Readonly storage for per-contract candle JSON files.

Layout:
    data/readonly/<INDEX_NAME>/candles/<strike>_<CE|PE>.json

Write-once semantics: callers should check `exists()` first.
Freshness: `is_fresh()` and `stale_contracts()` decide whether a file
still represents the latest available market data. Used by both the
startup loader and the daily refresh so stale files are refetched.
"""
import json
import os
import stat
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from upstox_app.common.config import upstox_config

logger = get_logger(__name__)


def _format_strike(value: Any) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f.is_integer():
        return str(int(f))
    return str(f)


class CandleStorage:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._readonly_root = Path(
            getattr(upstox_config, "OPTIONS_READONLY_DIR", "data/readonly")
        )
        self._warned_windows = False

    # ---- paths ---------------------------------------------------------
    def index_candle_dir(self, index_name: str) -> Path:
        d = self._readonly_root / index_name.upper() / "candles"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def candle_path(self, index_name: str, strike: Any, option_type: str) -> Path:
        folder = self.index_candle_dir(index_name)
        return folder / f"{_format_strike(strike)}_{option_type.upper()}.json"

    def exists(self, index_name: str, strike: Any, option_type: str) -> bool:
        if strike is None or not option_type:
            return False
        return self.candle_path(index_name, strike, option_type).exists()

    # ---- read/write ----------------------------------------------------
    def save(self, index_name: str, strike: Any, option_type: str,
             payload: Dict[str, Any]) -> Path:
        path = self.candle_path(index_name, strike, option_type)
        with self._lock:
            tmp = path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, default=str)
            self._make_writable(path)
            tmp.replace(path)
            self._apply_readonly(path)
        return path

    def overwrite(self, index_name: str, strike: Any, option_type: str,
                  payload: Dict[str, Any]) -> Path:
        return self.save(index_name, strike, option_type, payload)

    def load(self, index_name: str, strike: Any, option_type: str) -> Optional[Dict[str, Any]]:
        path = self.candle_path(index_name, strike, option_type)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to read candle file | path={path} | err={exc}")
            return None

    def stored_expiry(self, index_name: str, strike: Any, option_type: str) -> Optional[str]:
        data = self.load(index_name, strike, option_type)
        if not isinstance(data, dict):
            return None
        expiry = data.get("expiry")
        return str(expiry) if expiry else None

    # ---- freshness -----------------------------------------------------
    def is_fresh(
        self,
        index_name: str,
        strike: Any,
        option_type: str,
        last_market_day_iso: str,
    ) -> bool:
        """
        A file is fresh when:
          - it exists
          - status is "success"
          - historical_count > 0 (non-empty candles array for the historical side)
          - its stored to_date is >= last_market_day_iso
        """
        data = self.load(index_name, strike, option_type)
        if not isinstance(data, dict):
            return False
        if data.get("status") != "success":
            return False
        try:
            if int(data.get("historical_count") or 0) <= 0:
                return False
        except (TypeError, ValueError):
            return False
        stored_to = data.get("to_date")
        if not stored_to or str(stored_to) < last_market_day_iso:
            return False
        return True

    def stale_contracts(
        self,
        index_name: str,
        contracts: List[Dict[str, Any]],
        last_market_day_iso: str,
    ) -> List[Dict[str, Any]]:
        """
        Contracts that need (re)fetching. Missing files count as stale
        because `is_fresh` returns False for them.
        """
        stale: List[Dict[str, Any]] = []
        for c in contracts:
            strike = c.get("strike_price")
            otype = c.get("option_type") or c.get("instrument_type")
            if strike is None or not otype:
                continue
            if not self.is_fresh(index_name, strike, otype, last_market_day_iso):
                stale.append(c)
        return stale

    def missing_contracts(self, index_name: str, contracts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Legacy existence-only check. Kept for backward compatibility."""
        missing: List[Dict[str, Any]] = []
        for c in contracts:
            strike = c.get("strike_price")
            otype = c.get("option_type") or c.get("instrument_type")
            if strike is None or not otype:
                continue
            if not self.exists(index_name, strike, otype):
                missing.append(c)
        return missing

    # ---- permissions ---------------------------------------------------
    def _make_writable(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        except Exception:
            pass

    def _apply_readonly(self, path: Path) -> None:
        try:
            os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        except Exception as exc:
            if os.name == "nt" and not self._warned_windows:
                self._warned_windows = True
                logger.warning(
                    f"chmod not enforced on Windows for candle files | path={path} | err={exc}"
                )
            elif os.name != "nt":
                logger.warning(f"chmod failed | path={path} | err={exc}")


candle_storage = CandleStorage()