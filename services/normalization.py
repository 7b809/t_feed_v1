# services/normalization.py
"""Shared normalization for user-facing instrument selectors.

Users pass query parameters in many shapes:

    underlying : "nifty", "NiFty", "NIFTY 50", "nifty50",
                 "NIFTY_50", "BANKNIFTY", "Bank Nifty", ...
    type       : "ce", "CE", "Ce", "call", "Call", "pe", "put", ...
    strike     : 29500, "29500", "29500.0", "29,500"

This module maps all of those to the canonical forms used by the
registry and the strategy selection service:

    underlying : uppercase, no separators, aliased to a known ticker
    type       : "CE" / "PE" / "FUT"
    strike     : plain integer string, e.g. "22650"

Public API
----------

    normalize_underlying(value)   -> "NIFTY" | "SENSEX" | ...
    normalize_strike_type(value)  -> "CE" | "PE" | "FUT" | ""
    normalize_strike(value)       -> "22650" | ""
    normalize_selector(...)       -> dict of all four fields
    canonical_ticker(value)       -> alias of normalize_underlying

Dependency-free, so it can be imported from anywhere in the project.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Alias tables
# ---------------------------------------------------------------------------
# Canonical name -> accepted variants (matched after whitespace and
# separator stripping, uppercased).
UNDERLYING_ALIASES: dict[str, tuple[str, ...]] = {
    "NIFTY": (
        "NIFTY", "NIFTY50", "NIFTY_50", "NIFTY-50",
        "NIFTY 50", "NIFTY-50-INDEX",
    ),
    "SENSEX": (
        "SENSEX", "BSESENSEX", "BSE_SENSEX", "SENSEX30",
        "SENSEX 30", "SENSEX-30",
    ),
    "BANKNIFTY": (
        "BANKNIFTY", "BANK_NIFTY", "BANK-NIFTY",
        "NIFTYBANK", "NIFTY_BANK", "BANK NIFTY",
        "NIFTY BANK",
    ),
    "FINNIFTY": (
        "FINNIFTY", "FIN_NIFTY", "FIN-NIFTY",
        "NIFTYFIN", "NIFTY_FIN", "FIN NIFTY",
    ),
    "MIDCPNIFTY": (
        "MIDCPNIFTY", "MIDCP_NIFTY", "MIDCAPNIFTY",
        "MIDCAP_NIFTY", "MIDCP NIFTY",
        "NIFTY MID SELECT", "NIFTY_MID_SELECT", "NIFTY-MID-SELECT",
        "NIFTYMIDSELECT", "NIFTY MIDCAP SELECT",
    ),
}


STRIKE_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "CE": ("CE", "CALL", "C", "CALL OPTION"),
    "PE": ("PE", "PUT", "P", "PUT OPTION"),
    "FUT": ("FUT", "FUTURES", "FUTURE", "F"),
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
_DROP_CHARS = {" ", "\t", "\n", "\r", "_", "-", "."}


def _strip(value: Any) -> str:
    """Uppercase and drop whitespace / separator characters."""
    if value is None:
        return ""
    return "".join(
        ch.upper() for ch in str(value) if ch not in _DROP_CHARS
    )


def _build_alias_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for canonical, variants in UNDERLYING_ALIASES.items():
        mapping[canonical] = canonical
        for v in variants:
            mapping[_strip(v)] = canonical
    return mapping


_ALIAS_TO_CANONICAL: dict[str, str] = _build_alias_map()


def _build_type_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for canonical, variants in STRIKE_TYPE_ALIASES.items():
        mapping[canonical] = canonical
        for v in variants:
            mapping[_strip(v)] = canonical
    return mapping


_TYPE_TO_CANONICAL: dict[str, str] = _build_type_map()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def normalize_underlying(value: Any) -> str:
    """Return the canonical ticker for ``value``.

    Empty input -> "". Falls back to the stripped uppercase input
    when no alias matches, so custom tickers still work.
    """
    s = _strip(value)
    if not s:
        return ""
    return _ALIAS_TO_CANONICAL.get(s, s)


def normalize_strike_type(value: Any) -> str:
    """Return "CE", "PE", or "FUT" (or stripped input on miss)."""
    s = _strip(value)
    if not s:
        return ""
    return _TYPE_TO_CANONICAL.get(s, s)


def normalize_strike(value: Any) -> str:
    """Return a plain integer string for a strike.

    Accepts numeric strings, floats, ints, and comma-separated text.
    Non-numeric input is returned with separators stripped so it can
    still be used as a registry key.
    """
    if value is None or value == "":
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    cleaned = raw.replace(",", "")
    try:
        f = float(cleaned)
    except (TypeError, ValueError):
        return _strip(raw)
    if f.is_integer():
        return str(int(f))
    return str(f)


def canonical_ticker(value: Any) -> str:
    """Alias of :func:`normalize_underlying` for readability."""
    return normalize_underlying(value)


def normalize_selector(
    *,
    instrument_key: Any = None,
    underlying: Any = None,
    strike: Any = None,
    strike_type: Any = None,
) -> dict[str, str | None]:
    """Normalize a four-part selector into a single dict.

    Missing values come back as ``None`` (not empty strings) so they
    can be compared directly against query parameters.
    """
    key = str(instrument_key).strip() if instrument_key else None
    return {
        "instrument_key": key,
        "underlying": normalize_underlying(underlying) or None,
        "strike": normalize_strike(strike) or None,
        "type": normalize_strike_type(strike_type) or None,
    }