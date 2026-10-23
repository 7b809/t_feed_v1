# app/services/instrument_store.py
"""
Reads and (optionally) writes ``storage/mega/all_instruments.json``.

Expected file shape (JSON object keyed by ``instrument_key``):

    {
      "NSE_INDEX|Nifty 50": {
        "instrument_key": "NSE_INDEX|Nifty 50",
        "underlying": "NIFTY",
        "strike_price": null,
        "strike_type": "INDEX",
        "expiry": null,
        "trading_symbol": "NIFTY"
      },
      "NSE_FO|54321": {
        "instrument_key": "NSE_FO|54321",
        "underlying": "NIFTY",
        "strike_price": 24300.0,
        "strike_type": "CE",
        "expiry": "2026-10-29",
        "trading_symbol": "NIFTY 24300 CE 29 OCT 26"
      },
      ...
    }

Design notes
------------
* Thread-safe: every public method acquires an internal ``RLock``. Required
  because ``EmaFeedService`` invokes lookups from ``asyncio.to_thread(...)``
  worker threads.
* Missing file: ``reload()`` does NOT raise when the file is absent. It logs a
  warning and keeps an empty catalog so that the FastAPI app can still boot on
  a fresh checkout. Callers that truly need an instrument use ``require()``
  and receive a clear ``InstrumentStoreError`` explaining the situation.
* Atomic writes: JSON is written to ``<file>.tmp`` and then ``os.replace``-d
  into place, so a crash mid-write cannot corrupt the on-disk file. ``upsert``
  and friends hold the lock during the write, so concurrent callers cannot
  interleave and lose each other's data.
* Identity: the outer key is authoritative. If an inner ``instrument_key``
  disagrees with the outer key, a warning is logged and the outer key wins.
* Job 2 (EMA) only *reads* this file. ``upsert`` / ``upsert_many`` / ``remove``
  are exposed for maintenance, tests, and future job wiring — they are not
  used by the normal subscription flow.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


class InstrumentStoreError(RuntimeError):
    """Raised when the instruments catalog cannot be read, written, or queried."""


class InstrumentStore:
    """Thread-safe accessor for ``all_instruments.json``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = {}
        self.reload()

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------
    def reload(self) -> None:
        """Re-read the on-disk file. Missing file → empty catalog + warning."""
        with self._lock:
            if not self.path.exists():
                self._data = {}
                logger.warning(
                    "all_instruments.json not found at %s — starting with empty catalog",
                    self.path,
                )
                return

            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise InstrumentStoreError(
                    f"Failed to read {self.path}: {exc}"
                ) from exc

            if not isinstance(raw, dict):
                raise InstrumentStoreError(
                    f"{self.path} must contain a JSON object keyed by instrument_key"
                )

            cleaned: dict[str, dict[str, Any]] = {}
            skipped = 0
            for key, value in raw.items():
                if not isinstance(key, str) or not key:
                    skipped += 1
                    continue
                if not isinstance(value, dict):
                    skipped += 1
                    continue
                record = dict(value)
                inner = record.get("instrument_key")
                if inner is not None and inner != key:
                    logger.warning(
                        "instrument_key mismatch for %s (inner=%r) — outer key wins",
                        key,
                        inner,
                    )
                record["instrument_key"] = key
                cleaned[key] = record

            self._data = cleaned
            logger.info(
                "Loaded %d instruments from %s (skipped=%d)",
                len(self._data),
                self.path,
                skipped,
            )

    def get(self, instrument_key: str) -> dict[str, Any] | None:
        """Return a defensive copy of the record, or ``None`` if absent."""
        with self._lock:
            record = self._data.get(instrument_key)
            return dict(record) if record is not None else None

    def require(self, instrument_key: str) -> dict[str, Any]:
        """Like :meth:`get`, but raises :class:`InstrumentStoreError` when absent."""
        record = self.get(instrument_key)
        if record is None:
            raise InstrumentStoreError(
                f"Instrument {instrument_key!r} is not present in {self.path}"
            )
        return record

    def has(self, instrument_key: str) -> bool:
        with self._lock:
            return instrument_key in self._data

    def all(self) -> dict[str, dict[str, Any]]:
        """Return a defensive copy of the whole catalog."""
        with self._lock:
            return {key: dict(value) for key, value in self._data.items()}

    def count(self) -> int:
        with self._lock:
            return len(self._data)

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._data.keys())

    # ------------------------------------------------------------------
    # Write API (maintenance / tests; Job 2 itself is read-only)
    # ------------------------------------------------------------------
    def upsert(self, instrument_key: str, record: dict[str, Any]) -> dict[str, Any]:
        """Insert or merge a record and persist atomically."""
        if not isinstance(instrument_key, str) or not instrument_key:
            raise InstrumentStoreError("instrument_key must be a non-empty string")
        if not isinstance(record, dict):
            raise InstrumentStoreError("record must be a JSON object")

        with self._lock:
            merged = dict(self._data.get(instrument_key, {}))
            merged.update(record)
            merged["instrument_key"] = instrument_key
            self._data[instrument_key] = merged
            self._write_atomic_locked()
            return dict(merged)

    def upsert_many(
        self,
        records: Iterable[tuple[str, dict[str, Any]]],
    ) -> int:
        """Bulk upsert; writes the file once if anything was added/updated."""
        written = 0
        with self._lock:
            for instrument_key, record in records:
                if not isinstance(instrument_key, str) or not instrument_key:
                    continue
                if not isinstance(record, dict):
                    continue
                merged = dict(self._data.get(instrument_key, {}))
                merged.update(record)
                merged["instrument_key"] = instrument_key
                self._data[instrument_key] = merged
                written += 1
            if written:
                self._write_atomic_locked()
        return written

    def remove(self, instrument_key: str) -> bool:
        """Delete a record and persist atomically. Returns ``True`` if removed."""
        with self._lock:
            if instrument_key not in self._data:
                return False
            del self._data[instrument_key]
            self._write_atomic_locked()
            return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _write_atomic_locked(self) -> None:
        """Caller must already hold ``self._lock``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(self._data, indent=2, ensure_ascii=False)
        try:
            tmp.write_text(payload, encoding="utf-8")
            # os.replace is atomic on both POSIX and Windows when source
            # and destination are on the same filesystem.
            tmp.replace(self.path)
        except OSError as exc:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise InstrumentStoreError(
                f"Failed to write {self.path}: {exc}"
            ) from exc