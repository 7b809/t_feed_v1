# core/mongo_client.py
"""MongoDB client singleton (thread-safe).

Original code had a race: ``get_mongo_db()`` is called concurrently
from multiple scheduler threads on startup — the deferred-startup
one-shot, the recurring ``daily_refresh`` cron, ``pivot_check``,
``isolation_scan``, and the startup backfill — all within the same
second. The original ``if _db is None:`` check was unsynchronised, so
two or more threads could each see ``_db is None`` before either
assigned it. Visible symptoms in production logs:

  * ``Connected to MongoDB database: UPSTOX_APP_V2`` logged 2–3× in
    the same 09:18:00 window.
  * Multiple redundant ``pymongo.MongoClient`` instances were created
    (each opens its own connection pool and background monitor threads).
    Whichever thread won the final assignment of ``_db`` "wins", but
    the losing client is never closed and its threads leak until the
    process exits.

Fix
---
Double-checked locking with a module-level ``threading.Lock``:

  1. Fast path: ``if _db is not None: return _db`` — no lock on the
     hot path. Every call after the first pays only an atomic read
     of ``_db`` (CPython: a single bytecode ``LOAD_GLOBAL``, no GIL
     contention worth worrying about).
  2. Slow path: acquire ``_mongo_lock``, re-check ``_db is None``,
     then create the client + ping + assign. The re-check inside the
     lock is what makes the pattern safe: it guarantees only one
     thread ever enters the construction block.
  3. On construction failure, ``_client`` and ``_db`` are explicitly
     reset to ``None`` before re-raising, so a subsequent call can
     retry. Without this, a partial assignment (e.g. ``_client`` set,
     ``server_info()`` raised) would leave the module in a state where
     ``_client is not None`` but ``_db is None`` — the next caller
     would skip the ``try`` block via an outer guard we don't have
     and either crash or silently return ``None``. (The original code
     didn't have this bug because it assigned ``_client`` *inside*
     the ``if _db is None`` block, but it's still worth being
     explicit here.)

No behaviour change for callers. ``get_mongo_db()`` still:
  * returns the same ``pymongo.database.Database`` instance to every
    caller for the lifetime of the process,
  * forces a ``server_info()`` round-trip on first call so a broken
    Mongo connection fails loudly at startup rather than on the
    first real query,
  * raises on failure (unchanged).
"""

import threading

import pymongo
from pymongo.database import Database

from core.config import MONGO_URI, MONGO_DB_NAME
from core.logger import get_logger

logger = get_logger(__file__)

_client = None
_db = None

# Guards the construction block below. See module docstring for why
# this is required (multi-threaded startup race).
_mongo_lock = threading.Lock()


def get_mongo_db() -> Database:
    """Return a MongoDB database instance (singleton, thread-safe).

    Thread-safe double-checked-locking singleton:

      * First call (per process) constructs the ``MongoClient``,
        forces a ``server_info()`` ping, and caches the resulting
        ``Database`` handle in the module-level ``_db``.
      * Every subsequent call returns the cached handle without
        taking the lock.

    Raises the underlying ``pymongo`` exception if the initial
    connection / ping fails. The module state is reset so a later
    call can retry.
    """
    global _client, _db

    # Fast path — no lock required once the singleton is initialised.
    if _db is not None:
        return _db

    # Slow path — at most one thread ever reaches the construction
    # block. The second ``_db is None`` check inside the lock is what
    # makes this double-checked locking correct: a thread that was
    # waiting on the lock while another constructed the client will
    # observe ``_db is not None`` here and skip straight to the return.
    with _mongo_lock:
        if _db is None:
            try:
                _client = pymongo.MongoClient(
                    MONGO_URI,
                    serverSelectionTimeoutMS=5000,
                )
                _client.server_info()  # force connection check
                _db = _client[MONGO_DB_NAME]
                logger.info(
                    "Connected to MongoDB database: %s", MONGO_DB_NAME
                )
            except Exception:
                # Reset partial state so a later call can retry. Without
                # this, a failed ``server_info()`` would leave ``_client``
                # non-None (holding an open, broken connection pool) and
                # ``_db`` None — the next caller would try to rebuild
                # over the top of a leaked client.
                _client = None
                _db = None
                logger.exception("Failed to connect to MongoDB")
                raise

    return _db