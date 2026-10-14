"""Registry of live instruments loaded from the extracted backup tree.

The backup tree layout is::

    data/index/<underlying>/candles.json
    data/index/<underlying>/ema_crosses.json
    data/<underlying>/<strike>_<TYPE>/candles.json
    data/<underlying>/<strike>_<TYPE>/ema_crosses.json

Each `ema_crosses.json` payload carries `instrument_key`, `underlying`,
`strike_price`, `strike_type`, `expiry` and `trading_symbol`. We use
that as the source of truth so no extra map file is required.

Public lookups:

    get(instrument_key)                -> dict | None
    resolve(underlying, strike, type)  -> dict | None
    all()                              -> list[dict]
    subscribe_keys()                   -> list[str]
    query(underlying=None, ...)        -> list[dict]
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable

from core import config

logger = logging.getLogger(__name__)

_INDEX_PREFIXES = ("NSE_INDEX|", "BSE_INDEX|")


def _norm_type(value: Any) -> str:
    return str(value or "").strip().upper()


def _norm_underlying(value: Any) -> str:
    return str(value or "").strip().upper().replace(" ", "")


class InstrumentRegistry:
    def __init__(self) -> None:
        self._by_key: dict[str, dict[str, Any]] = {}
        self._by_triple: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._ordered: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # Loading                                                            #
    # ------------------------------------------------------------------ #
    def load(self, data_dir: Path | None = None) -> dict[str, Any]:
        root = Path(data_dir or config.LIVE_DATA_DIR)
        self._by_key.clear()
        self._by_triple.clear()
        self._ordered.clear()

        if not root.exists():
            logger.warning("Instrument registry: data dir %s missing", root)
            return {"count": 0, "data_dir": str(root)}

        ema_files = list(root.rglob("ema_crosses.json"))
        for path in ema_files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                logger.exception("Registry: unreadable %s", path)
                continue

            key = str(payload.get("instrument_key") or "").strip()
            if not key:
                continue

            entry = {
                "instrument_key": key,
                "underlying": str(payload.get("underlying") or "").strip(),
                "strike_price": payload.get("strike_price"),
                "strike_type": _norm_type(payload.get("strike_type")),
                "expiry": payload.get("expiry"),
                "trading_symbol": payload.get("trading_symbol"),
                "candles_path": str(path.with_name("candles.json")),
                "ema_path": str(path),
                "is_index": key.startswith(_INDEX_PREFIXES),
            }

            self._by_key[key] = entry
            self._ordered.append(entry)

            triple = (
                _norm_underlying(entry["underlying"]),
                str(entry["strike_price"] or ""),
                entry["strike_type"],
            )
            self._by_triple[triple] = entry

        logger.info(
            "Instrument registry loaded: %d instruments from %s",
            len(self._ordered),
            root,
        )
        return {"count": len(self._ordered), "data_dir": str(root)}

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
        u = _norm_underlying(underlying)
        strike_str = str(strike) if strike is not None else ""
        t = _norm_type(strike_type)
        return self._by_triple.get((u, strike_str, t))

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
        u = _norm_underlying(underlying) if underlying else None
        t = _norm_type(strike_type) if strike_type else None

        out: list[dict[str, Any]] = []
        for e in self._ordered:
            if keys is not None and e["instrument_key"] not in keys:
                continue
            if u and _norm_underlying(e["underlying"]) != u:
                continue
            if t and e["strike_type"] != t:
                continue
            if is_index is not None and bool(e["is_index"]) != is_index:
                continue
            out.append(e)
        return out


instrument_registry = InstrumentRegistry()