# core/greeks_regime_config.py
"""Environment-backed configuration for the independent live Greeks monitor."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from core import config

# Load .env BEFORE reading any environment variables below.
# override=False means real environment variables win over .env values.
load_dotenv()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


GREEKS_REGIME_ENABLED = _bool("GREEKS_REGIME_ENABLED", True)
GREEKS_REGIME_LOOKBACK_MINUTES = _int("GREEKS_REGIME_LOOKBACK_MINUTES", 20, 5)
GREEKS_REGIME_Z_THRESHOLD = _float("GREEKS_REGIME_Z_THRESHOLD", 0.5, 0.05)
GREEKS_REGIME_STD_FLOOR = _float("GREEKS_REGIME_STD_FLOOR", 0.0001, 0.00000001)
GREEKS_REGIME_TIMEZONE = (os.getenv("GREEKS_REGIME_TIMEZONE") or config.MARKET_TIMEZONE).strip()

_output_raw = os.getenv("GREEKS_REGIME_OUTPUT_DIR")
if _output_raw:
    _output_path = Path(_output_raw).expanduser()
    if not _output_path.is_absolute():
        _output_path = config.PROJECT_ROOT / _output_path
    GREEKS_REGIME_OUTPUT_DIR = _output_path
else:
    GREEKS_REGIME_OUTPUT_DIR = Path(config.LIVE_DATA_DIR)

GREEKS_REGIME_FSYNC = _bool("GREEKS_REGIME_FSYNC", False)