"""
Writable storage for EMA crossover JSON files.

Layout:
    data/runtime/<INDEX>/<strike>_<CE|PE>/historic_cross.json
    data/runtime/<INDEX>/<strike>_<CE|PE>/intraday_cross.json

Unlike candle files, crossover files are writable (runtime tree) and are
overwritten on every recomputation. The runtime root is taken from
upstox_config.OPTIONS_RUNTIME_DIR.
"""
import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

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


class CrossoverStorage:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runtime_root = Path(
            getattr(upstox_config, "OPTIONS_RUNTIME_DIR", "data/runtime")
        )

    # ---- paths ---------------------------------------------------------
    def contract_dir(self, index_name: str, strike: Any, option_type: str) -> Path:
        d = (
            self._runtime_root
            / index_name.upper()
            / f"{_format_strike(strike)}_{option_type.upper()}"
        )
        d.mkdir(parents=True, exist_ok=True)
        return d

    def path(
        self, index_name: str, strike: Any, option_type: str, source: str
    ) -> Path:
        if source not in ("historic", "intraday"):
            raise ValueError(f"source must be 'historic' or 'intraday', got {source!r}")
        return self.contract_dir(index_name, strike, option_type) / f"{source}_cross.json"

    # ---- write / read --------------------------------------------------
    def save(
        self,
        index_name: str,
        strike: Any,
        option_type: str,
        source: str,
        payload: Dict[str, Any],
    ) -> Path:
        path = self.path(index_name, strike, option_type, source)
        with self._lock:
            tmp = path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, default=str)
            tmp.replace(path)
        return path

    def load(
        self, index_name: str, strike: Any, option_type: str, source: str
    ) -> Optional[Dict[str, Any]]:
        path = self.path(index_name, strike, option_type, source)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to read crossover file | path={path} | err={exc}")
            return None


crossover_storage = CrossoverStorage()