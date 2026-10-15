"""Registry of live instruments loaded from the extracted backup tree
or registered dynamically.

The registry maintains three parallel views of every instrument:

    _by_key      : instrument_key -> entry
    _by_triple   : (UNDERLYING, "STRIKE", "TYPE") -> entry
    _ordered     : insertion-ordered list of entries

Both ``load()`` (scanning ``data/**/ema_crosses.json``) and
``register_adhoc()`` populate the triple index. The normalized
underlying / strike_type used for the triple come from
``services.normalization`` so that user inputs like "nifty", "NiFty",
"NIFTY 50", "ce", "call" all resolve through the same map the
``/live/*`` routes use.

``register_adhoc`` accepts optional ``underlying`` / ``strike_price``
/ ``strike_type`` / ``expiry`` kwargs. When they are provided (or
inferred from the key) the entry is indexed in ``_by_triple`` so the
triple form of the ``/live/*`` routes can find it. When they are
missing (e.g. a raw chart URL key with no metadata), the entry is
only reachable via ``get(key)`` — that is intentional.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable

from core import config
from services.normalization import (
    normalize_strike,
    normalize_strike_type,
    normalize_underlying,
)

logger = logging.getLogger(__name__)

_INDEX_PREFIXES = ("NSE_INDEX|", "BSE_INDEX|")


# --------------------------------------------------------------------------- #
# Backwards-compatible aliases                                               #
# --------------------------------------------------------------------------- #
# These names are used internally; they now delegate to the shared
# normalization module so there is a single source of truth.
def _norm_type(value: Any) -> str:
    return normalize_strike_type(value)


def _norm_underlying(value: Any) -> str:
    return normalize_underlying(value)


def _norm_strike(value: Any) -> str:
    return normalize_strike(value)


# --------------------------------------------------------------------------- #
# Registry                                                                   #
# --------------------------------------------------------------------------- #
class InstrumentRegistry:
    def __init__(self) -> None:
        self._by_key: dict[str, dict[str, Any]] = {}
        self._by_triple: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._ordered: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # Loading                                                            #
    # ------------------------------------------------------------------ #
    def load(self, data_dir: Path | None = None) -> dict[str, Any]:
        """Rebuild the registry by scanning ``data/**/ema_crosses.json``.

        The scan picks up:

        * backup-extracted option folders (with a full triple),
        * strategy-selection folders (with a full triple after Upstox
          resolution, or a ``STRATEGY|<TICKER>_<STRIKE>_<TYPE>``
          placeholder key before that),
        * any other folder that carries an ``ema_crosses.json`` with a
          non-empty ``instrument_key``.
        """
        root = Path(data_dir or config.LIVE_DATA_DIR)
        self._by_key.clear()
        self._by_triple.clear()
        self._ordered.clear()

        if not root.exists():
            logger.warning("Instrument registry: data dir %s missing", root)
            return {
                "count": 0,
                "triples": 0,
                "data_dir": str(root),
            }

        ema_files = list(root.rglob("ema_crosses.json"))
        for path in ema_files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                logger.exception("Registry: unreadable %s", path)
                continue

            key = str(payload.get("instrument_key") or "").strip()
            if not key:
                # Placeholder files written by the strategy service
                # before Upstox resolution has no key yet — skip
                # cleanly rather than registering an empty entry that
                # would pollute the triple map.
                continue

            underlying_raw = payload.get("underlying")
            strike_raw = payload.get("strike_price")
            type_raw = payload.get("strike_type")

            entry = {
                "instrument_key": key,
                "underlying": normalize_underlying(underlying_raw)
                or str(underlying_raw or "").strip(),
                "strike_price": strike_raw,
                "strike_type": normalize_strike_type(type_raw),
                "expiry": payload.get("expiry"),
                "trading_symbol": payload.get("trading_symbol"),
                "candles_path": str(path.with_name("candles.json")),
                "ema_path": str(path),
                "is_index": key.startswith(_INDEX_PREFIXES),
            }

            self._by_key[key] = entry
            self._ordered.append(entry)
            self._index_triple(entry)

        logger.info(
            "Instrument registry loaded: %d instruments (%d triples) from %s",
            len(self._ordered),
            len(self._by_triple),
            root,
        )
        return {
            "count": len(self._ordered),
            "triples": len(self._by_triple),
            "data_dir": str(root),
        }

    # ------------------------------------------------------------------ #
    # Dynamic / Ad-hoc Registration                                      #
    # ------------------------------------------------------------------ #
    def register_adhoc(
        self,
        instrument_key: str,
        trading_symbol: str | None = None,
        *,
        underlying: str | None = None,
        strike_price: Any | None = None,
        strike_type: str | None = None,
        expiry: Any | None = None,
    ) -> dict[str, Any]:
        """Register an instrument that is not present in the backup.

        When ``underlying`` / ``strike_price`` / ``strike_type`` are all
        provided (directly or through a later call), the entry is also
        indexed in ``_by_triple`` so ``resolve(underlying, strike,
        type)`` works — this is what the ``/live/*`` routes use for the
        triple selector.

        Idempotent: calling it again for an existing key backfills any
        missing triple fields on the existing entry and re-indexes it.
        """
        key = str(instrument_key or "").strip()
        if not key:
            raise ValueError("instrument_key is required")

        # ---- 1. Merge into an existing entry if one is present -------
        existing = self._by_key.get(key)
        if existing is not None:
            changed = False
            if underlying is not None and not existing.get("underlying"):
                existing["underlying"] = normalize_underlying(underlying)
                changed = True
            if (
                strike_price is not None
                and existing.get("strike_price") in (None, "")
            ):
                existing["strike_price"] = strike_price
                changed = True
            if strike_type is not None and not existing.get("strike_type"):
                existing["strike_type"] = normalize_strike_type(strike_type)
                changed = True
            if expiry is not None and not existing.get("expiry"):
                existing["expiry"] = expiry
                changed = True
            if trading_symbol and not existing.get("trading_symbol"):
                existing["trading_symbol"] = trading_symbol
                changed = True

            if changed or (
                existing.get("underlying")
                and existing.get("strike_price") not in (None, "")
                and existing.get("strike_type")
            ):
                self._index_triple(existing)
            return existing

        # ---- 2. Build a fresh entry ----------------------------------
        is_idx = key.startswith(_INDEX_PREFIXES)
        symbol = trading_symbol or key.split("|", 1)[-1]

        if underlying:
            u = normalize_underlying(underlying)
        elif is_idx:
            u = symbol
        else:
            u = symbol.split()[0] if symbol else ""

        # Best-effort inference from the symbol when the caller did
        # not pass the triple explicitly.
        inferred_type = normalize_strike_type(strike_type)
        inferred_strike = strike_price
        if not inferred_type or inferred_strike in (None, ""):
            parts = str(symbol).split()
            if len(parts) >= 2:
                if not inferred_strike:
                    try:
                        inferred_strike = int(float(parts[-2]))
                    except (TypeError, ValueError):
                        pass
                if not inferred_type:
                    inferred_type = normalize_strike_type(parts[-1])

        entry = {
            "instrument_key": key,
            "underlying": u or "",
            "strike_price": inferred_strike,
            "strike_type": inferred_type,
            "expiry": expiry,
            "trading_symbol": symbol,
            "candles_path": "",
            "ema_path": "",
            "is_index": is_idx,
        }
        self._by_key[key] = entry
        self._ordered.append(entry)
        self._index_triple(entry)
        return entry

    # ------------------------------------------------------------------ #
    # Triple indexing helper                                             #
    # ------------------------------------------------------------------ #
    def _index_triple(self, entry: dict[str, Any]) -> None:
        """Index an entry in ``_by_triple`` when all three parts exist.

        Entries without a complete triple are still valid for
        ``get(key)`` lookups, but they are not resolvable through the
        triple form — so we skip them here. When two entries share the
        same triple (e.g. a strategy placeholder replaced by the real
        Upstox key), the most recently indexed one wins; callers should
        register the richer entry last, which the strategy service
        does.
        """
        u = normalize_underlying(entry.get("underlying"))
        s = normalize_strike(entry.get("strike_price"))
        t = normalize_strike_type(entry.get("strike_type"))
        if u and s and t:
            self._by_triple[(u, s, t)] = entry

    # ------------------------------------------------------------------ #
    # Lookups                                                            #
    # ------------------------------------------------------------------ #
    def get(self, instrument_key: str) -> dict[str, Any] | None:
        return self._by_key.get(str(instrument_key or "").strip())

    def resolve(
        self,
        underlying: str,
        strike: Any = None,
        strike_type: str | None = None,
    ) -> dict[str, Any] | None:
        u = normalize_underlying(underlying)
        s = normalize_strike(strike)
        t = normalize_strike_type(strike_type)
        if not (u and s and t):
            return None

        entry = self._by_triple.get((u, s, t))
        if entry is not None:
            return entry

        # Alias-aware fallback. If the caller typed "NIFTY" but the
        # index is registered under "NIFTY 50" (or vice versa), try a
        # small set of alternates before giving up.
        alternates = {
            f"{u}50",
            f"{u} 50",
            u.split(" ", 1)[0],
            u.replace("50", ""),
        }
        for alt in alternates:
            alt_norm = normalize_underlying(alt)
            if not alt_norm or alt_norm == u:
                continue
            entry = self._by_triple.get((alt_norm, s, t))
            if entry is not None:
                return entry
        return None

    def all(self) -> list[dict[str, Any]]:
        return list(self._ordered)

    def subscribe_keys(self) -> list[str]:
        return [e["instrument_key"] for e in self._ordered]

    def query(
        self,
        underlying: str | None = None,
        strike_type: str | None = None,
        is_index: bool | None = None,
        instrument_keys: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        keys = (
            {str(k).strip() for k in instrument_keys}
            if instrument_keys is not None
            else None
        )
        u = normalize_underlying(underlying) if underlying else None
        t = normalize_strike_type(strike_type) if strike_type else None

        out: list[dict[str, Any]] = []
        for e in self._ordered:
            if keys is not None and e["instrument_key"] not in keys:
                continue
            if u and normalize_underlying(e.get("underlying")) != u:
                continue
            if t and normalize_strike_type(e.get("strike_type")) != t:
                continue
            if is_index is not None and bool(e.get("is_index")) != is_index:
                continue
            out.append(e)
        return out


instrument_registry = InstrumentRegistry()