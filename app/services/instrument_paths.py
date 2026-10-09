"""Shared path / underlying resolution for candle, EMA, and live jobs.

Layout
------
Options and futures::

    data/<underlying>/<strike>_<strike_type>/historical.json
    data/<underlying>/<strike>_<strike_type>/ema_crosses.json

Index / underlying instruments (e.g. ``NSE_INDEX|Nifty 50``)::

    data/index/<underlying>/historical.json
    data/index/<underlying>/ema_crosses.json

Non-option, non-index instruments (fallback) live under
``data/<underlying>/<instrument_type or 'spot'>/``.

All jobs must agree on where an instrument's files live, so the path logic
lives here in exactly one place.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from app.core.config import settings

# ---------------------------------------------------------------------------
# Underlying / index name normalisation
# ---------------------------------------------------------------------------
_INDEX_ALIASES: dict[str, str] = {
    "nifty50": "nifty",
    "nifty": "nifty",
    "banknifty": "banknifty",
    "finnifty": "finnifty",
    "midcpnifty": "midcpnifty",
    "sensex": "sensex",
    "bankex": "bankex",
}


def normalize_underlying_name(name: str) -> str:
    """Normalise ``"Nifty 50"``/``"BANKNIFTY"``/``"Sensex"`` to a folder name."""
    if not isinstance(name, str):
        return "unknown"
    raw = name.strip().lower().replace(" ", "").replace("_", "")
    if not raw:
        return "unknown"
    return _INDEX_ALIASES.get(raw, raw)


def _strip_index_prefix(value: str) -> str:
    """Turn ``"NSE_INDEX|Nifty 50"`` into ``"Nifty 50"``."""
    if "|" in value:
        return value.split("|", 1)[1]
    return value


# ---------------------------------------------------------------------------
# Index instrument detection
# ---------------------------------------------------------------------------
def is_index_instrument(item: dict[str, Any]) -> bool:
    """Return True for index/underlying rows like ``NSE_INDEX|Nifty 50``."""
    ikey = str(item.get("instrument_key") or "")
    if ikey.startswith("NSE_INDEX|") or ikey.startswith("BSE_INDEX|"):
        return True

    # Fallback based on shape: no strike, no option type, source tag matches.
    strike = item.get("strike_price")
    has_strike = strike not in (None, "", 0) and strike != 0
    itype = item.get("instrument_type")
    source = str(item.get("source") or "")
    if not has_strike and not itype and source == "startup_underlying":
        return True

    return False


# ---------------------------------------------------------------------------
# Underlying resolution
# ---------------------------------------------------------------------------
def resolve_underlying(item: dict[str, Any]) -> str:
    """Resolve an instrument to a normalised underlying folder name.

    Order of precedence:
    1. Explicit field (``underlying``, ``underlying_symbol``, ...).
    2. ``underlying_key`` (e.g. ``NSE_INDEX|Nifty 50``).
    3. Prefix of ``trading_symbol`` (``BANKNIFTY...``, ``NIFTY...``, ...).
    4. The instrument_key itself, if it is an index row.
    """
    for key in ("underlying", "underlying_symbol", "asset", "root_symbol"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_underlying_name(value)

    uk = item.get("underlying_key")
    if isinstance(uk, str) and uk.strip():
        return normalize_underlying_name(_strip_index_prefix(uk))

    symbol = str(item.get("trading_symbol") or "").upper()
    for name in ("MIDCPNIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "NIFTY"):
        if symbol.startswith(name):
            return normalize_underlying_name(name)

    # Last resort: derive from instrument_key when it is an index row.
    if is_index_instrument(item):
        ikey = str(item.get("instrument_key") or "")
        return normalize_underlying_name(_strip_index_prefix(ikey))

    return "unknown"


# ---------------------------------------------------------------------------
# Directory + file paths
# ---------------------------------------------------------------------------
def _instrument_dir(item: dict[str, Any]) -> Path:
    underlying = resolve_underlying(item)

    # Index / underlying instruments -> data/index/<underlying>/
    if is_index_instrument(item):
        return settings.data_dir / "index" / underlying

    # Options and futures -> data/<underlying>/<strike>_<type>/
    strike = item.get("strike_price")
    strike_type = str(item.get("instrument_type") or "").strip().upper()

    try:
        strike_num = float(strike)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        strike_num = 0.0

    if strike_num > 0:
        strike_str = (
            str(int(strike_num)) if strike_num.is_integer() else str(strike_num)
        )
        folder = f"{strike_str}_{strike_type}" if strike_type else strike_str
    else:
        folder = strike_type or "spot"

    return settings.data_dir / underlying / folder


def build_candle_path(item: dict[str, Any]) -> Path:
    """Path to the merged history+intraday candle file for an instrument."""
    return _instrument_dir(item) / "historical.json"


def build_ema_cross_path(item: dict[str, Any]) -> Path:
    """Path to the EMA cross file for an instrument."""
    return _instrument_dir(item) / "ema_crosses.json"


# ---------------------------------------------------------------------------
# Per-file write locks
# ---------------------------------------------------------------------------
# Multiple jobs (batch EMA + live EMA) can write to the same
# ``ema_crosses.json``. This registry hands out a single ``threading.Lock``
# per absolute path so writes never interleave.
_write_locks: dict[str, threading.Lock] = {}
_write_locks_guard = threading.Lock()


def get_file_write_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _write_locks_guard:
        lock = _write_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _write_locks[key] = lock
        return lock