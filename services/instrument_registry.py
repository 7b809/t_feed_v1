# File: services/instrument_registry.py
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

Path auto-population
--------------------
Every entry produced by this registry — whether loaded from disk by
``load()`` or created by ``register_adhoc()`` — carries a **valid,
non-empty** ``ema_path`` and ``candles_path``.

For entries discovered by ``load()``, both paths point at the real
files in the extracted backup tree (``.../ema_crosses.json`` and
``.../candles.json`` next to it).

For entries created by ``register_adhoc()``, both paths point at the
canonical live-data layout::

    <LIVE_DATA_DIR>/index/<safe_instrument_key>/ema_crosses.json
    <LIVE_DATA_DIR>/index/<safe_instrument_key>/candles.json

The ``_safe_folder`` rule applied to the instrument_key is identical
to ``routes.admin_routes._safe_folder`` and to the fallback in
``services.live_cross_store.LiveCrossStore._path_for``, so the three
code paths produce byte-identical paths for the same instrument.

Why this matters
----------------
Before this revision ``register_adhoc`` deliberately left both fields
as empty strings::

    entry = {..., "candles_path": "", "ema_path": "", ...}

Downstream callers do::

    path = Path(entry["ema_path"]).with_name(config.LIVE_CROSSES_FILENAME)

``Path("")`` is ``PosixPath('.')``, whose ``.name`` is ``''``, and
``.with_name(...)`` on such a path raises::

    ValueError: PosixPath('.') has an empty name

This is exactly the failure logged at 2026-10-21 12:57:01 for the
ad-hoc ``BSE_FO|1124739``-style entries — every live EMA cross for
those instruments was silently dropped because ``live_cross_store.save``
blew up before it could write anything.

Filling the paths here closes the bug at the source. The defensive
guards in ``routes.admin_routes._ensure_ema_path`` and in
``live_cross_store._path_for`` remain in place as second / third
lines of defence, but they should never fire now.

``load()`` still returns the real absolute ``data_dir`` in its status
dict. Route handlers that expose that dict to the browser are
expected to pass it through the redaction helper (see
``routes.admin_routes._sanitize_payload``) before returning — the
registry itself keeps the real path so internal callers can use it.
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
# Canonical path helpers                                                     #
# --------------------------------------------------------------------------- #
def _safe_folder(name: Any) -> str:
    """Filesystem-safe folder slug for an instrument_key.

    Mirrors ``routes.admin_routes._safe_folder`` (and the matching
    helper in ``services.live_cross_store``) so the canonical paths
    produced here are byte-identical to the ones those modules derive
    from the same instrument_key.

    Rules:
      * strip surrounding whitespace
      * replace path separators, ``:``, ``*``, ``?``, ``"``, ``<``,
        ``>``, and spaces with ``_`` (defensive — modern Linux distros
        allow all of these, but a URL-encoded pipe in a filename
        invites trouble)
      * strip leading/trailing ``.`` and ``_``
      * fall back to ``"unknown"`` when the result is empty
    """
    text = str(name or "unknown").strip()
    for ch in ("|", "/", "\\", ":", "*", "?", '"', "<", ">", " "):
        text = text.replace(ch, "_")
    return text.strip("._") or "unknown"


def _canonical_paths(instrument_key: str) -> tuple[str, str]:
    """Return ``(ema_path, candles_path)`` for an ad-hoc instrument.

    Uses the same directory layout as
    ``routes.admin_routes._ensure_core_and_strategy_indexes_registered``::

        <LIVE_DATA_DIR>/index/<safe_folder(instrument_key)>/ema_crosses.json
        <LIVE_DATA_DIR>/index/<safe_folder(instrument_key)>/candles.json
    """
    base = Path(config.LIVE_DATA_DIR) / "index" / _safe_folder(instrument_key)
    return (str(base / "ema_crosses.json"), str(base / "candles.json"))


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

        Both ``ema_path`` and ``candles_path`` are derived from the
        discovered file path itself, so they always point at the real
        files in the extracted backup tree — not at the canonical
        fallback layout.

        The returned status dict carries the **real** absolute
        ``data_dir``. Route handlers that expose it to the browser
        must sanitise it (see ``routes.admin_routes._sanitize_payload``).
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
                # Real backup-tree paths — never overwrite these with
                # the canonical fallback; that would point readers at
                # an empty file instead of the actual backup data.
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
        It also backfills ``ema_path`` / ``candles_path`` when the
        existing entry carries empty values (e.g. an entry created
        earlier in the process before the path auto-population was
        added).

        Fresh entries **always** carry a valid ``ema_path`` and
        ``candles_path`` pointing at the canonical layout under
        ``<LIVE_DATA_DIR>/index/<safe_folder(instrument_key)>/``. See
        the module docstring for the rationale.
        """
        key = str(instrument_key or "").strip()
        if not key:
            raise ValueError("instrument_key is required")

        # Canonical paths are used for new entries and as a backfill
        # for existing entries whose path fields are still empty.
        canonical_ema, canonical_candles = _canonical_paths(key)

        # ---- 1. Merge into an existing entry if one is present ------- #
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

            # Backfill the path fields. Only when the existing value is
            # empty — do NOT clobber paths that ``load()`` derived from
            # the real backup tree, since those point at actual data.
            if not existing.get("ema_path"):
                existing["ema_path"] = canonical_ema
                changed = True
            if not existing.get("candles_path"):
                existing["candles_path"] = canonical_candles
                changed = True

            if changed or (
                existing.get("underlying")
                and existing.get("strike_price") not in (None, "")
                and existing.get("strike_type")
            ):
                self._index_triple(existing)
            return existing

        # ---- 2. Build a fresh entry ---------------------------------- #
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
            # Both paths always populated so downstream callers can do
            # ``Path(entry["ema_path"]).with_name(...)`` without a
            # ``ValueError: PosixPath('.') has an empty name``.
            "candles_path": canonical_candles,
            "ema_path": canonical_ema,
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