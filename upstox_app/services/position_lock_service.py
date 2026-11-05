# upstox_app/services/position_lock_service.py
"""Persistent lock registry for Upstox positions (MongoDB-backed).

A position is identified by its ``instrument_token`` — the same key
the Upstox portfolio endpoints use, e.g. ``NSE_FO|43919``.

Storage layout
--------------
Database:   ``MONGO_DB`` from ``core.config`` (default ``market_engine``)
Collection: ``ORDER_LEDGER_COLLECTION`` from ``core.config``
            (falls back to ``position_locks`` when unset)
Document:   single, ``_id = "position_locks"``

::

    {
      "_id": "position_locks",
      "count": 3,
      "data": [
        {
          "instrument_token": "NSE_FO|43919",
          "locked_at":        "2026-11-05T10:11:12.345678+00:00",
          "reason":           "hedge",                 # optional
          "note":             "hold through expiry",   # optional
          "source":           "dashboard",             # api|dashboard|cli|service
          "locked_by":        "session-user",          # optional
          "trading_symbol":   "NIFTY24NOV24000CE",     # from snapshot
          "quantity":         65,
          "side":             "LONG",
          "product":          "D",
          "average_price":    100.0,
          "last_price":       110.0
        },
        ...
      ],
      "created_at": "2026-11-05T09:00:00.000000+00:00",
      "updated_at": "2026-11-05T10:11:12.345678+00:00"
    }

``count`` is always kept in sync with ``len(data)``. It exists as a
field (rather than being derived only at read time) so MongoDB queries
and dashboards can reference it without loading ``data``.

Coexistence with the order ledger
---------------------------------
The lock registry is stored **inside** the ``ORDER_LEDGER_COLLECTION``
as a single document whose ``_id`` is the fixed string
``"position_locks"``. Every other document in that collection (order
rows, ledger entries, …) is untouched: all reads / CAS writes in this
module are scoped strictly by ``{"_id": "position_locks"}``.

Concurrency
-----------
Writes are serialised in-process via a single ``threading.RLock``,
then applied with a CAS (compare-and-swap) loop on ``updated_at``
so multiple worker processes cannot silently overwrite one another.
A CAS miss retries up to 5 times before returning an error.

Config (via core.config + env fallbacks, read at import)
---------------------------------------------------------
    MONGO_URI                   from core.config (MONGO_URL / MONGO_URI env),
                                then MONGODB_URI / UPSTOX_MONGO_URI,
                                default mongodb://localhost:27017
    MONGO_DB                    from core.config (MONGO_DB env),
                                then MONGO_DB_NAME / MONGODB_DB /
                                UPSTOX_MONGO_DB, default market_engine
    ORDER_LEDGER_COLLECTION     from core.config, default position_locks

Migration note
--------------
An earlier revision of this module used ``_id = "locked_positions"``.
If a deployment already has that document, copy it over once via::

    db.order_ledger.updateOne(
        {"_id": "position_locks"},
        {"$setOnInsert": db.order_ledger.findOne({"_id": "locked_positions"})},
        {"upsert": True}
    )

after which the legacy document can be deleted.

Fail-soft behaviour
-------------------
Every public helper returns a plain dict and never raises on MongoDB
errors. The read helpers report ``status: "error"`` with an empty
registry, and ``filter_positions`` fails **closed** — the caller
(``exit_all_positions``) checks for that and refuses to exit when the
lock registry is unreachable, so a transient Mongo outage cannot
cause the operator's protected positions to be liquidated.

CLI
---
    python -m upstox_app.services.position_lock_service list
    python -m upstox_app.services.position_lock_service lock NSE_FO|43919 --reason hedge
    python -m upstox_app.services.position_lock_service lock NSE_FO|43919 NSE_FO|43920
    python -m upstox_app.services.position_lock_service unlock NSE_FO|43919
    python -m upstox_app.services.position_lock_service clear
    python -m upstox_app.services.position_lock_service migrate --from-file data/locked_positions.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from pymongo import MongoClient
from pymongo.errors import PyMongoError

from core import config as core_config
from core.logger import get_logger

logger = get_logger(__file__)


# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #
# Sourced from core.config. The extra env fallbacks preserve backward
# compatibility for deployments that historically set MONGODB_URI /
# UPSTOX_MONGO_URI / MONGO_DB_NAME / MONGODB_DB / UPSTOX_MONGO_DB
# directly on this module.
MONGO_URI = (
    core_config.MONGO_URI
    or os.getenv("MONGODB_URI")
    or os.getenv("UPSTOX_MONGO_URI")
    or "mongodb://localhost:27017"
)
MONGO_DB_NAME = (
    core_config.MONGO_DB
    or os.getenv("MONGO_DB_NAME")
    or os.getenv("MONGODB_DB")
    or os.getenv("UPSTOX_MONGO_DB")
    or "market_engine"
)
# Primary source: ORDER_LEDGER_COLLECTION from core.config.
# Falls back to "position_locks" so a deployment that has not yet set
# ORDER_LEDGER_COLLECTION in .env keeps working with the previous
# collection name and does not silently start writing into an unrelated
# collection.
POSITION_LOCKS_COLLECTION = (
    core_config.ORDER_LEDGER_COLLECTION or "position_locks"
)

# The singleton document holding the lock registry. Stored inside
# ORDER_LEDGER_COLLECTION alongside order rows; the fixed string id
# means the lock service can never collide with an order document.
LOCK_DOC_ID = "position_locks"
_CAS_RETRIES = 5
_CAS_RETRY_SLEEP = 0.02  # seconds


# --------------------------------------------------------------------------- #
# Module state                                                                #
# --------------------------------------------------------------------------- #
_lock = threading.RLock()
_client: MongoClient | None = None
_collection = None


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_token(token: Any) -> str:
    return str(token or "").strip()


def _get_collection():
    """Return the collection handle, or ``None`` on failure."""
    global _client, _collection
    if _collection is not None:
        return _collection
    with _lock:
        if _collection is not None:
            return _collection
        try:
            _client = MongoClient(
                MONGO_URI,
                serverSelectionTimeoutMS=3000,
                connect=False,
            )
            _collection = _client[MONGO_DB_NAME][POSITION_LOCKS_COLLECTION]
            # Ensure the singleton doc exists so subsequent ``update_one``
            # calls never have to pay the upsert cost.
            _collection.update_one(
                {"_id": LOCK_DOC_ID},
                {
                    "$setOnInsert": {
                        "_id": LOCK_DOC_ID,
                        "count": 0,
                        "data": [],
                        "created_at": _now_iso(),
                        "updated_at": _now_iso(),
                    }
                },
                upsert=True,
            )
            logger.info(
                "[position-lock] connected to MongoDB "
                "db=%s collection=%s doc_id=%s",
                MONGO_DB_NAME, POSITION_LOCKS_COLLECTION, LOCK_DOC_ID,
            )
        except PyMongoError as exc:
            logger.exception(
                "[position-lock] MongoDB init failed: %s", exc,
            )
            _collection = None
            _client = None
        return _collection


def _read_doc() -> dict[str, Any]:
    """Return the current ``{count, data, updated_at}`` view.

    Returns ``{"status": "error", ...}`` on a Mongo failure. Callers
    check ``status`` before consuming ``data``.
    """
    coll = _get_collection()
    if coll is None:
        return {
            "status": "error",
            "error": "mongodb_unavailable",
            "count": 0,
            "data": [],
        }
    try:
        doc = coll.find_one({"_id": LOCK_DOC_ID}) or {}
    except PyMongoError as exc:
        logger.exception("[position-lock] MongoDB read failed")
        return {
            "status": "error",
            "error": f"mongodb_read_failed: {type(exc).__name__}",
            "count": 0,
            "data": [],
        }
    data = doc.get("data") or []
    if not isinstance(data, list):
        data = []
    return {
        "status": "ok",
        "count": len(data),
        "data": data,
        "updated_at": doc.get("updated_at"),
    }


def _mutate(
    fn: Callable[[list[dict[str, Any]]], tuple[list[dict[str, Any]], Any]],
) -> tuple[bool, Any, str | None]:
    """Apply ``fn`` to the current ``data`` array with CAS retry.

    ``fn`` receives the current list and returns ``(new_data, result)``.
    Returns ``(success, result, error)``. On success, ``new_data`` has
    been written and ``result`` is the caller's value. On failure,
    ``error`` is populated and ``result`` is unused.
    """
    coll = _get_collection()
    if coll is None:
        return False, None, "mongodb_unavailable"

    for _attempt in range(_CAS_RETRIES):
        try:
            doc = coll.find_one({"_id": LOCK_DOC_ID}) or {}
        except PyMongoError as exc:
            logger.exception("[position-lock] read failed during mutation")
            return (
                False,
                None,
                f"mongodb_read_failed: {type(exc).__name__}",
            )

        current = doc.get("data") or []
        if not isinstance(current, list):
            current = []
        prev_updated_at = doc.get("updated_at")

        try:
            new_data, result_value = fn(current)
        except Exception as exc:
            logger.exception("[position-lock] mutation function raised")
            return False, None, f"mutation_raised: {type(exc).__name__}"

        if new_data == current:
            # Nothing to write; caller's result is still valid.
            return True, result_value, None

        try:
            res = coll.update_one(
                {"_id": LOCK_DOC_ID, "updated_at": prev_updated_at},
                {
                    "$set": {
                        "data": new_data,
                        "count": len(new_data),
                        "updated_at": _now_iso(),
                    }
                },
            )
        except PyMongoError as exc:
            logger.exception(
                "[position-lock] write failed during mutation"
            )
            return (
                False,
                None,
                f"mongodb_write_failed: {type(exc).__name__}",
            )

        if res.modified_count == 1:
            return True, result_value, None

        # CAS miss (another writer beat us to it) — retry.
        time.sleep(_CAS_RETRY_SLEEP)

    logger.warning(
        "[position-lock] CAS retries exhausted after %d attempts",
        _CAS_RETRIES,
    )
    return False, None, "cas_retries_exhausted"


_SNAPSHOT_FIELDS = (
    "trading_symbol",
    "quantity",
    "side",
    "product",
    "average_price",
    "last_price",
    "exchange",
    "pnl",
)


def _build_entry(
    token: str,
    *,
    reason: str | None = None,
    note: str | None = None,
    source: str | None = None,
    locked_by: str | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "instrument_token": token,
        "locked_at": _now_iso(),
    }
    if reason:
        entry["reason"] = str(reason)
    if note:
        entry["note"] = str(note)
    entry["source"] = str(source or "service")
    if locked_by:
        entry["locked_by"] = str(locked_by)
    if isinstance(snapshot, dict):
        for field in _SNAPSHOT_FIELDS:
            if field in snapshot and snapshot[field] is not None:
                entry[field] = snapshot[field]
    return entry


# --------------------------------------------------------------------------- #
# Read helpers                                                                #
# --------------------------------------------------------------------------- #
def is_locked(instrument_token: str) -> bool:
    token = _normalise_token(instrument_token)
    if not token:
        return False
    doc = _read_doc()
    if doc["status"] != "ok":
        return False
    return any(
        e.get("instrument_token") == token
        for e in doc["data"]
        if isinstance(e, dict)
    )


def list_locked() -> dict[str, Any]:
    """Return ``{status, count, locked: {token: entry}}``.

    ``locked`` is a dict keyed by ``instrument_token`` — the same shape
    consumed by ``get_positions._annotate``.
    """
    doc = _read_doc()
    if doc["status"] != "ok":
        return {
            "status": "error",
            "count": 0,
            "locked": {},
            "error": doc.get("error"),
        }
    locked: dict[str, dict[str, Any]] = {}
    for entry in doc["data"]:
        if not isinstance(entry, dict):
            continue
        token = _normalise_token(entry.get("instrument_token"))
        if token:
            locked[token] = dict(entry)
    return {
        "status": "ok",
        "count": len(locked),
        "locked": locked,
        "updated_at": doc.get("updated_at"),
    }


# --------------------------------------------------------------------------- #
# Mutation helpers                                                            #
# --------------------------------------------------------------------------- #
def lock(
    instrument_token: str,
    *,
    reason: str | None = None,
    note: str | None = None,
    source: str | None = None,
    locked_by: str | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Lock a single position. Idempotent — a second call reports
    ``already_locked=True`` and returns the existing entry."""
    token = _normalise_token(instrument_token)
    if not token:
        return {
            "status": "invalid_input",
            "error": "instrument_token is required",
        }

    existing_holder: dict[str, Any] = {}

    def _apply(current: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Any]:
        for e in current:
            if isinstance(e, dict) and e.get("instrument_token") == token:
                existing_holder["entry"] = dict(e)
                return current, {"already": True}
        new_entry = _build_entry(
            token,
            reason=reason,
            note=note,
            source=source,
            locked_by=locked_by,
            snapshot=snapshot,
        )
        existing_holder["entry"] = new_entry
        return current + [new_entry], {"already": False}

    ok, result, err = _mutate(_apply)
    if not ok:
        return {"status": "error", "error": err}

    entry = existing_holder.get("entry", {})
    if result.get("already"):
        return {
            "status": "ok",
            "already_locked": True,
            "instrument_token": token,
            "entry": entry,
        }

    logger.info(
        "[position-lock] locked %s (source=%s)",
        token, entry.get("source", "?"),
    )
    return {
        "status": "ok",
        "already_locked": False,
        "instrument_token": token,
        "entry": entry,
    }


def unlock(instrument_token: str) -> dict[str, Any]:
    token = _normalise_token(instrument_token)
    if not token:
        return {
            "status": "invalid_input",
            "error": "instrument_token is required",
        }

    removed_holder: dict[str, Any] = {}

    def _apply(current: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Any]:
        kept: list[dict[str, Any]] = []
        for e in current:
            if isinstance(e, dict) and e.get("instrument_token") == token:
                removed_holder["entry"] = dict(e)
                continue
            kept.append(e)
        return kept, {"was_locked": "entry" in removed_holder}

    ok, result, err = _mutate(_apply)
    if not ok:
        return {"status": "error", "error": err}

    was_locked = bool(result.get("was_locked"))
    if was_locked:
        logger.info("[position-lock] unlocked %s", token)
        return {
            "status": "ok",
            "was_locked": True,
            "instrument_token": token,
            "entry": removed_holder.get("entry", {}),
        }
    return {
        "status": "ok",
        "was_locked": False,
        "instrument_token": token,
    }


def lock_many(
    instrument_tokens: Iterable[str],
    *,
    reason: str | None = None,
    note: str | None = None,
    source: str | None = None,
    locked_by: str | None = None,
) -> dict[str, Any]:
    tokens = [_normalise_token(t) for t in instrument_tokens]
    tokens = [t for t in tokens if t]
    if not tokens:
        return {"status": "invalid_input", "error": "no tokens provided"}

    added_holder: list[str] = []
    already_holder: list[str] = []

    def _apply(current: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Any]:
        existing = {
            e.get("instrument_token")
            for e in current
            if isinstance(e, dict) and e.get("instrument_token")
        }
        new_entries: list[dict[str, Any]] = []
        for t in tokens:
            if t in existing:
                already_holder.append(t)
                continue
            new_entries.append(
                _build_entry(
                    t,
                    reason=reason,
                    note=note,
                    source=source,
                    locked_by=locked_by,
                )
            )
            added_holder.append(t)
        return current + new_entries, None

    ok, _, err = _mutate(_apply)
    if not ok:
        return {"status": "error", "error": err}

    logger.info(
        "[position-lock] lock_many locked=%d already=%d",
        len(added_holder), len(already_holder),
    )
    return {
        "status": "ok",
        "locked": list(added_holder),
        "already_locked": list(already_holder),
    }


def unlock_many(instrument_tokens: Iterable[str]) -> dict[str, Any]:
    tokens = {_normalise_token(t) for t in instrument_tokens}
    tokens.discard("")
    if not tokens:
        return {"status": "invalid_input", "error": "no tokens provided"}

    removed_holder: list[str] = []
    not_locked_holder: list[str] = []

    def _apply(current: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Any]:
        kept: list[dict[str, Any]] = []
        for e in current:
            if not isinstance(e, dict):
                kept.append(e)
                continue
            tok = e.get("instrument_token")
            if tok in tokens:
                removed_holder.append(tok)
                continue
            kept.append(e)
        for t in tokens:
            if t not in removed_holder:
                not_locked_holder.append(t)
        return kept, None

    ok, _, err = _mutate(_apply)
    if not ok:
        return {"status": "error", "error": err}

    logger.info(
        "[position-lock] unlock_many removed=%d not_locked=%d",
        len(removed_holder), len(not_locked_holder),
    )
    return {
        "status": "ok",
        "removed": list(removed_holder),
        "not_locked": list(not_locked_holder),
    }


def clear_all() -> dict[str, Any]:
    cleared_holder: dict[str, int] = {"count": 0}

    def _apply(current: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Any]:
        cleared_holder["count"] = len(current)
        return [], None

    ok, _, err = _mutate(_apply)
    if not ok:
        return {"status": "error", "error": err}

    logger.info("[position-lock] cleared %d lock(s)", cleared_holder["count"])
    return {"status": "ok", "cleared": cleared_holder["count"]}


# --------------------------------------------------------------------------- #
# Filtering                                                                   #
# --------------------------------------------------------------------------- #
def filter_positions(
    positions: Iterable[dict[str, Any]],
    *,
    include_locked: bool = False,
) -> dict[str, Any]:
    """Split positions into ``to_exit`` and ``skipped_locked`` buckets.

    On MongoDB failure this returns ``status="error"`` with **empty**
    ``to_exit``. Callers must treat that as "refuse to exit" — the
    lock registry is the only authority on what must be preserved, so
    proceeding on a read failure could liquidate protected positions.
    ``exit_all_positions`` enforces this.
    """
    doc = _read_doc()
    if doc["status"] != "ok":
        logger.error(
            "[position-lock] filter_positions — registry unavailable: %s",
            doc.get("error"),
        )
        return {
            "status": "error",
            "error": doc.get("error") or "mongodb_unavailable",
            "to_exit": [],
            "skipped_locked": [],
            "locked_count": 0,
            "unlocked_count": 0,
        }

    locked_tokens = {
        e.get("instrument_token")
        for e in doc["data"]
        if isinstance(e, dict) and e.get("instrument_token")
    }

    to_exit: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        token = _normalise_token(pos.get("instrument_token"))
        if not token:
            continue
        if token in locked_tokens and not include_locked:
            skipped.append(dict(pos))
        else:
            to_exit.append(dict(pos))

    return {
        "status": "ok",
        "to_exit": to_exit,
        "skipped_locked": skipped,
        "locked_count": len(skipped),
        "unlocked_count": len(to_exit),
    }


# --------------------------------------------------------------------------- #
# Legacy JSON migration                                                       #
# --------------------------------------------------------------------------- #
def migrate_from_file(path: str) -> dict[str, Any]:
    """One-shot import of a legacy JSON lock file into MongoDB.

    Idempotent on ``instrument_token`` — tokens already present in
    the DB are skipped.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {"status": "error", "error": "file_not_found", "path": path}
    except Exception as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(raw, dict):
        return {"status": "error", "error": "file_not_dict"}

    incoming: list[dict[str, Any]] = []
    for token_raw, meta in raw.items():
        token = _normalise_token(token_raw)
        if not token:
            continue
        entry: dict[str, Any] = {
            "instrument_token": token,
            "locked_at": _now_iso(),
        }
        if isinstance(meta, dict):
            for k in ("locked_at", "reason", "note", "source", "locked_by"):
                if meta.get(k):
                    entry[k] = meta[k]
        else:
            entry["note"] = str(meta)
        entry.setdefault("source", "migration")
        incoming.append(entry)

    added_holder: list[str] = []
    skipped_holder: list[str] = []

    def _apply(current: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Any]:
        existing = {
            e.get("instrument_token")
            for e in current
            if isinstance(e, dict) and e.get("instrument_token")
        }
        to_add: list[dict[str, Any]] = []
        for entry in incoming:
            tok = entry["instrument_token"]
            if tok in existing:
                skipped_holder.append(tok)
                continue
            to_add.append(entry)
            added_holder.append(tok)
        return current + to_add, None

    ok, _, err = _mutate(_apply)
    if not ok:
        return {"status": "error", "error": err}

    logger.info(
        "[position-lock] migrate imported=%d already_locked=%d",
        len(added_holder), len(skipped_holder),
    )
    return {
        "status": "ok",
        "imported": len(added_holder),
        "already_locked": len(skipped_holder),
        "total": len(added_holder) + len(skipped_holder),
    }


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Lock / unlock Upstox positions (MongoDB-backed).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_lock = sub.add_parser("lock", help="Lock one or more positions.")
    p_lock.add_argument("tokens", nargs="+", help="instrument_token(s)")
    p_lock.add_argument("--reason", default=None)
    p_lock.add_argument("--note", default=None)

    p_unlock = sub.add_parser("unlock", help="Unlock one or more positions.")
    p_unlock.add_argument("tokens", nargs="+")

    sub.add_parser("list", help="List locked positions.")
    sub.add_parser("clear", help="Clear every lock.")

    p_mig = sub.add_parser("migrate", help="Import a legacy JSON lock file.")
    p_mig.add_argument(
        "--from-file", default="data/locked_positions.json",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.cmd == "lock":
        if len(args.tokens) == 1:
            result = lock(
                args.tokens[0], reason=args.reason, note=args.note,
                source="cli",
            )
        else:
            result = lock_many(
                args.tokens, reason=args.reason, note=args.note,
                source="cli",
            )
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") == "ok" else 1
    if args.cmd == "unlock":
        if len(args.tokens) == 1:
            result = unlock(args.tokens[0])
        else:
            result = unlock_many(args.tokens)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") == "ok" else 1
    if args.cmd == "list":
        print(json.dumps(list_locked(), indent=2, default=str))
        return 0
    if args.cmd == "clear":
        print(json.dumps(clear_all(), indent=2, default=str))
        return 0
    if args.cmd == "migrate":
        result = migrate_from_file(args.from_file)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") == "ok" else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())