"""Lot-size table used by upstox_services.

Values reflect the 2026 NSE/BSE F&O lot-size revision:
    NIFTY 50       -> 65
    SENSEX         -> 20
    BANKNIFTY      -> 30
    FINNIFTY       -> 60
    MIDCPNIFTY     -> 120

Resolution order
----------------
1. Explicit ``underlying`` (already normalized by the caller).
2. Registry trading symbol / underlying.
3. Scan the raw instrument key for a known ticker substring
   (e.g. ``NSE_FO|NIFTY25OCT25000CE``).
4. Per-exchange default (``NSE_FO`` -> NIFTY 65, ``BSE_FO`` -> SENSEX 20).
5. Last resort: qty = lots (unit_fallback).

Step 4 is what prevents the ``UDAPI1104: Quantity should be multiple
of lot size`` error when the registry does not have an entry for the
instrument key (fresh deploy, ad-hoc key, chart-only key, etc.).
"""

from __future__ import annotations

from typing import Any

from services.normalization import normalize_underlying

LOT_SIZES: dict[str, int] = {
    "NIFTY": 65,
    "SENSEX": 20,
    "BANKNIFTY": 30,
    "FINNIFTY": 60,
    "MIDCPNIFTY": 120,
}

# Fallback used only when steps 1-3 fail. This is what keeps the
# margin check from sending qty=1 on an unknown NSE_FO / BSE_FO key.
DEFAULT_LOT_SIZES_BY_EXCHANGE: dict[str, int] = {
    "NSE_FO": 65,   # NIFTY 50 default
    "BSE_FO": 20,   # SENSEX default
}


def get_lot_size(underlying: str | None) -> int | None:
    """Return the lot size for a normalized underlying ticker, or None."""
    key = normalize_underlying(underlying)
    if not key:
        return None
    return LOT_SIZES.get(key)


def _infer_underlying_from_key(key: str) -> str | None:
    """Scan the raw instrument key for a known ticker substring."""
    text = str(key or "").upper()
    if not text:
        return None
    # Longest names first so BANKNIFTY does not match NIFTY.
    for name in sorted(LOT_SIZES, key=len, reverse=True):
        if name in text:
            return name
    return None


def _default_for_exchange(instrument_key: str) -> int | None:
    text = str(instrument_key or "").upper()
    if text.startswith("NSE_FO"):
        return DEFAULT_LOT_SIZES_BY_EXCHANGE["NSE_FO"]
    if text.startswith("BSE_FO"):
        return DEFAULT_LOT_SIZES_BY_EXCHANGE["BSE_FO"]
    return None


def lots_to_quantity(
    *,
    underlying: str | None,
    lots: int,
    instrument_key: str | None = None,
) -> tuple[int, int | None, str]:
    """Convert lots to a concrete quantity.

    Returns ``(quantity, lot_size, source)`` where ``source`` is one of:

        "lot_table"           — lot size from the underlying
        "lot_table_inferred"  — lot size inferred from the key
        "exchange_default"    — per-exchange fallback (NSE_FO / BSE_FO)
        "unit_fallback"       — nothing matched; qty = lots
    """
    try:
        n_lots = int(lots)
    except (TypeError, ValueError):
        n_lots = 1
    if n_lots < 1:
        n_lots = 1

    # 1) Explicit underlying
    lot = get_lot_size(underlying)
    if lot is not None:
        return n_lots * lot, lot, "lot_table"

    # 2) Infer from the instrument key
    inferred_name = _infer_underlying_from_key(instrument_key or "")
    if inferred_name:
        inferred_lot = LOT_SIZES.get(inferred_name)
        if inferred_lot is not None:
            return n_lots * inferred_lot, inferred_lot, "lot_table_inferred"

    # 3) Per-exchange default
    exchange_default = _default_for_exchange(instrument_key or "")
    if exchange_default is not None:
        return (
            n_lots * exchange_default,
            exchange_default,
            "exchange_default",
        )

    # 4) Nothing matched
    return n_lots, None, "unit_fallback"