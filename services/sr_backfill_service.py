# services/sr_backfill_service.py
"""Intraday SR backfill — seeds the SR engine from candles.

## 2026-10-26 change
The service now (by default) fetches **historical + intraday** merged
candles for each instrument, matching the on-demand calc at
``/live-sr/calc``. Previously it seeded from today's intraday only,
which caused the live engine to miss pivots at the start of the day
(those that require 15 bars *before* the first tick, i.e. yesterday's
tail).

Config: ``SR_BACKFILL_INCLUDE_HISTORICAL`` (default true).

## 2026-10-28 change — historical-candle cache
The periodic refresh job (``live_sr_refresh_from_api``, every
``LIVE_SR_REFRESH_FROM_API_MINUTES`` minutes) used to re-read every
instrument's ``candles.json`` from disk on every cycle. With 170
instruments × ~500 candles each, that was ~85,000 JSON objects parsed
per cycle for data that does not change intraday (the backup archive
is re-extracted once per daily refresh at 08:30 IST).

Historical candles are now cached in-process for the trading day.
Only the FIRST ``run()`` of the day pays the disk-read cost; every
subsequent cycle reuses the cache. ``reason="manual"`` triggers still
invalidate the cache so a backup re-extract via ``/admin/hard-refresh``
is picked up on the next manual SR backfill. ``clear_historical_cache()``
is exposed for direct invocation.

## 2026-11-04 change — readiness latch, snapshot epoch, one-shot policy
----------------------------------------------------------
Three additions, all module-level and process-local:

1. **Readiness latch** — ``is_ready()`` returns True once *today's*
   full backfill has completed in this process. The latch resets
   automatically when ``_SEEDED_DAY`` no longer matches today's IST
   date, and can be cleared manually via ``clear_seed_latch()``. A
   fresh process starts with the latch unset — that is intentional,
   because a new process has an empty in-memory SR engine even when
   today's ``live_sr_levels.json`` files already exist, and therefore
   *needs* to re-seed on feed-start to warm the candle buffer.

2. **Snapshot epoch** — ``snapshot_epoch()`` is a monotonically
   increasing counter that bumps on every completed seed with at
   least one instrument seeded. Downstream consumers that pair this
   with ``live_sr_levels_service.snapshot()`` can cheaply detect
   "has the SR world changed since I last looked?" by comparing
   epochs instead of diffing full snapshots.

3. **One-shot-per-day seed policy** — ``run()`` now short-circuits
   automatic triggers (``feed_start``, scheduler) when
   ``is_ready()`` is already True for today. Manual triggers
   (``reason="manual"``) and explicit ``force=True`` always bypass
   the latch. This stops a mid-session feed restart from paying the
   full historical + intraday merge cost a second time when the SR
   engine in this process is already warm, while still allowing a
   forced re-seed after a backup re-extract or a code push.

Callers can inspect the latch through ``sr_backfill_service.status()``
(a superset of ``last_status()`` that adds ``ready``, ``seeded_day``,
``seeded_at``, and ``snapshot_epoch``).

## 2026-11-04 fix — expose ``is_ready_for_today()``
--------------------------------------------------
The 2026-11-04 readiness latch was exposed only under the name
``is_ready()``. Two downstream consumers, however, look up the
method under the name ``is_ready_for_today()``:

  * ``services/scheduler_service.sr_backfill_ready()``:
        probe = getattr(sr_backfill_service, "is_ready_for_today", None)
        if probe is None:
            return False, "missing_probe"
  * ``services/dummy_order_service._upstream_ready()``:
        (gated the same way — currently logs a warning and proceeds
         without the readiness gate when the probe is absent)

Because the method was named ``is_ready()``, both probes returned
``None`` and both consumers fell into their "missing probe" branches.
The scheduler case skipped the entire ``dummy_order_tracker`` every
30 s from startup onward (visible in the runtime log as
``dummy_order_tracker skipped — missing_probe``). The dummy-order
case emitted a WARNING and proceeded without the readiness gate.

``is_ready_for_today()`` is now exposed both as a module-level
function and as an instance method. Both are thin aliases for
``is_ready()`` — the semantics are identical, only the name differs.
The rename preserves backward compatibility (``is_ready()`` still
works) while satisfying the scheduler/dummy-order contract.

A third consumer joined this contract on the same day:
``services/sandbox_execution_service.SandboxExecutionService._gate()``
probes the same name (with a fall-back to ``is_ready()``, and a
final ``sr_readiness_probe_missing`` failure mode if neither method
resolves). The live sandbox executor relies on this latch to refuse
to place real orders off a partially-built SR series — same race
the scheduler and dummy-order services were already guarding against.

## 2026-11-06 change — pluggable historical source + ad-hoc backfill
-------------------------------------------------------------------
Two independent additions, both driven by the isolated-instrument
pipeline.

**(A) ``SR_BACKFILL_HISTORICAL_SOURCE`` — pluggable historical source.**

Historical candles used to be read *only* from the on-disk backup
tree (``candles.json``). That works for the daily-refresh flow but is
not the only possible source — some deployments ship a codebase whose
intraday API (``HistoryV3Api.get_intra_day_candle_data``) is the
canonical, always-available source, and the disk copy can lag behind
or be absent entirely (e.g. an option instrument that was just listed
today has no backup entry yet).

``_fetch_merged_candles()`` now consults ``SR_BACKFILL_HISTORICAL_SOURCE``:

    * ``"api"`` / ``"intraday"`` / ``"intraday_api"``
        Pure-API mode. BOTH the "historical" and "intraday" roles are
        served by ``HistoryV3Api.get_intra_day_candle_data(key, "minutes", "1")``.
        No disk reads at all. Use this in deployments where the disk
        backup is not authoritative and every instrument must be
        backfilled from the live API.

    * ``"disk"`` / ``"legacy"``
        Legacy behaviour. Historical from the in-process day cache
        over ``candles.json``; intraday from the API.

    * ``"merge"`` / ``"auto"`` / ``"both"`` (default)
        Historical tries disk first; when the disk read yields
        nothing, falls back to the API. Intraday always from the API.
        This is the safest default — it preserves the existing
        disk-first behaviour for instruments with backup files while
        transparently unblocking the "just-listed option" case.

The API call itself is ``_fetch_via_intraday_api()`` — a thin,
documented alias for ``_fetch_intraday_candles()`` (the canonical
``HistoryV3Api.get_intra_day_candle_data`` invocation) so the flag
wiring is explicit at every call site.

**(B) ``backfill_instruments()`` — ad-hoc mid-session backfill.**

``run()`` is the full-pipeline entry point: it consults
``should_run()``, honours the one-shot-per-day latch, iterates the
full instrument registry, and (by default) marks the readiness latch
via ``_mark_seeded()``. That is exactly right for the startup and
periodic flows, but wrong for the isolated-instrument pipeline:

    * When ``isolated_instrument_promotion_service`` registers a
      newly-active isolated/opposite pair mid-session, only **two**
      instruments need seeding — not 170.
    * The seed must NOT flip the readiness latch: the frozen replay
      series used by ``dummy_order_service`` is already built and
      must remain stable for the day.
    * The seed must NOT touch the frozen replay series: the isolated
      pair only needs the live buffer warmed so subsequent ticks see
      the candle tails.

``backfill_instruments()`` is that path:

    * accepts a mixed list of registry entry dicts and/or bare
      ``instrument_key`` strings;
    * resolves/``register_adhoc``-s any missing keys in
      ``instrument_registry``;
    * reuses the same ``_seed_one_instrument()`` helper as ``run()``
      so candle fetch, pivot detection, and buffer seeding are
      byte-identical;
    * by default ``update_replay=False``, so the readiness latch and
      ``snapshot_epoch`` stay untouched;
    * never calls ``should_run()`` and never consults
      ``is_ready()`` — partial mid-session seeds are the whole point.

``register_and_backfill(keys, reason=...)`` is a convenience wrapper
for the common "single key or list of keys, resolve via registry,
seed" case — used by the promotion service's
``on_isolation_triggered`` and by
``isolated_instrument_promotion_service._prepare_isolation_pair``.

## 2026-11-06 fix — ``bucket``/``ts_ms`` on API-derived candles
---------------------------------------------------------------
Runtime traceback observed on 2026-11-06 10:46:12::

    File "services/sr_backfill_service.py", line 654, in _seed_one_instrument
      candles = _fetch_merged_candles(entry, lookback_days)
    File "services/sr_backfill_service.py", line 625, in _fetch_merged_candles
      return live_sr_calc_service.merge_candles(historical, intraday)
    File "services/live_sr_calc_service.py", line 2333, in _merge_by_minute
      by_bucket[c["bucket"]] = c
    KeyError: 'bucket'

Root cause: ``_fetch_intraday_candles()`` hand-built candle dicts with
only ``{time, open, high, low, close, volume}``. Those dicts lack the
``bucket`` (and ``ts_ms``) fields that
``live_sr_calc_service._merge_by_minute()`` requires for dedupe.
The bug only surfaced when the disk-based historical read yielded
empty (log: ``historical fallback source=api candles=90 (disk empty)``)
and the API-derived candles were fed straight into ``merge_candles()``.

Fix: ``_fetch_intraday_candles()`` now normalises via
``live_sr_calc_service._normalise_candle()`` — the same normaliser the
calc endpoint uses — so API-derived candles carry the exact
``{time, ts_ms, bucket, open, high, low, close, volume}`` shape. A
belt-and-braces manual-bucket fallback runs if the import is
unavailable (e.g. during early startup where the calc service module
hasn't loaded yet). ``_fetch_merged_candles()`` additionally filters
out any legacy-shaped candles immediately before merge, so a future
producer regression can never re-introduce the same crash.

Runs when ALL of the following hold:
  * ``SR_BACKFILL_ENABLED`` is True
  * ``LIVE_SR_ENABLED`` is True
  * current time is in the configured window (default 09:15–23:59 IST)
  * today is Mon-Fri
  * today is NOT a trading holiday

Called from:
  * ``services.live_feed_service.LiveFeedService.start()`` — before
    spawning the WebSocket worker thread.
  * ``POST /live-sr/backfill`` — manual trigger.
  * The periodic scheduler job ``live_sr_refresh_from_api`` (every
    ``LIVE_SR_REFRESH_FROM_API_MINUTES`` minutes).
  * The deferred scheduler job ``replay_seed_at_open`` (09:16 IST,
    idempotent) — fallback for a pre-09:15 startup where feed-start
    correctly skipped.
  * (2026-11-06) ``isolated_instrument_promotion_service.apply_promotion()``
    and ``on_isolation_triggered()`` — via ``backfill_instruments()``
    for the newly-active isolated/opposite pair.

Telegram: sent only on the ``startup`` / ``feed_start`` path. The
periodic refresh passes ``send_telegram=False`` to avoid flooding.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

_MARKET_TZ = ZoneInfo("Asia/Kolkata")

# --------------------------------------------------------------------------- #
# Historical-candle cache                                                     #
# --------------------------------------------------------------------------- #
# Historical candles from each instrument's candles.json are stable for
# the whole trading day; the backup archive itself is re-extracted once
# per daily refresh (08:30 IST). Re-reading 170 files every 2 minutes
# just to re-seed pivots against a tail that did not move is wasted I/O.
#
# Cache invalidation:
#   * auto on IST date rollover (see _load_historical_cached)
#   * explicit via clear_historical_cache()
#   * forced from run(reason="manual")
_HISTORICAL_CACHE: dict[tuple[str, int], list[dict[str, Any]]] = {}
_HISTORICAL_CACHE_DAY: str = ""
_HISTORICAL_CACHE_LOCK = threading.Lock()

# --------------------------------------------------------------------------- #
# Readiness latch / snapshot epoch (2026-11-04)                               #
# --------------------------------------------------------------------------- #
# These are process-local. On process restart they start blank, which is
# exactly what we want: the in-memory SR engine is also blank after a
# restart, so a fresh seed on the next feed_start is correct.
#
# ``_SEEDED_DAY`` is the IST ISO date for which the full seed last
# completed. It is compared against ``_today_market_iso()`` on every
# ``is_ready()`` call, so day rollover resets the latch implicitly
# without a scheduled task.
#
# ``_SNAPSHOT_EPOCH`` bumps once per successful run with at least one
# instrument seeded. It is monotonic for the life of the process;
# consumers must not assume persistence across restarts.
_SEEDED_DAY: str = ""
_SEEDED_AT: str | None = None
_SNAPSHOT_EPOCH: int = 0
_SEED_LOCK = threading.Lock()


def _today_market_iso() -> str:
    return _now_market().date().isoformat()


def _load_historical_cached(
    entry: dict[str, Any],
    lookback_days: int,
) -> list[dict[str, Any]]:
    """Return historical candles for ``entry``, cached for the day.

    The cache key is ``(instrument_key, lookback_days)`` so a caller
    that overrides the lookback for a specific instrument (none do
    today; the periodic refresh always uses
    ``LIVE_SR_CALC_DEFAULT_LOOKBACK_DAYS``) does not collide with the
    default-window cached value.

    Returns ``[]`` for an instrument with no ``candles.json`` on disk
    (ad-hoc indexes registered on demand, brand-new options with no
    backup entry yet). That empty list IS cached — a missing backup
    file is not a transient condition within a session, so re-reading
    it every 2 minutes would only add disk I/O. Manual triggers bypass
    the cache, so a newly-appeared backup file is picked up the next
    time someone clicks ``POST /live-sr/backfill`` or triggers a hard
    refresh.

    Never raises.
    """
    global _HISTORICAL_CACHE_DAY

    key = str(entry.get("instrument_key") or "").strip()
    if not key:
        return []

    today = _today_market_iso()
    cache_key = (key, int(lookback_days))

    with _HISTORICAL_CACHE_LOCK:
        if _HISTORICAL_CACHE_DAY != today:
            _HISTORICAL_CACHE.clear()
            _HISTORICAL_CACHE_DAY = today
        cached = _HISTORICAL_CACHE.get(cache_key)

    if cached is not None:
        return cached

    try:
        from services.live_sr_calc_service import live_sr_calc_service
        historical = live_sr_calc_service.load_historical_candles_for_entry(
            entry, lookback_days,
        )
    except Exception:
        logger.exception(
            "[sr-backfill] historical load failed for %s", key,
        )
        historical = []

    with _HISTORICAL_CACHE_LOCK:
        _HISTORICAL_CACHE[cache_key] = historical
    return historical


def clear_historical_cache() -> int:
    """Drop the in-process historical-candle cache.

    Returns the number of entries that were discarded. Safe to call
    from any thread. Used automatically by ``run(reason="manual")`` and
    available for direct invocation after a backup re-extract.

    Note: this does **not** clear the readiness latch. Use
    ``clear_seed_latch()`` for that — the two are independent concerns
    (cache vs "did we already seed today").
    """
    global _HISTORICAL_CACHE_DAY
    with _HISTORICAL_CACHE_LOCK:
        n = len(_HISTORICAL_CACHE)
        _HISTORICAL_CACHE.clear()
        _HISTORICAL_CACHE_DAY = ""
    return n


# --------------------------------------------------------------------------- #
# Readiness latch / snapshot epoch — public surface (2026-11-04)              #
# --------------------------------------------------------------------------- #
# Two names are exposed for the readiness latch:
#
#   * ``is_ready()``             — canonical name; used internally by
#                                  ``run()`` to enforce the one-shot
#                                  policy, and by ``main.py``.
#   * ``is_ready_for_today()``   — contractual name consumed by
#                                  ``scheduler_service.sr_backfill_ready()``
#                                  and ``dummy_order_service._upstream_ready()``
#                                  and ``sandbox_execution_service._gate()``.
#
# Both are exact aliases — identical semantics, different name. Adding
# the second name closes the "missing_probe" bug where the scheduler
# skipped ``dummy_order_tracker`` on every tick from startup onward
# because ``getattr(sr_backfill_service, "is_ready_for_today", None)``
# returned ``None``.
# --------------------------------------------------------------------------- #
def is_ready() -> bool:
    """True when today's full backfill has completed in this process.

    Resets automatically on IST date rollover (``_SEEDED_DAY`` no longer
    matches today). A fresh process starts unready even if today's
    ``live_sr_levels.json`` files exist — the in-memory SR engine is
    empty after a restart, so a seed is required to warm the buffer.

    Safe to call from any thread. O(1).
    """
    with _SEED_LOCK:
        return _SEEDED_DAY == _today_market_iso()


def is_ready_for_today() -> bool:
    """Alias for ``is_ready()`` — contractual name.

    Consumed by:
      * ``services/scheduler_service.sr_backfill_ready()``
        (via ``getattr(sr_backfill_service, "is_ready_for_today", None)``)
      * ``services/dummy_order_service._upstream_ready()``
        (same pattern)
      * ``services/sandbox_execution_service.SandboxExecutionService._gate()``
        (same pattern; falls back to ``is_ready()`` if this method is
        absent, and raises ``sr_readiness_probe_missing`` only when
        neither name resolves)

    Both the scheduler and the dummy-order service look the method up
    by this exact name. Exposing it here means those probes now resolve
    and the "missing_probe" / "not implemented" fallback branches are
    no longer taken.

    Semantics are identical to ``is_ready()`` — this is a pure alias.
    Prefer either name; they cannot diverge.
    """
    return is_ready()


def snapshot_epoch() -> int:
    """Monotonically increasing counter, bumped on every successful
    seed with at least one instrument.

    Consumers that pair this with
    ``live_sr_levels_service.snapshot(key)`` can detect "has the SR
    world changed since I last looked?" by comparing epoch values
    instead of diffing full snapshots.

    Process-local; not persisted across restarts.
    """
    with _SEED_LOCK:
        return _SNAPSHOT_EPOCH


def seed_status() -> dict[str, Any]:
    """Return the current readiness latch + epoch as a dict.

    Useful for status endpoints and tests. Values are a copy —
    mutating the returned dict does not affect the module state.
    """
    with _SEED_LOCK:
        return {
            "ready": _SEEDED_DAY == _now_market().date().isoformat(),
            "seeded_day": _SEEDED_DAY or None,
            "seeded_at": _SEEDED_AT,
            "snapshot_epoch": _SNAPSHOT_EPOCH,
        }


def clear_seed_latch() -> None:
    """Reset the readiness latch and epoch to a fresh-process state.

    Use after a full SR reset (``live_sr_levels_service.reset_state()``)
    or in tests. Does **not** touch the historical-candle cache — use
    ``clear_historical_cache()`` for that.
    """
    global _SEEDED_DAY, _SEEDED_AT, _SNAPSHOT_EPOCH
    with _SEED_LOCK:
        _SEEDED_DAY = ""
        _SEEDED_AT = None
        _SNAPSHOT_EPOCH = 0


def _mark_seeded() -> int:
    """Record that today's seed just completed. Returns the new epoch.

    Called at most once per successful ``run()`` (i.e. once per day
    for automatic triggers, on every successful manual run otherwise).
    """
    global _SEEDED_DAY, _SEEDED_AT, _SNAPSHOT_EPOCH
    with _SEED_LOCK:
        _SEEDED_DAY = _now_market().date().isoformat()
        _SEEDED_AT = _now_iso()
        _SNAPSHOT_EPOCH += 1
        return _SNAPSHOT_EPOCH


# --------------------------------------------------------------------------- #
# Small time helpers                                                          #
# --------------------------------------------------------------------------- #
def _now_market() -> datetime:
    return datetime.now(_MARKET_TZ)

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _load_sdk():
    try:
        import upstox_client  # type: ignore
        return upstox_client
    except ImportError:
        return None

def _coerce_dict(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    to_dict = getattr(raw, "to_dict", None)
    if callable(to_dict):
        try:
            d = to_dict()
        except Exception:
            d = None
        if isinstance(d, dict):
            return d
    data_attr = getattr(raw, "data", None)
    if isinstance(data_attr, dict):
        return data_attr
    return None


def _normalise_candle_via_calc_service(
    raw: Any,
) -> dict[str, Any] | None:
    """Normalise one raw Upstox candle via the calc service's normaliser.

    ``live_sr_calc_service._normalise_candle()`` produces the canonical
    ``{time, ts_ms, bucket, open, high, low, close, volume}`` shape that
    ``live_sr_calc_service.merge_candles()`` requires.

    Returns ``None`` when the calc service is unavailable or the raw
    entry cannot be normalised. Never raises.
    """
    try:
        from services.live_sr_calc_service import _normalise_candle
    except Exception:
        return None
    try:
        return _normalise_candle(raw)
    except Exception:
        return None


def _normalise_candle_fallback(
    raw: Any,
) -> dict[str, Any] | None:
    """Hand-build a candle with ``bucket``/``ts_ms`` fields.

    Used only when the calc-service normaliser is unavailable. Mirrors
    the shape produced by ``live_sr_calc_service._normalise_candle``
    for the list-of-lists Upstox form. Never raises.
    """
    if not isinstance(raw, list) or len(raw) < 5:
        return None
    try:
        ts_str = str(raw[0])
        o = float(raw[1])
        h = float(raw[2])
        l = float(raw[3])
        c_val = float(raw[4])
        v = float(raw[5]) if len(raw) > 5 and raw[5] is not None else 0.0

        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_MARKET_TZ)
        ts_epoch = dt.timestamp()
        return {
            "time": dt.isoformat(),
            "ts_ms": int(ts_epoch * 1000),
            "bucket": int(ts_epoch // 60),
            "open": o,
            "high": h,
            "low": l,
            "close": c_val,
            "volume": v,
        }
    except (ValueError, TypeError, IndexError):
        return None


def _fetch_intraday_candles(
    instrument_key: str,
) -> list[dict[str, Any]]:
    """Fetch intraday 1-min candles via HistoryV3Api.get_intra_day_candle_data.

    Returns candles in the canonical
    ``{time, ts_ms, bucket, open, high, low, close, volume}`` shape so
    they can be merged by ``live_sr_calc_service.merge_candles()``.

    2026-11-06 fix: the previous implementation hand-built candle dicts
    lacking ``bucket`` / ``ts_ms``. When the historical half was empty
    (disk miss) and the API candles flowed straight into
    ``_merge_by_minute()``, this produced ``KeyError: 'bucket'``. The
    fetcher now delegates to
    ``live_sr_calc_service._normalise_candle()`` (with a manual-bucket
    fallback if that import is unavailable).

    Returns ``[]`` on any failure; never raises.
    """
    sdk = _load_sdk()
    if sdk is None:
        return []

    try:
        cfg = sdk.Configuration()
        # Token setup — previously missing. The calc service sets the
        # access_token from token_service; mirror that here so the API
        # call is authenticated on the same code path.
        try:
            from services.token_service import token_service
            token = token_service.get_access_token()
            if token:
                cfg.access_token = token
        except Exception:
            # Token service failure is non-fatal — Upstox client may
            # fall back to env-var / default credentials.
            pass

        api = sdk.HistoryV3Api(sdk.ApiClient(cfg))
        response = api.get_intra_day_candle_data(
            instrument_key, "minutes", "1",
        )
    except Exception as exc:
        logger.warning(
            "[sr-backfill] intraday fetch failed for %s: %s: %s",
            instrument_key, type(exc).__name__, exc,
        )
        return []

    body = _coerce_dict(response)
    if body is None:
        return []
    raw_candles = (body.get("data") or {}).get("candles") or []
    if not isinstance(raw_candles, list):
        return []

    out: list[dict[str, Any]] = []
    for raw in raw_candles:
        c = _normalise_candle_via_calc_service(raw)
        if c is None:
            # Fallback — hand-build with bucket / ts_ms.
            c = _normalise_candle_fallback(raw)
        if c is not None:
            out.append(c)
    return out


def _fetch_via_intraday_api(
    instrument_key: str,
) -> list[dict[str, Any]]:
    """Fetch 1-min candles via ``HistoryV3Api.get_intra_day_candle_data``.

    Documented alias for ``_fetch_intraday_candles()`` used by the
    ``SR_BACKFILL_HISTORICAL_SOURCE == "api"`` branch of
    ``_fetch_merged_candles``. Named distinctly so the historical-source
    flag wiring is explicit at each call site — the same API call now
    serves both the "historical" and "intraday" roles when the flag is
    set to ``"api"``.

    Returns ``[]`` on any failure; never raises.
    """
    return _fetch_intraday_candles(instrument_key)


def _is_bucket_shaped(c: Any) -> bool:
    """Return True when ``c`` carries a ``bucket`` key suitable for merge."""
    return (
        isinstance(c, dict)
        and isinstance(c.get("bucket"), int)
        and c.get("bucket") is not None
    )


def _fetch_merged_candles(
    entry: dict[str, Any],
    lookback_days: int,
) -> list[dict[str, Any]]:
    """Fetch historical + intraday candles per ``SR_BACKFILL_HISTORICAL_SOURCE``.

    Source modes (config flag, case-insensitive):

      * ``"api"`` / ``"intraday"`` / ``"intraday_api"``
            Pure-API mode. BOTH the historical and intraday roles are
            served by ``HistoryV3Api.get_intra_day_candle_data(key,
            "minutes", "1")``. No disk reads. Use this in deployments
            where the disk backup is not authoritative and every
            instrument must be backfilled from the live API.

      * ``"disk"`` / ``"legacy"``
            Legacy behaviour. Historical from the in-process day cache
            over ``candles.json``; intraday from the API.

      * ``"merge"`` / ``"auto"`` / ``"both"`` (default)
            Historical tries disk first; when the disk read yields
            nothing, falls back to the API. Intraday always from the
            API. Preserves the existing disk-first behaviour for
            instruments with backup files while transparently
            unblocking the "just-listed option" case.

    Never raises. A failed source returns ``[]`` for that half; the
    other half still seeds what it can.

    2026-11-06 fix: before merging, both halves are defensively
    filtered to candles carrying the ``bucket`` field. This guards
    against any future producer regression that re-introduces the
    hand-built candle shape; the primary fix lives in
    ``_fetch_intraday_candles()``.
    """
    source = str(
        getattr(config, "SR_BACKFILL_HISTORICAL_SOURCE", "auto")
    ).strip().lower()

    key = str(entry.get("instrument_key") or "").strip()
    if not key:
        return []

    # ---- Pure-API mode --------------------------------------------- #
    if source in ("api", "intraday", "intraday_api"):
        candles = _fetch_via_intraday_api(key)
        candles = [c for c in candles if _is_bucket_shaped(c)]
        logger.info(
            "[sr-backfill] %s fetch source=api candles=%d",
            key, len(candles),
        )
        return candles

    # ---- Disk-first modes (legacy / auto) -------------------------- #
    try:
        from services.live_sr_calc_service import live_sr_calc_service
    except Exception:
        logger.exception(
            "[sr-backfill] live_sr_calc_service import failed; "
            "falling back to intraday-only"
        )
        candles = _fetch_intraday_candles(key)
        return [c for c in candles if _is_bucket_shaped(c)]

    historical = _load_historical_cached(entry, lookback_days)
    if not historical and source in ("merge", "auto", "both", "fallback"):
        # Disk read yielded nothing (brand-new option, absent backup
        # file, post-hard-refresh missed entry). Fall back to the API
        # for the historical half so the merged series still seeds.
        historical = _fetch_via_intraday_api(key)
        logger.info(
            "[sr-backfill] %s historical fallback source=api candles=%d "
            "(disk empty)",
            key, len(historical),
        )

    try:
        intraday = live_sr_calc_service.fetch_intraday_candles_for_entry(key)
    except Exception:
        logger.exception(
            "[sr-backfill] intraday fetch failed for %s", key,
        )
        intraday = []

    # Defensive filter — protects _merge_by_minute from legacy-shaped
    # candles regardless of which source produced them.
    historical = [c for c in (historical or []) if _is_bucket_shaped(c)]
    intraday = [c for c in (intraday or []) if _is_bucket_shaped(c)]

    if historical and intraday:
        return live_sr_calc_service.merge_candles(historical, intraday)
    return historical or intraday


# --------------------------------------------------------------------------- #
# Shared per-instrument seed helper                                           #
# --------------------------------------------------------------------------- #
def _seed_one_instrument(
    entry: dict[str, Any],
    include_historical: bool,
    lookback_days: int,
) -> dict[str, Any]:
    """Seed one instrument into ``live_sr_levels_service``.

    Shared by ``run()`` and ``backfill_instruments()`` so the candle
    fetch, pivot scan, and buffer seeding are byte-identical between
    the full-pipeline and ad-hoc paths.

    Returns ``{"key", "candles", "pivots", "error"}``. Never raises —
    all failures are captured in ``error``.
    """
    from services.live_sr_levels_service import live_sr_levels_service

    key = str(entry.get("instrument_key") or "").strip()
    if not key:
        return {"key": None, "candles": 0, "pivots": 0, "error": "missing_key"}

    try:
        if include_historical:
            candles = _fetch_merged_candles(entry, lookback_days)
        else:
            candles = _fetch_intraday_candles(key)
    except Exception as exc:
        logger.exception("[sr-backfill] candle fetch failed for %s", key)
        return {
            "key": key, "candles": 0, "pivots": 0,
            "error": f"fetch_failed:{type(exc).__name__}",
        }

    if not candles:
        return {"key": key, "candles": 0, "pivots": 0, "error": None}

    try:
        result = live_sr_levels_service.seed_candles(key, entry, candles)
        return {
            "key": key,
            "candles": int(result.get("candles_seeded") or 0),
            "pivots": int(result.get("pivots_detected") or 0),
            "error": result.get("error"),
        }
    except Exception as exc:
        logger.exception("[sr-backfill] seed failed for %s", key)
        return {
            "key": key, "candles": 0, "pivots": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }


# --------------------------------------------------------------------------- #
# Gate helpers                                                                #
# --------------------------------------------------------------------------- #
def _is_trading_day() -> tuple[bool, str]:
    now = _now_market()
    if now.weekday() > 4:
        return False, f"weekend ({now.strftime('%A')})"

    try:
        from services.market_holiday_service import market_holiday_service
        payload = market_holiday_service.get_today()
        status = str(payload.get("status") or "").lower()
        if status == "holiday":
            holidays = payload.get("holidays") or []
            desc = (
                holidays[0].get("description")
                if holidays and isinstance(holidays[0], dict)
                else None
            )
            return False, f"holiday ({desc or 'unknown'})"
    except Exception:
        logger.exception(
            "[sr-backfill] holiday check failed; assuming trading day"
        )

    return True, "trading_day"

def _in_window() -> tuple[bool, str]:
    now = _now_market().time()
    start = dtime(
        int(config.SR_BACKFILL_START_HOUR),
        int(config.SR_BACKFILL_START_MINUTE),
    )
    end = dtime(
        int(config.SR_BACKFILL_END_HOUR),
        int(config.SR_BACKFILL_END_MINUTE),
    )
    if start <= now <= end:
        return True, "in_window"
    return False, (
        f"outside_window (now={now.isoformat()}, "
        f"window={start.isoformat()}-{end.isoformat()})"
    )

# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class SRBackfillService:
    def __init__(self) -> None:
        self._last_status: dict[str, Any] = {"status": "never"}

    # ------------------------------------------------------------------ #
    # Gate                                                               #
    # ------------------------------------------------------------------ #
    def should_run(self) -> tuple[bool, str]:
        if not config.SR_BACKFILL_ENABLED:
            return False, "disabled"
        if not config.LIVE_SR_ENABLED:
            return False, "sr_disabled"
        trading, reason = _is_trading_day()
        if not trading:
            return False, reason
        in_win, win_reason = _in_window()
        if not in_win:
            return False, win_reason
        return True, "ok"

    # ------------------------------------------------------------------ #
    # Run                                                                #
    # ------------------------------------------------------------------ #
    def run(
        self,
        reason: str = "feed_start",
        entries: list[dict[str, Any]] | None = None,
        send_telegram: bool = True,
        force: bool = False,
        update_replay: bool = True,
    ) -> dict[str, Any]:
        """Seed the SR engine.

        ``force`` bypasses the one-shot-per-day latch but does **not**
        clear the historical-candle cache — use ``reason="manual"`` for
        the post-hard-refresh path that needs fresh disk reads.

        ``update_replay`` (2026-11-04): when ``False``, the run updates
        only the live buffer and **does not** mark the readiness latch.
        The scheduler's periodic ``_refresh_live_sr`` tick passes
        ``update_replay=False`` so that (a) it never overwrites the
        frozen replay series that ``dummy_order_service`` reads, and
        (b) it never falsely marks ``is_ready_for_today()`` as True
        when the replay series hasn't actually been seeded. The
        ``replay_seed_at_open`` scheduler job passes ``update_replay=
        True`` — it is the only deferred path allowed to seed the
        replay series.

        Returns a summary dict. When the run is skipped (disabled,
        outside window, already seeded today), the dict carries
        ``status="skipped"`` and a ``skip_reason``. When the run
        completes, ``status`` is ``"ok"`` or ``"partial"``.
        """
        should, why = self.should_run()
        if not should:
            summary = {
                "status": "skipped",
                "reason": reason,
                "skip_reason": why,
                "ran_at": _now_iso(),
                "instruments": 0,
                "candles_total": 0,
                "pivots_total": 0,
                "errors": 0,
                "ready": is_ready(),
                "snapshot_epoch": snapshot_epoch(),
                "update_replay": update_replay,
            }
            logger.info("[sr-backfill] skipped — %s", why)
            self._last_status = summary
            return summary

        # ---- One-shot-per-day policy ------------------------------- #
        # Automatic triggers (feed_start, scheduler) are skipped once
        # today's seed has completed in this process. Manual and
        # force=True always bypass.
        if not force and reason != "manual" and is_ready():
            summary = {
                "status": "skipped",
                "reason": reason,
                "skip_reason": "already_seeded_today",
                "ran_at": _now_iso(),
                "instruments": 0,
                "candles_total": 0,
                "pivots_total": 0,
                "errors": 0,
                "ready": True,
                "snapshot_epoch": snapshot_epoch(),
                "update_replay": update_replay,
            }
            logger.info(
                "[sr-backfill] skipped — already_seeded_today "
                "(reason=%s; pass force=True or reason='manual' to re-run)",
                reason,
            )
            self._last_status = summary
            return summary

        # Manual triggers always read fresh historical data. This is
        # the path an operator uses after POST /admin/hard-refresh
        # (which re-extracts the backup archive), so the newly-written
        # candles.json files must be picked up immediately.
        if reason == "manual":
            cleared = clear_historical_cache()
            logger.info(
                "[sr-backfill] historical cache cleared "
                "(entries=%d) for manual trigger", cleared,
            )

        if entries is None:
            from services.instrument_registry import instrument_registry
            entries = list(instrument_registry.all())

        entries = [e for e in entries if isinstance(e, dict)]
        if not entries:
            summary = {
                "status": "no_instruments",
                "reason": reason,
                "ran_at": _now_iso(),
                "instruments": 0,
                "candles_total": 0,
                "pivots_total": 0,
                "errors": 0,
                "ready": is_ready(),
                "snapshot_epoch": snapshot_epoch(),
                "update_replay": update_replay,
            }
            self._last_status = summary
            return summary

        include_historical = bool(
            getattr(config, "SR_BACKFILL_INCLUDE_HISTORICAL", True)
        )
        lookback_days = int(
            getattr(config, "LIVE_SR_CALC_DEFAULT_LOOKBACK_DAYS", 5)
        )
        workers = max(1, int(config.SR_BACKFILL_MAX_WORKERS))
        t0 = _now_market()
        logger.info(
            "[sr-backfill] START reason=%s instruments=%d workers=%d "
            "include_historical=%s lookback_days=%d update_replay=%s "
            "historical_source=%s",
            reason, len(entries), workers, include_historical,
            lookback_days, update_replay,
            str(getattr(config, "SR_BACKFILL_HISTORICAL_SOURCE", "auto")),
        )

        errors = 0
        candles_total = 0
        pivots_total = 0
        seeded_instruments = 0

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    _seed_one_instrument, e, include_historical, lookback_days,
                )
                for e in entries
            ]
            for fut in as_completed(futures):
                try:
                    r = fut.result()
                except Exception:
                    errors += 1
                    continue
                c = int(r.get("candles") or 0)
                p = int(r.get("pivots") or 0)
                if r.get("error"):
                    errors += 1
                if c > 0:
                    seeded_instruments += 1
                candles_total += c
                pivots_total += p

        elapsed = (_now_market() - t0).total_seconds()

        # ---- Readiness latch + snapshot epoch ---------------------- #
        # The latch only fires when:
        #   * at least one instrument actually seeded, AND
        #   * the caller is doing a replay-series update
        #     (update_replay=True). The every-2-minute live-buffer
        #     refresh (update_replay=False) MUST NOT mark the latch,
        #     because it does not touch the replay series that
        #     dummy_order_service reads.
        if seeded_instruments > 0 and update_replay:
            epoch = _mark_seeded()
            ready = True
        else:
            epoch = snapshot_epoch()
            ready = is_ready()

        summary = {
            "status": "ok" if errors == 0 else "partial",
            "reason": reason,
            "ran_at": _now_iso(),
            "instruments_total": len(entries),
            "instruments_seeded": seeded_instruments,
            "candles_total": candles_total,
            "pivots_total": pivots_total,
            "errors": errors,
            "elapsed_seconds": round(elapsed, 2),
            "include_historical": include_historical,
            "lookback_days": lookback_days,
            "ready": ready,
            "snapshot_epoch": epoch,
            "update_replay": update_replay,
        }
        logger.info(
            "[sr-backfill] COMPLETE — instruments=%d seeded=%d candles=%d "
            "pivots=%d errors=%d elapsed=%.1fs epoch=%d update_replay=%s",
            len(entries), seeded_instruments, candles_total,
            pivots_total, errors, elapsed, epoch, update_replay,
        )
        logger.info(
            "[sr-backfill] historical cache: day=%s entries=%d",
            _HISTORICAL_CACHE_DAY or "cold",
            len(_HISTORICAL_CACHE),
        )

        if send_telegram:
            try:
                from services.telegram_service import telegram_service
                _DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                _THIN = "────────────────────────────────"
                msg = (
                    f"{_DIV}\n"
                    f"🎯 SR BACKFILL · {reason.upper()}\n"
                    f"{_DIV}\n"
                    f"🕒 Time:           {_now_market().strftime('%H:%M:%S IST')}\n"
                    f"📊 Instruments:    {len(entries)}\n"
                    f"✅ Seeded:         {seeded_instruments}\n"
                    f"🕯️ Candles:        {candles_total}\n"
                    f"📐 Pivots:         {pivots_total}\n"
                    f"📜 Historical:     "
                    f"{'included' if include_historical else 'disabled'}\n"
                    f"❌ Errors:         {errors}\n"
                    f"⏱️ Elapsed:        {round(elapsed, 1)}s\n"
                    f"{_THIN}\n"
                    f"Live ticks will continue from here. Live feed starting…"
                )
                level = "SUCCESS" if errors == 0 else "WARNING"
                telegram_service.send_message(
                    "SR Backfill Completed", msg, level=level,
                )
            except Exception:
                logger.exception(
                    "[sr-backfill] Telegram notification failed"
                )

        self._last_status = summary
        return summary

    # ------------------------------------------------------------------ #
    # Ad-hoc mid-session backfill (2026-11-06)                            #
    # ------------------------------------------------------------------ #
    def backfill_instruments(
        self,
        entries_or_keys: list[dict[str, Any] | str],
        *,
        reason: str = "adhoc",
        include_historical: bool | None = None,
        lookback_days: int | None = None,
        update_replay: bool = False,
        send_telegram: bool = False,
    ) -> dict[str, Any]:
        """Seed the SR engine for a **specific set** of instruments.

        Unlike ``run()`` this method:

          * does **not** consult ``should_run()`` — it is intended to
            be invoked mid-session for instruments that just became
            active (e.g. the newly-isolated opposite side);
          * does **not** consult ``is_ready()`` — partial mid-session
            seeds are the point;
          * does **not** mark the readiness latch (``update_replay=
            False`` by default), because the frozen replay series
            consumed by ``dummy_order_service`` is already built and
            must remain stable for the day;
          * accepts a mix of registry-entry dicts and bare
            ``instrument_key`` strings;
          * resolves bare keys through
            ``instrument_registry.get()``, and registers unknowns via
            ``instrument_registry.register_adhoc()`` before fetching.

        Uses the same ``_seed_one_instrument()`` helper as ``run()``,
        so candle fetch, pivot scan, and buffer seeding are
        byte-identical between the full-pipeline and ad-hoc paths.

        Parameters
        ----------
        entries_or_keys : list[dict | str]
            Registry entry dicts and/or instrument key strings.
        reason : str
            Free-form tag logged with the call and stored in
            ``last_status()``. Default ``"adhoc"``.
        include_historical : bool | None
            ``None`` → ``SR_BACKFILL_INCLUDE_HISTORICAL``.
        lookback_days : int | None
            ``None`` → ``LIVE_SR_CALC_DEFAULT_LOOKBACK_DAYS``.
        update_replay : bool
            Default ``False`` (do not touch the frozen replay series
            and do not bump the readiness latch).
        send_telegram : bool
            Default ``False``. Ad-hoc typically runs dozens of times
            per session — Telegram would flood.

        Never raises. Returns a summary dict shaped like ``run()``'s
        (status / instruments_total / instruments_seeded / candles_total
        / pivots_total / errors / elapsed_seconds / ready /
        snapshot_epoch / update_replay).
        """
        from services.instrument_registry import instrument_registry

        if include_historical is None:
            include_historical = bool(
                getattr(config, "SR_BACKFILL_INCLUDE_HISTORICAL", True)
            )
        if lookback_days is None:
            lookback_days = int(
                getattr(config, "LIVE_SR_CALC_DEFAULT_LOOKBACK_DAYS", 5)
            )
        workers = max(1, int(getattr(config, "SR_BACKFILL_MAX_WORKERS", 8)))

        # ---- Resolve inputs to registry-entry dicts ---------------- #
        entries: list[dict[str, Any]] = []
        for item in entries_or_keys or []:
            if isinstance(item, dict):
                entries.append(item)
                continue
            key = str(item or "").strip()
            if not key:
                continue
            entry = instrument_registry.get(key)
            if entry is None:
                try:
                    entry = instrument_registry.register_adhoc(key)
                except Exception:
                    logger.exception(
                        "[sr-backfill] register_adhoc failed for %s", key,
                    )
                    entry = None
            if entry:
                entries.append(entry)

        if not entries:
            return {
                "status": "no_resolvable_instruments",
                "reason": reason,
                "ran_at": _now_iso(),
                "instruments_total": len(entries_or_keys or []),
                "instruments_seeded": 0,
                "candles_total": 0,
                "pivots_total": 0,
                "errors": 0,
                "ready": is_ready(),
                "snapshot_epoch": snapshot_epoch(),
                "update_replay": update_replay,
            }

        t0 = _now_market()
        logger.info(
            "[sr-backfill] ADHOC START reason=%s instruments=%d "
            "workers=%d include_historical=%s lookback_days=%d "
            "update_replay=%s historical_source=%s",
            reason, len(entries), workers, include_historical,
            lookback_days, update_replay,
            str(getattr(config, "SR_BACKFILL_HISTORICAL_SOURCE", "auto")),
        )

        errors = 0
        candles_total = 0
        pivots_total = 0
        seeded_instruments = 0

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    _seed_one_instrument, e, include_historical, lookback_days,
                )
                for e in entries
            ]
            for fut in as_completed(futures):
                try:
                    r = fut.result()
                except Exception:
                    errors += 1
                    continue
                c = int(r.get("candles") or 0)
                p = int(r.get("pivots") or 0)
                if r.get("error"):
                    errors += 1
                if c > 0:
                    seeded_instruments += 1
                candles_total += c
                pivots_total += p

        elapsed = (_now_market() - t0).total_seconds()

        # ---- Readiness latch: NEVER touched unless explicitly asked -- #
        if seeded_instruments > 0 and update_replay:
            epoch = _mark_seeded()
            ready = True
        else:
            epoch = snapshot_epoch()
            ready = is_ready()

        summary = {
            "status": "ok" if errors == 0 else "partial",
            "reason": reason,
            "ran_at": _now_iso(),
            "instruments_total": len(entries),
            "instruments_seeded": seeded_instruments,
            "candles_total": candles_total,
            "pivots_total": pivots_total,
            "errors": errors,
            "elapsed_seconds": round(elapsed, 2),
            "include_historical": include_historical,
            "lookback_days": lookback_days,
            "ready": ready,
            "snapshot_epoch": epoch,
            "update_replay": update_replay,
        }
        logger.info(
            "[sr-backfill] ADHOC COMPLETE — instruments=%d seeded=%d "
            "candles=%d pivots=%d errors=%d elapsed=%.1fs epoch=%d",
            len(entries), seeded_instruments, candles_total,
            pivots_total, errors, elapsed, epoch,
        )

        if send_telegram:
            try:
                from services.telegram_service import telegram_service
                _DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                _THIN = "────────────────────────────────"
                msg = (
                    f"{_DIV}\n"
                    f"🎯 SR ADHOC BACKFILL · {reason.upper()}\n"
                    f"{_DIV}\n"
                    f"🕒 Time:           {_now_market().strftime('%H:%M:%S IST')}\n"
                    f"📊 Instruments:    {len(entries)}\n"
                    f"✅ Seeded:         {seeded_instruments}\n"
                    f"🕯️ Candles:        {candles_total}\n"
                    f"📐 Pivots:         {pivots_total}\n"
                    f"❌ Errors:         {errors}\n"
                    f"⏱️ Elapsed:        {round(elapsed, 1)}s\n"
                )
                level = "SUCCESS" if errors == 0 else "WARNING"
                telegram_service.send_message(
                    "SR Adhoc Backfill Completed", msg, level=level,
                )
            except Exception:
                logger.exception(
                    "[sr-backfill] Telegram notification failed"
                )

        self._last_status = summary
        return summary

    def register_and_backfill(
        self,
        keys: list[str] | str,
        *,
        reason: str = "adhoc",
        include_historical: bool | None = None,
        lookback_days: int | None = None,
    ) -> dict[str, Any]:
        """Convenience wrapper: register ad-hoc keys and seed them.

        Accepts a single instrument key or a list of keys. Missing keys
        are registered via ``instrument_registry.register_adhoc()`` by
        ``backfill_instruments()``. Uses ``update_replay=False`` (the
        frozen replay series is untouched), matching the isolated-
        instrument pipeline's expectations.

        Used by:
          * ``isolated_instrument_promotion_service.on_isolation_triggered()``
          * ``isolated_instrument_promotion_service._prepare_isolation_pair()``

        Never raises.
        """
        if isinstance(keys, str):
            keys = [keys]
        keys = [str(k).strip() for k in (keys or []) if k]
        return self.backfill_instruments(
            keys,
            reason=reason,
            include_historical=include_historical,
            lookback_days=lookback_days,
            update_replay=False,
            send_telegram=False,
        )

    # ------------------------------------------------------------------ #
    # Readiness / status                                                 #
    # ------------------------------------------------------------------ #
    def is_ready(self) -> bool:
        """True when today's backfill has completed in this process.

        Thin proxy for the module-level ``is_ready()`` so callers that
        already hold the service singleton do not need to import the
        free function.
        """
        return is_ready()

    def is_ready_for_today(self) -> bool:
        """Alias for ``is_ready()`` — contractual name.

        This is the exact name looked up by:

          * ``services/scheduler_service.sr_backfill_ready()`` —
            ``getattr(sr_backfill_service, "is_ready_for_today", None)``
          * ``services/dummy_order_service._upstream_ready()`` —
            same pattern
          * ``services/sandbox_execution_service.SandboxExecutionService._gate()``
            — same pattern, with a fall-back to ``is_ready()`` when
            this method is absent (so the executor raises
            ``sr_readiness_probe_missing`` only if BOTH names are
            missing).

        Before this alias existed, both the scheduler and the
        dummy-order service fell into their "missing probe" branch:
        the scheduler skipped every ``dummy_order_tracker`` tick with
        ``missing_probe``, and the dummy-order service proceeded
        without the readiness gate (logged as a WARNING). Both bugs
        close as soon as this method resolves — the underlying latch
        (``is_ready()``) was already working; only the name was
        missing.

        Semantics are identical to ``is_ready()``; both read the same
        ``_SEEDED_DAY`` module state under the same lock. Prefer either
        name; they cannot diverge.
        """
        return is_ready_for_today()

    def snapshot_epoch(self) -> int:
        """Current snapshot epoch — see module-level ``snapshot_epoch()``."""
        return snapshot_epoch()

    def clear_seed_latch(self) -> None:
        """Reset the readiness latch and epoch — proxy for
        module-level ``clear_seed_latch()``."""
        clear_seed_latch()

    def last_status(self) -> dict[str, Any]:
        """Return the most recent ``run()`` / ``backfill_instruments()``
        summary (copy)."""
        return dict(self._last_status)

    def status(self) -> dict[str, Any]:
        """Full status: readiness latch + epoch + last-run summary.

        Superset of ``last_status()``. Consumers that only need the
        last run can keep using ``last_status()``; new callers that
        want to reason about "is the engine warm?" should read
        ``ready`` and ``snapshot_epoch`` from here.
        """
        return {
            **seed_status(),
            "last_run": dict(self._last_status),
        }


sr_backfill_service = SRBackfillService()