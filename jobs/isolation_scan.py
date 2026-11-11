# jobs/isolation_scan.py
"""Isolation scan tick + startup backfill helper.

Two jobs live here:

  * ``run_isolation_scan`` — every ``ISOLATION_SCAN_INTERVAL_SECONDS``
    (default 30s). Skips when outside the configured scan window, when
    the feed is not connected, or when today's isolation is already
    persisted.
  * ``run_startup_backfill`` — one-shot at process start (and on the
    deferred-startup cutoff). Walks today's intraday candles for a
    missed touch and reconstructs the isolation record if one exists.

Token handling
--------------
Both jobs need an Upstox access token, read from Mongo via
``core.token_service.fetch_access_token``. Because the scan tick fires
every 30 s, naive per-tick fetches produce ~120 ``Access token fetched``
lines per hour for what is a fast read of an already-connected Mongo
client. We cache the token for ``_TOKEN_CACHE_TTL_SECONDS`` seconds
(default **1800** = 30 minutes) to reduce that to ~2 fetches per hour.

The TTL is intentionally long:

  * Upstox access tokens are **daily** — they are set once in the
    morning by the upstream app and remain valid until end-of-session
    (or explicit rotation). Caching for 30 min is well inside that
    window.
  * The token is re-validated at least once per day by
    ``run_daily_refresh`` (which calls ``validate_access_token``
    directly, bypassing this cache).
  * If the upstream app rotates the token mid-day, the stale token
    will cause a visible ``Upstox API status=error: ...`` line on the
    first search after rotation; the next fetch (≤30 min later, or
    immediately after a process restart) picks up the new token.

Override the TTL via ``TOKEN_CACHE_TTL_SECONDS`` in ``.env`` if needed
— e.g. lower it to 300 during a debugging window where you expect
frequent token rotations. Setting it to ``0`` disables caching entirely
(every call fetches fresh).

Backfill outcome
----------------
``IsolationBackfillService.backfill_if_needed()`` can return ``None``
for two very different reasons:

  1. No qualifying touch was found in today's candles → nothing to
     reconstruct. Normal, expected.
  2. A qualifying touch WAS found but reconstruction failed (search
     returned nothing, option candle fetch failed, NIFTY close
     unavailable at that minute, …). Isolation for today is MISSING
     — this is a real failure and should be loud.

The backfill service now exposes ``last_touch_found`` /
``last_touch_minute`` / ``last_error`` attributes to distinguish these.
``run_startup_backfill`` below logs an ERROR for case 2 — which fires
correctly for the 10:19:28 ``BANKNIFTY/R3 @ 09:37 search failed`` case
that the original code misreported as ``nothing to reconstruct``.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from core.config import (
    TIMEZONE,
    ISOLATION_SCAN_START_HOUR,
    ISOLATION_SCAN_START_MINUTE,
    ISOLATION_SCAN_END_HOUR,
    ISOLATION_SCAN_END_MINUTE,
)
from core.logger import get_logger
from core.token_service import fetch_access_token
from jobs.daily_refresh import get_feed_service
from services.isolation_service import IsolationService
from services.isolation_backfill_service import IsolationBackfillService
from core import config
logger = get_logger(__file__)

# How long a fetched token is reused before re-reading from Mongo.
# See module docstring ("Token handling") for the rationale. Override
# via ``TOKEN_CACHE_TTL_SECONDS`` in ``.env`` if a shorter/longer
# window is needed. Setting to 0 disables caching (fetches every call).
_TOKEN_CACHE_TTL_SECONDS = int(
    config.TOKEN_CACHE_TTL_SECONDS,1800
)

# (token, fetched_at_epoch). Cleared implicitly on process restart.
# Tuple assignment is atomic in CPython, so no lock is required for the
# read/write race between the scan tick and a startup backfill running
# concurrently.
_token_cache: tuple[str, float] | None = None

_isolation_service: IsolationService | None = None
_isolation_token: str | None = None


def _get_token() -> str:
    """Return a token, cached for up to ``_TOKEN_CACHE_TTL_SECONDS``.

    Raises the underlying exception from ``fetch_access_token`` on a
    cache miss + fetch failure. Callers must handle this (both
    ``run_isolation_scan`` and ``run_startup_backfill`` already do).
    """
    global _token_cache
    now = time.time()
    if _token_cache is not None and _TOKEN_CACHE_TTL_SECONDS > 0:
        cached_token, fetched_at = _token_cache
        if now - fetched_at < _TOKEN_CACHE_TTL_SECONDS:
            return cached_token
    token = fetch_access_token()
    _token_cache = (token, now)
    return token


def get_isolation_service(token: str) -> IsolationService:
    global _isolation_service, _isolation_token
    if _isolation_service is None or _isolation_token != token:
        _isolation_service = IsolationService(token)
        _isolation_token = token
    return _isolation_service


def _within_scan_window(now: datetime) -> bool:
    if now.weekday() > 4:
        return False
    start = now.replace(
        hour=ISOLATION_SCAN_START_HOUR,
        minute=ISOLATION_SCAN_START_MINUTE,
        second=0, microsecond=0,
    )
    end = now.replace(
        hour=ISOLATION_SCAN_END_HOUR,
        minute=ISOLATION_SCAN_END_MINUTE,
        second=0, microsecond=0,
    )
    return start <= now <= end


def run_isolation_scan() -> None:
    """Scheduler tick: every ISOLATION_SCAN_INTERVAL_SECONDS."""
    now = datetime.now(ZoneInfo(TIMEZONE))
    if not _within_scan_window(now):
        return

    feed = get_feed_service()
    if not feed:
        logger.debug("isolation scan skipped: feed not connected")
        return

    try:
        token = _get_token()
    except Exception:
        logger.exception("isolation scan skipped: token fetch failed")
        return

    try:
        service = get_isolation_service(token)
        record = service.scan_once(feed)
        if record:
            logger.info(
                "isolation created (live): %s (mode=%s)",
                record["trading_symbol"],
                record["standard_execution_mode"],
            )
    except Exception:
        logger.exception("isolation scan tick failed")


def run_startup_backfill() -> None:
    """One-shot at startup: reconstruct missed isolation, if any.

    See module docstring ("Backfill outcome") for the two return-``None``
    cases and why they must be logged differently.
    """
    try:
        token = _get_token()
    except Exception:
        logger.exception("Backfill skipped: token fetch failed")
        return

    try:
        service = get_isolation_service(token)
        backfill = IsolationBackfillService(token, service)
        record = backfill.backfill_if_needed()

        if record:
            logger.warning(
                "STARTUP BACKFILL: isolation reconstructed — %s | %s/%s "
                "| mode=%s | backfilled_at=%s",
                record["trading_symbol"],
                record["trigger_index_canonical"],
                record["trigger_side"],
                record["standard_execution_mode"],
                record.get("backfilled_at"),
            )
        elif getattr(backfill, "last_touch_found", False):
            # Backfill found a qualifying touch but could not
            # reconstruct the isolation record. This is a real failure
            # — not the "nothing to reconstruct" case.
            logger.error(
                "STARTUP BACKFILL: touch WAS found at %s but isolation "
                "was NOT created. Isolation for today is MISSING. "
                "Reason: %s",
                getattr(backfill, "last_touch_minute", "?"),
                getattr(backfill, "last_error", "unknown"),
            )
        else:
            logger.info("Startup backfill: nothing to reconstruct")
    except Exception:
        logger.exception("Startup backfill failed")