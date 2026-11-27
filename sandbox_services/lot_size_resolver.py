"""
lot_size_resolver.py

Centralised lot-size resolver shared by every sandbox order service
(``place_order_service``, ``place_gtt_order``, ``place_dual_gtt_service``).

Resolution order — first match wins
-----------------------------------
    1. Explicit ``lot_size_override`` at the caller site   (handled upstream)
    2. Upstox instruments master keyed by ``instrument_key`` [AUTHORITATIVE]
    3. ``SYMBOL_LOT_SIZE_MAP``                              [fast fallback]
    4. ``EXCHANGE_DEFAULT_LOT_MAP``                         [last-ditch fallback]

The master is downloaded from the public Upstox CDN
(https://assets.upstox.com/market-quote/instruments/exchange/*.json.gz).
No access token is required. The file is cached for ``CACHE_TTL_SECONDS``
(default 6 h) and refreshed lazily; call ``warm_master_cache()`` at app
startup to preload it.

Why this matters
----------------
``EXCHANGE_DEFAULT_LOT_MAP["NSE_FO"]`` is an *approximation* — the real lot
size is per-contract (NIFTY=65, BANKNIFTY=30, FINNIFTY=60, stock F&O varies
per stock, …). Only the instruments master holds the correct value for a
specific ``instrument_key`` like ``NSE_FO|47892``.
"""
from __future__ import annotations

import gzip
import json
import threading
import urllib.parse
from datetime import datetime, timezone
from typing import Dict, Optional

import requests

from core.logger import get_logger

logger = get_logger(__file__)

# ---------------------------------------------------------------------------
# Fallback maps — used only if the instruments master is unreachable
# ---------------------------------------------------------------------------
# Sources:
#   * NSE circular FAOP70616 (eff. Oct 28, 2025; fully from Dec 30, 2025)
#   * BSE Index Derivatives lot sizes (stable since mid-2025)
# ---------------------------------------------------------------------------
SYMBOL_LOT_SIZE_MAP: Dict[str, int] = {
    # NSE index derivatives
    "NIFTY":      65,
    "BANKNIFTY":  30,
    "FINNIFTY":   60,   # <-- was 65 in the old map (stale)
    "MIDCPNIFTY": 120,
    "NIFTYNXT50": 25,
    # BSE index derivatives
    "SENSEX":     20,
    "BANKEX":     30,
}

EXCHANGE_DEFAULT_LOT_MAP: Dict[str, int] = {
    "NSE_FO": 65,
    "BSE_FO": 20,
    "NSE_EQ": 1,
    "BSE_EQ": 1,
}

# ---------------------------------------------------------------------------
# Instrument-master cache (thread-safe, lazily refreshed)
# ---------------------------------------------------------------------------
_MASTER_URLS = {
    "NSE": "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",
    "BSE": "https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz",
}

_MASTER: Dict[str, int] = {}
_MASTER_LOADED_AT: Optional[datetime] = None
_MASTER_LOCK = threading.Lock()
_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 h


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalize_instrument_token(instrument_token: str) -> str:
    """URL-decode and strip an instrument key: 'NSE_FO%7C47892' -> 'NSE_FO|47892'."""
    return urllib.parse.unquote(instrument_token).strip()


def _download_master(url: str) -> Dict[str, int]:
    """Download one exchange master and return ``{instrument_key: lot_size}``."""
    logger.info(f"Downloading instrument master: {url}")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    raw = gzip.decompress(resp.content)
    rows = json.loads(raw)

    out: Dict[str, int] = {}
    for row in rows:
        key = row.get("instrument_key")
        lot = row.get("lot_size")
        if key and lot is not None:
            try:
                out[key] = int(lot)
            except (TypeError, ValueError):
                continue
    logger.info(f"Loaded {len(out)} instruments from {url.rsplit('/', 1)[-1]}")
    return out


def _ensure_master_loaded() -> None:
    """Refresh the cache if empty or stale. Thread-safe."""
    global _MASTER, _MASTER_LOADED_AT

    with _MASTER_LOCK:
        now = datetime.now(timezone.utc)
        if (
            _MASTER
            and _MASTER_LOADED_AT
            and (now - _MASTER_LOADED_AT).total_seconds() < _CACHE_TTL_SECONDS
        ):
            return

        combined: Dict[str, int] = {}
        errors = []
        for exch, url in _MASTER_URLS.items():
            try:
                combined.update(_download_master(url))
            except Exception as exc:
                errors.append((exch, str(exc)))
                logger.warning(f"Could not download {exch} master: {exc}")

        if combined:
            _MASTER = combined
            _MASTER_LOADED_AT = now
        if errors and not combined:
            raise RuntimeError(
                f"All instrument master downloads failed: {errors}"
            )


def warm_master_cache() -> None:
    """
    Preload the master cache — call once from FastAPI ``lifespan`` startup.
    Never raises; logs a warning if the download fails.
    """
    try:
        _ensure_master_loaded()
        logger.info(f"Lot-size master cache warmed ({len(_MASTER)} instruments).")
    except Exception as exc:
        logger.warning(f"Lot-size master warm-up failed: {exc}")


def resolve_lot_size_from_master(instrument_token: str) -> Optional[int]:
    """Return the authoritative ``lot_size`` for the given key, or ``None``."""
    clean = normalize_instrument_token(instrument_token)
    try:
        _ensure_master_loaded()
    except Exception as exc:
        logger.warning(f"Instrument master unavailable, falling back: {exc}")
        return None
    return _MASTER.get(clean)


# ---------------------------------------------------------------------------
# Public resolver
# ---------------------------------------------------------------------------
def resolve_lot_size(
    instrument_token: str,
    symbol: Optional[str] = None,
    *,
    strict: bool = False,
) -> int:
    """
    Resolve the lot size for an instrument.

    Parameters
    ----------
    instrument_token : str
        Upstox instrument key (raw or URL-encoded).
    symbol : str, optional
        Underlying symbol (``NIFTY``, ``SENSEX``, …) — used only if the
        master lookup fails.
    strict : bool, default False
        When True, raise ``LookupError`` instead of falling back when the
        master has no entry for this instrument. Recommended for LIVE trading.
    """
    # --- Tier 2 — instrument master (authoritative) -------------------------
    master_lot = resolve_lot_size_from_master(instrument_token)
    if master_lot:
        logger.debug(
            f"lot_size={master_lot} for '{instrument_token}' (source=master)"
        )
        return master_lot

    if strict:
        raise LookupError(
            f"Instrument master has no lot_size for '{instrument_token}' "
            f"and strict=True."
        )

    # --- Tier 3 — symbol map -----------------------------------------------
    if symbol:
        clean_sym = symbol.upper().strip()
        if clean_sym in SYMBOL_LOT_SIZE_MAP:
            lot = SYMBOL_LOT_SIZE_MAP[clean_sym]
            logger.debug(
                f"lot_size={lot} for '{instrument_token}' "
                f"(source=symbol_map, symbol='{symbol}')"
            )
            return lot

    # --- Tier 4 — exchange default -----------------------------------------
    normalized = normalize_instrument_token(instrument_token)
    exchange = normalized.split("|")[0].upper() if "|" in normalized else ""
    lot = EXCHANGE_DEFAULT_LOT_MAP.get(exchange, 1)
    logger.warning(
        f"lot_size={lot} for '{instrument_token}' "
        f"(source=exchange_default, exchange='{exchange}') — "
        f"verify before live trading."
    )
    return lot