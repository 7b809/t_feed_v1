# services/live_sr_calc_service.py
"""On-demand, stateless SR calculation — LIVE mode, never reads saved data.

This service is fundamentally different from
``services.live_sr_levels_service``:

    * NO per-instrument state.
    * NEVER reads ``data/<...>/live_sr_levels.json`` **by default**.
    * NEVER writes anything to disk.
    * Every call recomputes from fresh data and returns the result.

Per-minute pivot cache (2026-11-05)
----------------------------------
An **opt-in** per-minute cache is exposed via
``calculate_pivots_cached(instrument_key, left, right)`` for callers
that want to avoid redundant intraday-API fetches on every 30 s
scheduler tick.

The cache key is ``(instrument_key, left_bars, right_bars)`` and the
TTL is one minute.  Within a single minute, every call with the same
key returns **byte-identical** level dicts (``cached=True``); at the
next minute boundary the entry is invalidated and the pipeline
recomputes from fresh data.  This gives determinism *within* a minute
while still refreshing *once per minute*.

The base methods (``calculate``, ``calculate_dual``,
``build_series_for_day``, ``get_candle_close_at*``) remain fully
stateless and always recompute from fresh data — only
``calculate_pivots_cached`` opts into the cache.

When to use it
~~~~~~~~~~~~~~
The 2026-11-05 ``dummy_order_service`` tick loop calls the ``(15, 15)``
pivot scan every 30 s per open POST-1130 order (see
``_update_post_1130_order``).  **That path currently reads from
``live_sr_levels_service._states``** (the live engine's in-memory
buffer, which is already fast and consistent) and therefore does
**not** need this cache.  The cache exists for callers that would
otherwise hit the intraday API twice per minute per instrument — e.g.
a future exit-tracker rewrite that prefers fresh-from-API data.

When to avoid it
~~~~~~~~~~~~~~~~
    * **Admin / replay tools** — pass through ``calculate()`` or
      ``calculate_dual()`` directly; those always compute fresh.
    * **First-fetch races** — if the very first call in a minute
      lands before the API has published the current minute's candle,
      the cache stores the incomplete series for the rest of that
      minute.  Callers with that risk should use
      ``force_refresh=True`` or read the in-memory buffer via
      ``live_sr_levels_service``.

G1 v4.1 — DETERMINISM CONTRACT
------------------------------
Two new methods exist so that callers (``dummy_order_service`` in
particular) can compute entry / stop / target against ONE fixed,
reproducible candle series instead of mixing three mutable sources:

    ``build_series_for_day(instrument_key, lookback_days=None)``
        → merged historical + intraday series for the day.  This is
          the *canonical* input for order-entry resolution and candle
          replay.  Guarantees internal consistency (entry, stop,
          target all see the same bars).

    ``get_candle_close_at_from_series(series, iso_time)``
        → pure in-memory lookup against a pre-built series.  No API
          calls, no disk reads.  Returns ``(price, source)``.

Additionally, two behaviour changes close the "silent fallback to the
newest available bar" class of bugs:

    * ``get_first_intraday_candle`` now verifies that the API's first
      candle actually lands on the expected session-open minute (09:15
      IST by default).  If the intraday window has been truncated
      (mid-day restart, API glitch, trimmed buffer), the method
      returns ``None`` instead of silently returning a later bar as
      if it were the OR candle.

    * ``get_candle_close_at`` continues to return ``None`` for an
      absent minute (unchanged).  Callers must treat ``None`` as
      "defer / unresolved", never as "fall through to live LTP".

Post-11:30 dual-configuration (2026-11-05)
------------------------------------------
The post-11:30 isolated-instrument strategy uses **two different**
pivot windows against the SAME candle series:

    * ENTRY confirmation window  → ``(15, 30)``  (wide right edge —
                                    fewer false triggers after midday
                                    chop)
    * EXIT  confirmation window  → ``(15, 15)``  (symmetric — fast
                                    reaction to the post-entry
                                    resistance that closes the trade)

``calculate_dual()`` builds the merged series ONCE and runs both
pivot passes in a single call, so entry and exit use byte-identical
candle input.  This is the recommended path for the post-11:30
strategy.  The single-config ``calculate()`` remains unchanged so
existing consumers (routes, dashboards, replay tools) keep working.

Every emitted level dict is now enriched with the strategy-facing
shape requested by the post-11:30 spec:

    {
        # ... existing fields (price, level, pivot_time, confirmed_time,
        #     confirmed_time_price, pivot_bar_index, confirmed_bar_index,
        #     left_bars, right_bars, is_today) are preserved verbatim so
        #     no existing consumer breaks ...

        "level_price":       471.65,        # float, numeric price
        "type":              "resistance",  # "support" | "resistance"
        "pivot_time":        "<iso>",       # pivot candle timestamp
        "confirmation_time": "<iso>",       # confirming candle timestamp
        "status":            "confirmed",   # always "confirmed"
    }

The historical field named ``level`` intentionally REMAINS the type
string ("support" / "resistance") — ``_mark_is_today``, the sort
comparators and the ``nearest`` computation all key on it, so
changing it to a float would break every consumer.  Use
``level_price`` when you need the numeric price, or ``price`` (the
pre-existing name) which is the same value.  ``confirmation_time``
is an alias of the pre-existing ``confirmed_time``.

Opt-in `live_sr` block
----------------------
Setting ``include_live_sr=True`` (default ``False``) asks the service to
ALSO read the persisted ``data/<...>/live_sr_levels.json`` — the file
maintained by ``live_sr_levels_service`` — and attach it to the response
under a **new top-level ``live_sr`` key**.

The persisted levels are deliberately **NOT merged into ``levels``**:

    * ``levels`` is always the "fresh from historical + intraday"
      calculation — the same live-data-only guarantee as when
      ``include_live_sr`` is off.
    * ``live_sr`` is a separate, parallel view of what the running
      engine has actually ticked and persisted today.

Callers who want both views side-by-side get them without cross-
contamination; callers who want to merge can do so in their own code
based on their own semantics (fresh != live).

Public loader helpers
---------------------
Public methods exist specifically so that other services can reuse this
service's candle loaders without duplication:

    ``load_historical_candles_for_entry(entry, lookback_days)``
        → historical candles (list only)
    ``fetch_intraday_candles_for_entry(instrument_key)``
        → today's intraday candles (list only)
    ``merge_candles(historical, intraday)``
        → merged list (intraday wins on minute collision)
    ``build_series_for_day(instrument_key, lookback_days=None)``
        → merged series for the day (historical + intraday) — G1 v4.1
    ``get_candle_close_at(instrument_key, iso_time)``
        → close of the 1-min candle at that minute (or None)
    ``get_candle_close_at_from_series(series, iso_time)``
        → same lookup, against a pre-built series — G1 v4.1
    ``get_first_intraday_candle(instrument_key, expected_first_minute=None)``
        → first intraday candle, verified against the expected
          session-open minute (or None) — G1 v4.1
    ``calculate_pivots_cached(instrument_key, left, right, ...)``
        → per-minute-cached pivot scan (opt-in) — 2026-11-05

The first three exist so the periodic refresh job
(``live_sr_refresh_from_api``, see
``services/scheduler_service.py``) can produce exactly the same
historical + intraday merge the calc endpoint uses, and feed it into
``live_sr_levels_service.seed_candles``.

The close-at helpers exist so the entry-signal / isolation engines can
resolve a historical candle close without re-implementing the
minute-bucket matching logic (see
``strategy_entry_signal_service._entry_price_for`` and
``level_isolated_instrument_service._maybe_self_heal``).

INTRAday by default
-------------------
The endpoint is an "intraday SR" API — its primary use case is "what
SR levels have been confirmed on this instrument TODAY?". Therefore
``only_today`` defaults to **True**. The response is filtered to levels
whose ``confirmed_time`` falls on the current IST calendar day.

To inspect the full merged window (historical + today), pass
``only_today=false`` explicitly. The response then carries
``mode = "full"`` instead of ``"intraday"``.

Historical candles are still fetched in intraday mode — a pivot that
forms at 09:15 needs 15 bars *before* it to be confirmed, and those
bars come from yesterday's session. The filter is on the OUTPUT, not
the INPUT.

Data sources (merged by minute; intraday wins on collision):

    1. Historical 1-min candles from the backup tree::

           data/index/<underlying>/candles.json          (indexes)
           data/<underlying>/<strike>_<TYPE>/candles.json (options)

       Looked up via ``entry["candles_path"]`` (set by
       ``services.instrument_registry``); falls back to the canonical
       layout above when the registry entry has no path.

    2. Today's intraday 1-min candles from Upstox::

           upstox_client.HistoryV3Api.get_intra_day_candle_data(
               instrument_key, "minutes", "1"
           )

    (3. OPTIONAL, only when ``include_live_sr=true``) the persisted
        ``data/<...>/live_sr_levels.json`` — attached under ``live_sr``,
        never merged into ``levels``.)

Historical-file format
----------------------
The candle-backup writer produces files shaped like::

    {
      "updated_at":     "2026-10-25T17:17:24.284028+00:00",
      "relative_path":  "nifty/22400_CE/candles.json",
      "count":          2695,
      "candles": [
        {
          "timestamp": "2026-10-14T09:15:00+05:30",
          "open":      471.65,
          "high":      471.8,
          "low":       444.65,
          "close":     446.45,
          "volume":    12740,
          "oi":        1412970
        },
        ...
      ]
    }

Candle normalisation
--------------------
Historical files in the backup tree come in several shapes across
deployments. ``_normalise_candle`` accepts all of these:

    * list-of-lists   : ``[ts, o, h, l, c, v?]``  (Upstox native)
    * dict-per-candle : ``{"time"|"ts"|"timestamp"|"datetime"|
                            "start_time"|"t": ...,
                            "open"|"o": ...,
                            "high"|"h": ...,
                            "low"|"l": ...,
                            "close"|"c": ...,
                            "volume"|"v"|"vol"|"vtt": ...}``
    * dict-per-candle with split date + time
                      : ``{"date": "YYYY-MM-DD", "time": "HH:MM:SS", ...}``

Wrapper unwrapping (``_extract_candles_from_payload``) accepts:

    * bare ``[...]``
    * ``{"candles": [...]}``               <-- candle-backup standard
    * ``{"data": {"candles": [...]}}``
    * ``{"data": [...]}``

Diagnostics
-----------
``_load_historical_candles`` returns a ``meta`` dict that always carries:

    ``path``, ``candles``, ``candles_raw``,
    ``candles_rejected_format``, ``candles_rejected_cutoff``,
    ``declared_count`` (from the file's own ``count`` field),
    ``count_mismatch`` (true when declared_count != len(candles)),
    ``file_updated_at``, ``relative_path``,
    ``first_candle``, ``last_candle``, ``error``

``_load_live_sr_state`` returns a ``live_sr`` dict that carries:

    ``path``, ``available``, ``date``, ``is_today``,
    ``candle_count``, ``level_count``, ``counts``,
    ``first_level_time``, ``last_level_time``, ``last_candle``,
    ``updated_at``, ``levels``, ``error``

Pivot detection mirrors ``LiveSRLevelsService._scan_all_pivots``
exactly — the strict TradingView-compatible rule:

    resistance pivot at i  iff  C[i].high > C[j].high
                                for j in [i-left, i-1]
                                and   j in [i+1, i+right]

    support    pivot at i  iff  C[i].low  < C[j].low
                                for j in [i-left, i-1]
                                and   j in [i+1, i+right]

A pivot at index ``i`` is confirmed at bar ``i + right_bars``.
Every confirmed level is retained for the day — there is no "broken" /
"intact" distinction; a level that price has crossed stays on the
chart, exactly as TradingView renders pivot lines.

Levels are returned **sorted by price ascending** (supports and
resistances intermixed), matching TradingView.

Response is deterministic given the same underlying data. The caller
can therefore poll this endpoint without risk of state drift.

See ``routes/live_sr_calc_routes.py`` for the HTTP surface.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger
from services.instrument_registry import instrument_registry
from services.normalization import (
    normalize_strike,
    normalize_strike_type,
    normalize_underlying,
)

logger = get_logger(__file__)

# --------------------------------------------------------------------------- #
# Accepted field-name variants                                                #
# --------------------------------------------------------------------------- #
_TIME_FIELDS = ("timestamp", "datetime", "ts", "start_time", "t", "time")
_OPEN_FIELDS = ("open", "o")
_HIGH_FIELDS = ("high", "h")
_LOW_FIELDS = ("low", "l")
_CLOSE_FIELDS = ("close", "c")
_VOLUME_FIELDS = ("volume", "v", "vol", "vtt")

# --------------------------------------------------------------------------- #
# Post-11:30 dual-configuration defaults (kept as module constants so the     #
# strategy service and admin routes can reference them without duplicating   #
# the literals; see ``core.config.POST_1130_ENTRY_SR_CONFIG`` /             #
# ``POST_1130_EXIT_SR_CONFIG`` for the operator-facing knobs).              #
# --------------------------------------------------------------------------- #
_ENTRY_SR_LEFT_BARS = 15
_ENTRY_SR_RIGHT_BARS = 30
_EXIT_SR_LEFT_BARS = 15
_EXIT_SR_RIGHT_BARS = 15

# --------------------------------------------------------------------------- #
# Per-minute pivot cache (2026-11-05)                                         #
#                                                                             #
# Cache key:  (instrument_key, left_bars, right_bars)                         #
# Value:      {minute_bucket, expires_at (monotonic), cached_at, payload}     #
#                                                                             #
# The minute_bucket is derived from wall-clock time (int(now.timestamp() //   #
# 60)).  Within a single minute, all calls with the same key return the SAME  #
# cached payload.  At the next minute boundary the bucket changes and the    #
# payload is recomputed.                                                      #
#                                                                             #
# TTL is a second-level safety net for the same-minute case (in case of a     #
# clock bump or a caller explicitly passing a stale ``minute_bucket``).       #
# --------------------------------------------------------------------------- #
_PIVOT_CACHE_TTL_SECONDS = 60.0
_PIVOT_CACHE_MAX_ENTRIES = 5000       # hard cap — LRU evicts the oldest
_PIVOT_CACHE_MAX_AGE_BUCKETS = 10     # prune entries older than 10 minutes

# --------------------------------------------------------------------------- #
# Time / path helpers                                                         #
# --------------------------------------------------------------------------- #
def _market_tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.MARKET_TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Kolkata")

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _today_iso() -> str:
    return datetime.now(_market_tz()).date().isoformat()

def _parse_time(ts_val: Any) -> datetime | None:
    """Parse a timestamp value into a tz-aware datetime, or return None."""
    if ts_val is None or ts_val == "":
        return None

    try:
        n = float(ts_val)
        if n > 1e10:  # millis
            return datetime.fromtimestamp(n / 1000.0, tz=_market_tz())
        if n > 1e9:  # seconds
            return datetime.fromtimestamp(n, tz=_market_tz())
    except (TypeError, ValueError):
        pass

    try:
        dt = datetime.fromisoformat(str(ts_val).replace("Z", "+00:00"))
    except Exception:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_market_tz())
    return dt

def _first_present(raw: dict[str, Any], fields: tuple[str, ...]) -> Any:
    for f in fields:
        if f in raw and raw[f] is not None:
            return raw[f]
    return None

def _extract_timestamp_from_dict(raw: dict[str, Any]) -> Any:
    """Extract a timestamp from a dict-per-candle entry."""
    for field in _TIME_FIELDS:
        v = raw.get(field)
        if v is not None:
            if field == "time":
                date_v = raw.get("date")
                if date_v is not None:
                    date_s = str(date_v)
                    if "T" not in date_s and " " not in date_s:
                        return f"{date_s}T{str(v)}"
            return v

    date_v = raw.get("date")
    time_v = raw.get("time")
    if date_v is not None and time_v is not None:
        date_s = str(date_v)
        if "T" in date_s or " " in date_s:
            return date_s
        return f"{date_s}T{str(time_v)}"

    if date_v is not None:
        return date_v
    if time_v is not None:
        return time_v
    return None

def _normalise_candle(raw: Any) -> dict[str, Any] | None:
    """Return ``{time, ts_ms, bucket, open, high, low, close, volume}``."""
    # ---- List-of-lists form ----------------------------------------- #
    if isinstance(raw, (list, tuple)) and len(raw) >= 5:
        dt = _parse_time(raw[0])
        if dt is None:
            return None
        try:
            return {
                "time": dt.isoformat(),
                "ts_ms": int(dt.timestamp() * 1000),
                "bucket": int(dt.timestamp() // 60),
                "open": float(raw[1]),
                "high": float(raw[2]),
                "low": float(raw[3]),
                "close": float(raw[4]),
                "volume": float(raw[5]) if len(raw) > 5 else 0.0,
            }
        except (IndexError, TypeError, ValueError):
            return None

    # ---- Dict form -------------------------------------------------- #
    if isinstance(raw, dict):
        ts_val = _extract_timestamp_from_dict(raw)
        dt = _parse_time(ts_val)
        if dt is None:
            return None

        o = _first_present(raw, _OPEN_FIELDS)
        h = _first_present(raw, _HIGH_FIELDS)
        l = _first_present(raw, _LOW_FIELDS)
        c = _first_present(raw, _CLOSE_FIELDS)
        v = _first_present(raw, _VOLUME_FIELDS)
        if v is None:
            v = 0.0

        if any(x is None for x in (o, h, l, c)):
            return None

        try:
            return {
                "time": dt.isoformat(),
                "ts_ms": int(dt.timestamp() * 1000),
                "bucket": int(dt.timestamp() // 60),
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(v),
            }
        except (TypeError, ValueError):
            return None

    return None

def _describe_raw(raw: Any) -> dict[str, Any]:
    try:
        if isinstance(raw, dict):
            return {
                "type": "dict",
                "keys": [str(k) for k in list(raw.keys())[:20]],
            }
        if isinstance(raw, (list, tuple)):
            return {"type": "list", "length": len(raw)}
        return {"type": type(raw).__name__}
    except Exception:
        return {"type": "unknown"}

def _default_session_open_time() -> dtime:
    """Return the configured first-minute for the OR window (default 09:15)."""
    raw = str(
        getattr(config, "OPENING_RANGE_FIRST_MINUTE", "09:15")
    ).strip()
    try:
        hh, mm = raw.split(":")
        return dtime(int(hh), int(mm))
    except (ValueError, AttributeError):
        return dtime(9, 15)

# --------------------------------------------------------------------------- #
# Level enrichment (2026-11-05)                                               #
# --------------------------------------------------------------------------- #
def _enrich_level(level: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``level`` with the post-11:30 spec fields.

    Existing fields (``price``, ``level`` [type string], ``pivot_time``,
    ``confirmed_time``, ``confirmed_time_price``, ``pivot_bar_index``,
    ``confirmed_bar_index``, ``left_bars``, ``right_bars``, ``is_today``)
    are preserved verbatim so no existing consumer breaks.

    Added fields:

        ``level_price``        — float, numeric price (== ``price``)
        ``type``               — "support" | "resistance" (== ``level``)
        ``confirmation_time``  — alias of ``confirmed_time``
        ``status``             — always ``"confirmed"`` (a level that
                                 reaches this method has already passed
                                 the ``i + right_bars`` confirmation
                                 check inside ``_compute_pivots``)
    """
    if not isinstance(level, dict):
        return {}

    out = dict(level)

    # Numeric price — prefer the existing ``price`` field; fall back to
    # ``level_price`` if a caller somehow supplied it pre-enriched.
    try:
        price_val = out.get("price")
        if price_val is None:
            price_val = out.get("level_price")
        out["level_price"] = float(price_val) if price_val is not None else None
    except (TypeError, ValueError):
        out["level_price"] = None

    # Type alias — the historical ``level`` field is the type string.
    out["type"] = out.get("type") or out.get("level")

    # Confirmation-time alias.
    out["confirmation_time"] = (
        out.get("confirmation_time") or out.get("confirmed_time")
    )

    # Status — any level emitted by ``_compute_pivots`` is confirmed by
    # construction (the pivot has ``right_bars`` bars after it).
    out.setdefault("status", "confirmed")

    return out

# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class LiveSRCalcService:
    """On-demand SR calculator. Stateless and safe to call concurrently.

    (2026-11-05) A per-minute pivot cache is available via
    ``calculate_pivots_cached(...)`` for callers that want to avoid
    redundant intraday-API fetches on every 30 s tick.  The base
    ``calculate()`` / ``calculate_dual()`` methods remain fully
    stateless and always compute from fresh data.
    """

    def __init__(self) -> None:
        # Per-minute pivot cache (opt-in via calculate_pivots_cached).
        #
        # key   = (instrument_key, left_bars, right_bars)
        # value = {
        #     "minute_bucket": int,        # wall-clock minute of the result
        #     "expires_at":    float,      # time.monotonic() deadline
        #     "cached_at":     float,      # time.monotonic() write time
        #     "payload":       dict,       # the returned result dict
        # }
        self._pivot_cache_lock = threading.RLock()
        self._pivot_cache: dict[tuple[str, int, int], dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # Public entry point                                                 #
    # ------------------------------------------------------------------ #
    def calculate(
        self,
        *,
        instrument_key: str | None = None,
        underlying: str | None = None,
        strike: str | int | float | None = None,
        strike_type: str | None = None,
        left_bars: int | None = None,
        right_bars: int | None = None,
        lookback_days: int | None = None,
        only_today: bool = True,       # <-- intraday by default
        include_candles: bool = False,
        include_live_sr: bool = False, # <-- opt-in live_sr block
        debug: bool = False,
    ) -> dict[str, Any]:
        # ---- 1. Resolve entry --------------------------------------- #
        entry, selector_source = self._resolve_entry(
            instrument_key, underlying, strike, strike_type,
        )
        if entry is None:
            return {
                "status": "not_found",
                "error": "instrument_not_found",
                "selector": {
                    "instrument_key": str(instrument_key) if instrument_key else None,
                    "underlying": normalize_underlying(underlying) or underlying,
                    "strike": normalize_strike(strike) or strike,
                    "type": normalize_strike_type(strike_type) or strike_type,
                },
                "generated_at": _now_iso(),
            }

        # ---- 2. Resolve params -------------------------------------- #
        left = max(1, min(100, left_bars if left_bars is not None else config.LIVE_SR_LEFT_BARS))
        right = max(1, min(100, right_bars if right_bars is not None else config.LIVE_SR_RIGHT_BARS))
        lb = max(1, min(
            30,
            lookback_days if lookback_days is not None
            else config.LIVE_SR_CALC_DEFAULT_LOOKBACK_DAYS,
        ))

        if left + right + 1 > config.LIVE_SR_MAX_CANDLES:
            return {
                "status": "invalid_params",
                "error": (
                    f"left_bars + right_bars + 1 = {left + right + 1} "
                    f"exceeds LIVE_SR_MAX_CANDLES ({config.LIVE_SR_MAX_CANDLES})"
                ),
                "params": {
                    "left_bars": left,
                    "right_bars": right,
                    "max_candles": config.LIVE_SR_MAX_CANDLES,
                },
                "generated_at": _now_iso(),
            }

        # ---- 3. Fetch both sources ---------------------------------- #
        historical, historical_meta = self._load_historical_candles(
            entry, lb, debug=debug,
        )
        intraday, intraday_meta = self._fetch_intraday_candles(entry["instrument_key"])

        # ---- 4. Merge ----------------------------------------------- #
        merged = self._merge_by_minute(historical, intraday)
        if not merged:
            return {
                "status": "no_data",
                "error": "no_candles_from_either_source",
                "instrument_key": entry["instrument_key"],
                "mode": "intraday" if only_today else "full",
                "today_iso": _today_iso(),
                "params": {
                    "left_bars": left,
                    "right_bars": right,
                    "lookback_days": lb,
                    "only_today": only_today,
                    "include_candles": include_candles,
                    "include_live_sr": include_live_sr,
                    "debug": debug,
                    "max_candles": config.LIVE_SR_CALC_MAX_CANDLES,
                },
                "sources": {
                    "historical": historical_meta,
                    "intraday": intraday_meta,
                },
                "live_sr": (
                    self._load_live_sr_state(entry)
                    if include_live_sr else None
                ),
                "generated_at": _now_iso(),
            }

        # ---- 5. Cap ------------------------------------------------- #
        merged_before_cap = len(merged)
        max_candles = config.LIVE_SR_CALC_MAX_CANDLES
        if len(merged) > max_candles:
            merged = merged[-max_candles:]

        # ---- 6. Enough data for a pivot? ---------------------------- #
        if len(merged) < left + right + 1:
            return {
                "status": "insufficient_data",
                "error": (
                    f"not enough candles to detect any pivots — "
                    f"need {left + right + 1}, have {len(merged)}"
                ),
                "instrument_key": entry["instrument_key"],
                "mode": "intraday" if only_today else "full",
                "today_iso": _today_iso(),
                "candle_count": len(merged),
                "candles_needed": left + right + 1,
                "params": {
                    "left_bars": left,
                    "right_bars": right,
                    "lookback_days": lb,
                    "only_today": only_today,
                    "include_candles": include_candles,
                    "include_live_sr": include_live_sr,
                    "debug": debug,
                    "max_candles": max_candles,
                },
                "sources": {
                    "historical": historical_meta,
                    "intraday": intraday_meta,
                },
                "live_sr": (
                    self._load_live_sr_state(entry)
                    if include_live_sr else None
                ),
                "generated_at": _now_iso(),
            }

        # ---- 7. Compute pivots -------------------------------------- #
        all_levels = self._compute_pivots(merged, left, right)

        # ---- 8. Mark is_today + sort ascending by price ------------- #
        self._mark_is_today(all_levels)
        all_levels.sort(key=lambda l: l["price"])

        total_before_filters = len(all_levels)

        # ---- 9. Apply caller filters -------------------------------- #
        levels = all_levels
        if only_today:
            levels = [l for l in levels if l["is_today"]]
        # Ascending price order — TradingView behaviour, supports and
        # resistances intermixed.
        levels.sort(key=lambda l: l["price"])

        # ---- 10. Current price + nearest hints ---------------------- #
        current_price = merged[-1]["close"] if merged else None

        nearest_resistance: dict[str, Any] | None = None
        nearest_support: dict[str, Any] | None = None
        if current_price is not None:
            resistances_above = sorted(
                (
                    l for l in all_levels
                    if l["level"] == "resistance" and l["price"] > current_price
                ),
                key=lambda x: x["price"],
            )
            supports_below = sorted(
                (
                    l for l in all_levels
                    if l["level"] == "support" and l["price"] < current_price
                ),
                key=lambda x: x["price"],
                reverse=True,
            )
            nearest_resistance = resistances_above[0] if resistances_above else None
            nearest_support = supports_below[0] if supports_below else None

        # ---- 11. Optional live_sr block ----------------------------- #
        live_sr_state: dict[str, Any] | None = None
        if include_live_sr:
            live_sr_state = self._load_live_sr_state(entry)

        # ---- 12. Warnings ------------------------------------------- #
        warnings: list[str] = []
        if historical_meta.get("error"):
            warnings.append(f"historical: {historical_meta['error']}")
        if intraday_meta.get("error"):
            warnings.append(f"intraday: {intraday_meta['error']}")
        if merged_before_cap > max_candles:
            warnings.append(
                f"merged candles capped from {merged_before_cap} to {max_candles}"
            )
        if historical_meta.get("count_mismatch"):
            warnings.append(
                f"historical: declared_count={historical_meta.get('declared_count')} "
                f"but file contains {historical_meta.get('candles_raw')} entries"
            )

        # Intraday-mode specific hints — make "0 today" obvious.
        if only_today and len(levels) == 0:
            if total_before_filters == 0:
                warnings.append(
                    "intraday mode: no pivots detected in the merged window"
                )
            else:
                warnings.append(
                    f"intraday mode: 0 levels confirmed today, "
                    f"{total_before_filters} historical level(s) available "
                    f"(pass only_today=false to include them)"
                )
        elif only_today and len(levels) < total_before_filters:
            warnings.append(
                f"intraday mode: filtered out "
                f"{total_before_filters - len(levels)} historical level(s) "
                f"(pass only_today=false to include them)"
            )

        # live_sr specific hints.
        if include_live_sr and isinstance(live_sr_state, dict):
            if live_sr_state.get("error"):
                warnings.append(f"live_sr: {live_sr_state['error']}")
            elif not live_sr_state.get("is_today"):
                warnings.append(
                    f"live_sr: file is dated {live_sr_state.get('date')} "
                    f"(not today — {live_sr_state.get('today_iso')})"
                )
            elif not config.LIVE_SR_ENABLED:
                warnings.append(
                    "live_sr: LIVE_SR_ENABLED is false — file may be stale"
                )

        # ---- 13. Build response ------------------------------------- #
        return {
            "status": "ok",
            "mode": "intraday" if only_today else "full",
            "today_iso": _today_iso(),
            "instrument_key": entry["instrument_key"],
            "trading_symbol": entry.get("trading_symbol"),
            "underlying": entry.get("underlying"),
            "strike": entry.get("strike_price"),
            "type": entry.get("strike_type"),
            "expiry": entry.get("expiry"),
            "is_index": bool(entry.get("is_index")),
            "selector_source": selector_source,
            "params": {
                "left_bars": left,
                "right_bars": right,
                "lookback_days": lb,
                "only_today": only_today,
                "include_candles": include_candles,
                "include_live_sr": include_live_sr,
                "debug": debug,
                "max_candles": max_candles,
            },
            "sources": {
                "historical": historical_meta,
                "intraday": intraday_meta,
            },
            "merged": {
                "candle_count": len(merged),
                "candle_count_before_cap": merged_before_cap,
                "first_candle": merged[0]["time"] if merged else None,
                "last_candle": merged[-1]["time"] if merged else None,
                "current_price": current_price,
            },
            "levels": levels,
            "counts": {
                "total": len(levels),
                "total_unfiltered": len(all_levels),
                "resistance": sum(1 for l in levels if l["level"] == "resistance"),
                "support": sum(1 for l in levels if l["level"] == "support"),
                "today": sum(1 for l in levels if l["is_today"]),
            },
            "nearest": {
                "resistance_above": nearest_resistance,
                "support_below": nearest_support,
            },
            "live_sr": live_sr_state,
            "warnings": warnings,
            "candles": merged if include_candles else None,
            "generated_at": _now_iso(),
        }

    # ------------------------------------------------------------------ #
    # Dual-configuration entry point (post-11:30 strategy)               #
    # ------------------------------------------------------------------ #
    def calculate_dual(
        self,
        *,
        instrument_key: str | None = None,
        underlying: str | None = None,
        strike: str | int | float | None = None,
        strike_type: str | None = None,
        entry_config: tuple[int, int] | None = None,
        exit_config: tuple[int, int] | None = None,
        lookback_days: int | None = None,
        only_today: bool = True,
        include_candles: bool = False,
        include_live_sr: bool = False,
        debug: bool = False,
    ) -> dict[str, Any]:
        """Compute SR levels for BOTH entry and exit windows in one pass.

        The merged candle series is built ONCE and reused for both
        pivot passes, which is the whole point of this method — the
        post-11:30 strategy needs ``(15, 30)`` for entry and
        ``(15, 15)`` for exit against byte-identical input.

        Defaults:

            * ``entry_config`` → ``(15, 30)`` — matches
              ``config.POST_1130_ENTRY_SR_CONFIG``.
            * ``exit_config``  → ``(15, 15)`` — matches
              ``config.POST_1130_EXIT_SR_CONFIG``.

        Override either tuple to compute against a custom window.
        Passing the same tuple for both configs is valid — the two
        result blocks will then be identical (useful for debugging
        the shared series).

        Response shape (illustrative)::

            {
                "status": "ok",
                "instrument_key": "NSE_FO|53942",
                "trading_symbol": "...",
                "mode": "intraday",
                "today_iso": "2026-11-05",
                "merged": { ...shared series metadata... },
                "sources": { "historical": {...}, "intraday": {...} },
                "entry": {
                    "params": {"left_bars": 15, "right_bars": 30},
                    "levels": [ ...enriched level dicts... ],
                    "counts": {...},
                    "nearest": {...},
                },
                "exit": {
                    "params": {"left_bars": 15, "right_bars": 15},
                    "levels": [ ...enriched level dicts... ],
                    "counts": {...},
                    "nearest": {...},
                },
                "live_sr": null | {...},
                "warnings": [...],
                "generated_at": "2026-11-05T...",
            }

        Every level dict in ``entry.levels`` / ``exit.levels`` carries the
        post-11:30 spec fields (``level_price``, ``type``, ``pivot_time``,
        ``confirmation_time``, ``status``) in addition to the historical
        fields (``price``, ``level``, ``confirmed_time``, ...).

        A level cannot appear in ``entry.levels`` unless it is confirmed
        within the wider ``(15, 30)`` window; the same level MAY appear
        in ``exit.levels`` confirmed earlier via the tighter ``(15, 15)``
        window.  This asymmetry is intentional — the entry filter is
        deliberately stricter than the exit filter.
        """
        # ---- Resolve configs ---------------------------------------- #
        if entry_config is None:
            entry_config = (
                int(getattr(config, "POST_1130_ENTRY_SR_CONFIG", (15, 30))[0]),
                int(getattr(config, "POST_1130_ENTRY_SR_CONFIG", (15, 30))[1]),
            )
        if exit_config is None:
            exit_config = (
                int(getattr(config, "POST_1130_EXIT_SR_CONFIG", (15, 15))[0]),
                int(getattr(config, "POST_1130_EXIT_SR_CONFIG", (15, 15))[1]),
            )

        entry_left = max(1, min(100, int(entry_config[0])))
        entry_right = max(1, min(100, int(entry_config[1])))
        exit_left = max(1, min(100, int(exit_config[0])))
        exit_right = max(1, min(100, int(exit_config[1])))

        # ---- 1. Resolve entry --------------------------------------- #
        entry, selector_source = self._resolve_entry(
            instrument_key, underlying, strike, strike_type,
        )
        if entry is None:
            return {
                "status": "not_found",
                "error": "instrument_not_found",
                "selector": {
                    "instrument_key": str(instrument_key) if instrument_key else None,
                    "underlying": normalize_underlying(underlying) or underlying,
                    "strike": normalize_strike(strike) or strike,
                    "type": normalize_strike_type(strike_type) or strike_type,
                },
                "generated_at": _now_iso(),
            }

        lb = max(1, min(
            30,
            lookback_days if lookback_days is not None
            else config.LIVE_SR_CALC_DEFAULT_LOOKBACK_DAYS,
        ))

        # Validate both configs against the max-candles cap.
        for label, l_bars, r_bars in (
            ("entry", entry_left, entry_right),
            ("exit", exit_left, exit_right),
        ):
            if l_bars + r_bars + 1 > config.LIVE_SR_MAX_CANDLES:
                return {
                    "status": "invalid_params",
                    "error": (
                        f"{label}_config left_bars + right_bars + 1 = "
                        f"{l_bars + r_bars + 1} exceeds "
                        f"LIVE_SR_MAX_CANDLES ({config.LIVE_SR_MAX_CANDLES})"
                    ),
                    "params": {
                        "entry_config": [entry_left, entry_right],
                        "exit_config": [exit_left, exit_right],
                        "max_candles": config.LIVE_SR_MAX_CANDLES,
                    },
                    "generated_at": _now_iso(),
                }

        # ---- 2. Fetch both sources ONCE ----------------------------- #
        historical, historical_meta = self._load_historical_candles(
            entry, lb, debug=debug,
        )
        intraday, intraday_meta = self._fetch_intraday_candles(entry["instrument_key"])

        merged = self._merge_by_minute(historical, intraday)
        if not merged:
            return {
                "status": "no_data",
                "error": "no_candles_from_either_source",
                "instrument_key": entry["instrument_key"],
                "mode": "intraday" if only_today else "full",
                "today_iso": _today_iso(),
                "params": {
                    "entry_config": [entry_left, entry_right],
                    "exit_config": [exit_left, exit_right],
                    "lookback_days": lb,
                    "only_today": only_today,
                    "include_candles": include_candles,
                    "include_live_sr": include_live_sr,
                    "debug": debug,
                },
                "sources": {
                    "historical": historical_meta,
                    "intraday": intraday_meta,
                },
                "live_sr": (
                    self._load_live_sr_state(entry)
                    if include_live_sr else None
                ),
                "generated_at": _now_iso(),
            }

        merged_before_cap = len(merged)
        max_candles = config.LIVE_SR_CALC_MAX_CANDLES
        if len(merged) > max_candles:
            merged = merged[-max_candles:]

        # ---- 3. Compute both pivot passes --------------------------- #
        entry_levels = self._compute_pivots(merged, entry_left, entry_right)
        exit_levels = self._compute_pivots(merged, exit_left, exit_right)

        # ---- 4. Mark is_today + filter + sort (both) ---------------- #
        self._mark_is_today(entry_levels)
        self._mark_is_today(exit_levels)

        entry_total_before = len(entry_levels)
        exit_total_before = len(exit_levels)

        if only_today:
            entry_levels = [l for l in entry_levels if l["is_today"]]
            exit_levels = [l for l in exit_levels if l["is_today"]]

        entry_levels.sort(key=lambda l: l["price"])
        exit_levels.sort(key=lambda l: l["price"])

        current_price = merged[-1]["close"] if merged else None

        # ---- 5. Build per-block counts + nearest hints -------------- #
        def _block(levels_list: list[dict[str, Any]],
                   total_unfiltered: int,
                   l_bars: int,
                   r_bars: int) -> dict[str, Any]:
            nearest_r = None
            nearest_s = None
            if current_price is not None:
                above = sorted(
                    (x for x in levels_list
                     if x["level"] == "resistance" and x["price"] > current_price),
                    key=lambda x: x["price"],
                )
                below = sorted(
                    (x for x in levels_list
                     if x["level"] == "support" and x["price"] < current_price),
                    key=lambda x: x["price"],
                    reverse=True,
                )
                nearest_r = above[0] if above else None
                nearest_s = below[0] if below else None
            return {
                "params": {"left_bars": l_bars, "right_bars": r_bars},
                "levels": levels_list,
                "counts": {
                    "total": len(levels_list),
                    "total_unfiltered": total_unfiltered,
                    "resistance": sum(
                        1 for l in levels_list if l["level"] == "resistance"
                    ),
                    "support": sum(
                        1 for l in levels_list if l["level"] == "support"
                    ),
                    "today": sum(1 for l in levels_list if l["is_today"]),
                },
                "nearest": {
                    "resistance_above": nearest_r,
                    "support_below": nearest_s,
                },
            }

        entry_block = _block(
            entry_levels, entry_total_before, entry_left, entry_right,
        )
        exit_block = _block(
            exit_levels, exit_total_before, exit_left, exit_right,
        )

        # ---- 6. Optional live_sr block ------------------------------ #
        live_sr_state: dict[str, Any] | None = None
        if include_live_sr:
            live_sr_state = self._load_live_sr_state(entry)

        # ---- 7. Warnings (shared + per-config) ---------------------- #
        warnings: list[str] = []
        if historical_meta.get("error"):
            warnings.append(f"historical: {historical_meta['error']}")
        if intraday_meta.get("error"):
            warnings.append(f"intraday: {intraday_meta['error']}")
        if merged_before_cap > max_candles:
            warnings.append(
                f"merged candles capped from {merged_before_cap} to {max_candles}"
            )
        if historical_meta.get("count_mismatch"):
            warnings.append(
                f"historical: declared_count={historical_meta.get('declared_count')} "
                f"but file contains {historical_meta.get('candles_raw')} entries"
            )

        for label, total_unfiltered, kept in (
            ("entry", entry_total_before, len(entry_levels)),
            ("exit", exit_total_before, len(exit_levels)),
        ):
            if only_today and kept == 0 and total_unfiltered > 0:
                warnings.append(
                    f"{label} config: 0 levels confirmed today, "
                    f"{total_unfiltered} historical level(s) available "
                    f"(pass only_today=false to include them)"
                )
            elif only_today and kept < total_unfiltered:
                warnings.append(
                    f"{label} config: filtered out "
                    f"{total_unfiltered - kept} historical level(s)"
                )

        if include_live_sr and isinstance(live_sr_state, dict):
            if live_sr_state.get("error"):
                warnings.append(f"live_sr: {live_sr_state['error']}")
            elif not live_sr_state.get("is_today"):
                warnings.append(
                    f"live_sr: file is dated {live_sr_state.get('date')} "
                    f"(not today — {live_sr_state.get('today_iso')})"
                )
            elif not config.LIVE_SR_ENABLED:
                warnings.append(
                    "live_sr: LIVE_SR_ENABLED is false — file may be stale"
                )

        # ---- 8. Build response -------------------------------------- #
        return {
            "status": "ok",
            "mode": "intraday" if only_today else "full",
            "today_iso": _today_iso(),
            "instrument_key": entry["instrument_key"],
            "trading_symbol": entry.get("trading_symbol"),
            "underlying": entry.get("underlying"),
            "strike": entry.get("strike_price"),
            "type": entry.get("strike_type"),
            "expiry": entry.get("expiry"),
            "is_index": bool(entry.get("is_index")),
            "selector_source": selector_source,
            "params": {
                "entry_config": [entry_left, entry_right],
                "exit_config": [exit_left, exit_right],
                "lookback_days": lb,
                "only_today": only_today,
                "include_candles": include_candles,
                "include_live_sr": include_live_sr,
                "debug": debug,
                "max_candles": max_candles,
            },
            "sources": {
                "historical": historical_meta,
                "intraday": intraday_meta,
            },
            "merged": {
                "candle_count": len(merged),
                "candle_count_before_cap": merged_before_cap,
                "first_candle": merged[0]["time"] if merged else None,
                "last_candle": merged[-1]["time"] if merged else None,
                "current_price": current_price,
            },
            "entry": entry_block,
            "exit": exit_block,
            "live_sr": live_sr_state,
            "warnings": warnings,
            "candles": merged if include_candles else None,
            "generated_at": _now_iso(),
        }

    # ------------------------------------------------------------------ #
    # Per-minute pivot cache (2026-11-05)                                #
    # ------------------------------------------------------------------ #
    def calculate_pivots_cached(
        self,
        instrument_key: str,
        left_bars: int,
        right_bars: int,
        *,
        lookback_days: int | None = None,
        only_today: bool = False,
        minute_bucket: int | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Return pivots for ``(instrument_key, left, right)`` with a
        per-minute cache.

        Intended for the 30 s tick-loop callers that would otherwise
        hit the intraday API twice per minute per instrument (e.g. the
        ``(15, 15)`` exit scan during POST-1130 tracking).

        Cache semantics
        ---------------
        * **Key**: ``(instrument_key, left_bars, right_bars)``.
        * **TTL**: one minute (``_PIVOT_CACHE_TTL_SECONDS``), reinforced
          by a minute-bucket key so a clock bump can't return stale
          data across the minute boundary.
        * **Within one minute**, every call with the same key returns
          **byte-identical** ``levels`` (``cached=True``).  This is
          what makes ``evaluate_post_1130_exits`` deterministic when
          it runs twice in the same minute.
        * **At the minute boundary**, the entry is invalidated and the
          pipeline recomputes from fresh data.
        * **Failed results** (``no_data`` / ``insufficient_data`` /
          ``invalid_params`` / ``not_found``) are **NOT cached** —
          the next call retries.  This prevents a transient API
          hiccup from freezing the cache for a whole minute.

        Parameters
        ----------
        instrument_key : str
            Full Upstox instrument key.
        left_bars, right_bars : int
            Pivot window.  Clamped to 1..100.
        lookback_days : int | None
            Historical tail for the merged series.  ``None`` uses the
            configured default (clamped to 1..30).
        only_today : bool
            When ``True`` (default ``False``), filter the returned
            ``levels`` list to today's confirmations.  Matches
            ``calculate_dual``'s default only when explicitly set.
        minute_bucket : int | None
            Override the current minute bucket — used for testing /
            replay.  When ``None`` (default), uses wall-clock time.
        force_refresh : bool
            When ``True``, skip the cache lookup and recompute (the
            result is still written to cache).

        Returns
        -------
        ``dict`` with the following keys::

            {
              "status":        "ok" | "no_data" | "insufficient_data"
                               | "invalid_params" | "not_found" | "error",
              "instrument_key": str,
              "params":        {"left_bars": int, "right_bars": int,
                                "only_today": bool},
              "minute_bucket": int,
              "cached":        bool,       # True when served from cache
              "cached_age_seconds": float, # 0.0 on miss
              "levels":        [ ...enriched level dicts... ],
              "candle_count":  int,
              "first_candle":  str | None,
              "last_candle":   str | None,
              "warnings":      list[str],
              "error":         str | None,
              "generated_at":  str,
            }

        Never raises.
        """
        # ---- Sanitise params ---------------------------------------- #
        key = str(instrument_key or "").strip()
        if not key:
            return {
                "status": "invalid_params",
                "error": "instrument_key must be a non-empty string",
                "instrument_key": None,
                "params": {"left_bars": None, "right_bars": None},
                "minute_bucket": None,
                "cached": False,
                "cached_age_seconds": 0.0,
                "levels": [],
                "candle_count": 0,
                "first_candle": None,
                "last_candle": None,
                "warnings": [],
                "generated_at": _now_iso(),
            }

        try:
            left = max(1, min(100, int(left_bars)))
            right = max(1, min(100, int(right_bars)))
        except (TypeError, ValueError):
            return {
                "status": "invalid_params",
                "error": "left_bars/right_bars must be integers",
                "instrument_key": key,
                "params": {
                    "left_bars": left_bars, "right_bars": right_bars,
                },
                "minute_bucket": None,
                "cached": False,
                "cached_age_seconds": 0.0,
                "levels": [],
                "candle_count": 0,
                "first_candle": None,
                "last_candle": None,
                "warnings": [],
                "generated_at": _now_iso(),
            }

        if left + right + 1 > config.LIVE_SR_MAX_CANDLES:
            return {
                "status": "invalid_params",
                "error": (
                    f"left_bars + right_bars + 1 = {left + right + 1} "
                    f"exceeds LIVE_SR_MAX_CANDLES ({config.LIVE_SR_MAX_CANDLES})"
                ),
                "instrument_key": key,
                "params": {"left_bars": left, "right_bars": right},
                "minute_bucket": None,
                "cached": False,
                "cached_age_seconds": 0.0,
                "levels": [],
                "candle_count": 0,
                "first_candle": None,
                "last_candle": None,
                "warnings": [],
                "generated_at": _now_iso(),
            }

        now = datetime.now(_market_tz())
        current_bucket = (
            int(minute_bucket) if minute_bucket is not None
            else int(now.timestamp() // 60)
        )
        cache_key = (key, left, right)

        # ---- Cache lookup ------------------------------------------ #
        if not force_refresh:
            with self._pivot_cache_lock:
                cached = self._pivot_cache.get(cache_key)
                if cached is not None:
                    if (
                        cached.get("minute_bucket") == current_bucket
                        and time.monotonic() < float(
                            cached.get("expires_at") or 0.0
                        )
                    ):
                        payload = dict(cached["payload"])
                        payload["cached"] = True
                        payload["cached_age_seconds"] = round(
                            time.monotonic()
                            - float(cached.get("cached_at") or 0.0),
                            3,
                        )
                        return payload

        # ---- Cache miss (or forced refresh) — compute -------------- #
        result = self._pivots_for_instrument_minute(
            key, left, right, lookback_days, only_today,
        )

        payload = {
            "status": result.get("status", "ok"),
            "instrument_key": key,
            "params": {
                "left_bars": left,
                "right_bars": right,
                "only_today": bool(only_today),
            },
            "minute_bucket": current_bucket,
            "cached": False,
            "cached_age_seconds": 0.0,
            "levels": result.get("levels", []) or [],
            "candle_count": result.get("candle_count", 0),
            "first_candle": result.get("first_candle"),
            "last_candle": result.get("last_candle"),
            "warnings": result.get("warnings", []) or [],
            "error": result.get("error"),
            "generated_at": _now_iso(),
        }

        # Only cache SUCCESSFUL results — a transient API error must
        # not freeze the cache for a whole minute.
        if payload["status"] == "ok":
            with self._pivot_cache_lock:
                now_mono = time.monotonic()
                self._pivot_cache[cache_key] = {
                    "minute_bucket": current_bucket,
                    "expires_at": now_mono + _PIVOT_CACHE_TTL_SECONDS,
                    "cached_at": now_mono,
                    "payload": dict(payload),
                }
                self._prune_pivot_cache_locked(current_bucket)

        return payload

    def _pivots_for_instrument_minute(
        self,
        instrument_key: str,
        left_bars: int,
        right_bars: int,
        lookback_days: int | None,
        only_today: bool,
    ) -> dict[str, Any]:
        """Internal: build the merged series + compute pivots (no cache).

        This is the compute path behind ``calculate_pivots_cached``.
        It intentionally does NOT emit the diagnostics / nearest /
        per-source-meta blocks that ``calculate`` and ``calculate_dual``
        return — those are for the HTTP surface, not for the 30 s
        tick-loop.  The level dicts themselves are byte-identical
        (they go through ``_compute_pivots`` → ``_enrich_level``).

        Never raises.
        """
        try:
            entry = instrument_registry.get(instrument_key)
            if entry is None:
                entry = instrument_registry.register_adhoc(instrument_key)
            if entry is None:
                return {
                    "status": "not_found",
                    "error": "instrument_not_found",
                    "levels": [],
                    "candle_count": 0,
                    "first_candle": None,
                    "last_candle": None,
                    "warnings": [],
                }

            lb = lookback_days if lookback_days is not None else (
                config.LIVE_SR_CALC_DEFAULT_LOOKBACK_DAYS
            )
            lb = max(1, min(30, lb))

            historical, _h_meta = self._load_historical_candles(entry, lb)
            intraday, _i_meta = self._fetch_intraday_candles(instrument_key)
            merged = self._merge_by_minute(historical, intraday)

            if not merged:
                return {
                    "status": "no_data",
                    "error": "no_candles_from_either_source",
                    "levels": [],
                    "candle_count": 0,
                    "first_candle": None,
                    "last_candle": None,
                    "warnings": [],
                }

            max_candles = config.LIVE_SR_CALC_MAX_CANDLES
            if len(merged) > max_candles:
                merged = merged[-max_candles:]

            if len(merged) < left_bars + right_bars + 1:
                return {
                    "status": "insufficient_data",
                    "error": (
                        f"not enough candles — need "
                        f"{left_bars + right_bars + 1}, have {len(merged)}"
                    ),
                    "levels": [],
                    "candle_count": len(merged),
                    "first_candle": merged[0]["time"] if merged else None,
                    "last_candle": merged[-1]["time"] if merged else None,
                    "warnings": [],
                }

            levels = self._compute_pivots(merged, left_bars, right_bars)
            self._mark_is_today(levels)
            levels.sort(key=lambda l: l["price"])

            if only_today:
                levels = [l for l in levels if l["is_today"]]
                levels.sort(key=lambda l: l["price"])

            return {
                "status": "ok",
                "error": None,
                "levels": levels,
                "candle_count": len(merged),
                "first_candle": merged[0]["time"] if merged else None,
                "last_candle": merged[-1]["time"] if merged else None,
                "warnings": [],
            }
        except Exception as exc:
            logger.exception(
                "[live-sr-calc] _pivots_for_instrument_minute failed for %s",
                instrument_key,
            )
            return {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "levels": [],
                "candle_count": 0,
                "first_candle": None,
                "last_candle": None,
                "warnings": [],
            }

    def _prune_pivot_cache_locked(self, current_bucket: int) -> None:
        """Prune stale + excess entries.  Caller must hold the lock."""
        # 1) Drop entries whose minute bucket is way behind current.
        stale_keys = [
            k
            for k, v in self._pivot_cache.items()
            if (
                current_bucket - int(v.get("minute_bucket") or 0)
                > _PIVOT_CACHE_MAX_AGE_BUCKETS
            )
        ]
        for k in stale_keys:
            self._pivot_cache.pop(k, None)

        # 2) Hard cap — evict oldest by cached_at (LRU-ish).
        if len(self._pivot_cache) > _PIVOT_CACHE_MAX_ENTRIES:
            items = sorted(
                self._pivot_cache.items(),
                key=lambda kv: float(kv[1].get("cached_at") or 0.0),
            )
            excess = len(items) - _PIVOT_CACHE_MAX_ENTRIES
            for k, _ in items[:excess]:
                self._pivot_cache.pop(k, None)

    def clear_pivot_cache(self) -> dict[str, Any]:
        """Drop every cached pivot result (test / admin helper).

        The base methods do not use the cache, so this is safe to call
        at any time.  Returns ``{"status": "cleared", "removed": N}``.
        """
        with self._pivot_cache_lock:
            n = len(self._pivot_cache)
            self._pivot_cache.clear()
        logger.info(
            "[live-sr-calc] cleared %d pivot cache entr%s",
            n, "y" if n == 1 else "ies",
        )
        return {"status": "cleared", "removed": n}

    def get_pivot_cache_stats(self) -> dict[str, Any]:
        """Return a lightweight stats snapshot for admin / ops.

        Fields::

            {
              "entries":              int,  # current cache size
              "max_entries":          int,
              "ttl_seconds":          float,
              "max_age_buckets":      int,
              "instruments_tracked":  int,  # distinct instrument keys
              "configs_tracked":      list[[int, int]],  # distinct
                                         # (left, right) tuples
              "oldest_cached_at":     float | None,
              "newest_cached_at":     float | None,
            }

        All timestamps are ``time.monotonic()`` — use them only for
        relative comparisons, not absolute display.
        """
        with self._pivot_cache_lock:
            entries = list(self._pivot_cache.values())
            keys = list(self._pivot_cache.keys())
            instruments = {k[0] for k in keys}
            configs = sorted({(k[1], k[2]) for k in keys})
            cached_times = [
                float(v.get("cached_at") or 0.0) for v in entries
            ]
            return {
                "entries": len(entries),
                "max_entries": _PIVOT_CACHE_MAX_ENTRIES,
                "ttl_seconds": _PIVOT_CACHE_TTL_SECONDS,
                "max_age_buckets": _PIVOT_CACHE_MAX_AGE_BUCKETS,
                "instruments_tracked": len(instruments),
                "configs_tracked": [[c[0], c[1]] for c in configs],
                "oldest_cached_at": (
                    min(cached_times) if cached_times else None
                ),
                "newest_cached_at": (
                    max(cached_times) if cached_times else None
                ),
            }

    # ------------------------------------------------------------------ #
    # Public loader helpers (used by sr_backfill_service)                #
    # ------------------------------------------------------------------ #
    def load_historical_candles_for_entry(
        self,
        entry: dict[str, Any],
        lookback_days: int,
    ) -> list[dict[str, Any]]:
        """Public wrapper around ``_load_historical_candles``.

        Returns just the candle list (diagnostics are dropped). Callers
        who need the ``meta`` dict should call the private method
        directly.

        Used by ``services/sr_backfill_service`` so the periodic
        refresh-from-API job (``live_sr_refresh_from_api``) can produce
        the same historical tail the calc endpoint uses. That's what
        makes ``live_sr_levels.json`` catch pivots that formed near the
        start of the session (they need 15 bars *before* today's open).
        """
        candles, _meta = self._load_historical_candles(entry, lookback_days)
        return candles

    def fetch_intraday_candles_for_entry(
        self,
        instrument_key: str,
    ) -> list[dict[str, Any]]:
        """Public wrapper around ``_fetch_intraday_candles``.

        Returns just the candle list. Never raises — a failed Upstox
        call returns ``[]``.
        """
        candles, _meta = self._fetch_intraday_candles(instrument_key)
        return candles

    def merge_candles(
        self,
        historical: list[dict[str, Any]],
        intraday: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Public wrapper around ``_merge_by_minute``.

        Merges by minute bucket; intraday wins on collision.
        """
        return self._merge_by_minute(historical, intraday)

    # ------------------------------------------------------------------ #
    # G1 v4.1 — canonical day series builder                             #
    # ------------------------------------------------------------------ #
    def build_series_for_day(
        self,
        instrument_key: str,
        lookback_days: int | None = None,
    ) -> list[dict[str, Any]]:
        """Build ONE deterministic candle series for the trading day.

        Merges:

            * the historical backup tail (``lookback_days`` days), from
              ``data/<...>/candles.json``, and
            * today's intraday 1-min candles from Upstox,

        in chronological order.  Intraday wins on any minute collision.

        This is the *canonical* input for order-entry resolution and
        replay (see ``services/dummy_order_service._build_fixed_series``).
        Using the same series for entry, stop and target computation
        guarantees internal consistency — the pre-v4.1 code mixed three
        mutable sources (in-memory buffer, intraday API, live LTP) and
        therefore produced different entries across restarts.

        Returns an empty list if neither source yields any candles.
        Never raises.

        Parameters
        ----------
        instrument_key : str
            The full Upstox instrument key (``NSE_FO|53942``,
            ``NSE_INDEX|Nifty 50``, etc.).
        lookback_days : int | None
            Days of historical tail to merge.  ``None`` falls back to
            ``config.LIVE_SR_CALC_DEFAULT_LOOKBACK_DAYS`` (clamped to
            1..30).
        """
        try:
            entry = instrument_registry.get(instrument_key)
            if entry is None:
                entry = instrument_registry.register_adhoc(instrument_key)
            if entry is None:
                logger.warning(
                    "[live-sr-calc] build_series_for_day: unknown "
                    "instrument %s", instrument_key,
                )
                return []

            lb = lookback_days if lookback_days is not None else (
                config.LIVE_SR_CALC_DEFAULT_LOOKBACK_DAYS
            )
            lb = max(1, min(30, lb))

            historical = self.load_historical_candles_for_entry(entry, lb)
            intraday = self.fetch_intraday_candles_for_entry(instrument_key)
            merged = self._merge_by_minute(historical, intraday)
            return merged
        except Exception:
            logger.exception(
                "[live-sr-calc] build_series_for_day failed for %s",
                instrument_key,
            )
            return []

    # ------------------------------------------------------------------ #
    # Candle-close helpers (used by entry-signal / isolation engines)   #
    # ------------------------------------------------------------------ #
    def get_candle_close_at(
        self,
        instrument_key: str,
        iso_time: str,
    ) -> float | None:
        """Return the close price of the 1-min candle at ``iso_time``.

        Convenience wrapper around ``fetch_intraday_candles_for_entry()``
        that handles the minute-bucket matching (seconds/microseconds
        zeroed) so callers don't have to re-implement the comparison.

        Used by ``strategy_entry_signal_service._entry_price_for()`` as
        the fallback path when ``live_sr_levels_service._states`` is
        missing or doesn't yet contain the instrument (see G1 v4.1 fix
        §5.B). This makes the historical entry price available in
        simulate mode even when the counter-side instrument was never
        registered with the live SR engine.

        Returns ``None`` when:

            * ``iso_time`` cannot be parsed,
            * no intraday candle exists for that minute (i.e. the bar
              is ABSENT — the method does NOT return the newest
              available bar),
            * the Upstox fetch fails (never raises).

        The "absent minute → None" contract is enforced by the exact
        minute-bucket comparison below.  A caller that wants to retry
        later or skip the signal should treat ``None`` as "not
        resolvable right now", NOT as "fall through to live LTP".
        """
        target = _parse_time(iso_time)
        if target is None:
            return None
        target_minute = target.replace(second=0, microsecond=0)

        candles = self.fetch_intraday_candles_for_entry(instrument_key)
        for c in candles:
            c_dt = _parse_time(c.get("time"))
            if c_dt is None:
                continue
            if c_dt.replace(second=0, microsecond=0) == target_minute:
                try:
                    return float(c["close"])
                except (KeyError, TypeError, ValueError):
                    return None
        return None

    def get_candle_close_at_from_series(
        self,
        series: list[dict[str, Any]],
        iso_time: str,
    ) -> tuple[float | None, str]:
        """Return ``(close, source)`` for ``iso_time`` from a pre-built
        series.

        Unlike ``get_candle_close_at`` this method performs NO API calls
        and NO disk reads — it is a pure in-memory lookup against a
        series the caller has already built (typically via
        ``build_series_for_day``).  This is the preferred path for
        ``services.dummy_order_service`` when resolving entry prices
        against its fixed per-day snapshot.

        ``source`` is one of:

            * ``"series"``           — close found in the series
            * ``"absent"``           — the requested minute is not
                                       in the series (caller should
                                       defer / skip, never LTP-fall
                                       through)
            * ``"bad_close_value"``  — the candle exists but its
                                       ``close`` field cannot be
                                       coerced to float
            * ``"invalid_iso_time"`` — ``iso_time`` could not be parsed
        """
        target = _parse_time(iso_time)
        if target is None:
            return None, "invalid_iso_time"
        target_minute = target.replace(second=0, microsecond=0)

        for c in series or []:
            c_dt = _parse_time(c.get("time"))
            if c_dt is None:
                continue
            if c_dt.replace(second=0, microsecond=0) == target_minute:
                try:
                    return float(c["close"]), "series"
                except (KeyError, TypeError, ValueError):
                    return None, "bad_close_value"
        return None, "absent"

    def get_first_intraday_candle(
        self,
        instrument_key: str,
        expected_first_minute: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the first 1-min intraday candle for ``instrument_key``,
        verified against the expected session-open minute.

        Used by ``level_isolated_instrument_service._maybe_self_heal()``
        to verify that ``first_candle.json`` matches today's real first
        bar (see G1 v4.1 fix §5.D).  The first candle's H / L must agree
        with the cached OR file — otherwise the cached OR is stale and
        must be recomputed.

        **Verification:** the API's first candle must land exactly on
        the expected session-open minute (``09:15`` IST by default, or
        ``config.OPENING_RANGE_FIRST_MINUTE`` when set).  If the
        intraday window has been truncated — e.g. a mid-day restart
        where the API only returns bars from 11:00 onwards, or an
        Upstox glitch that drops the opening bar — the method returns
        ``None`` rather than silently handing the caller a later bar
        as if it were the OR candle.

        Override the expected minute by passing
        ``expected_first_minute="HH:MM"`` (e.g. ``"09:16"`` for a
        caller that wants the first bar of a specific window).

        Returns ``None`` when:

            * the fetch returns no candles,
            * the first candle is earlier than the expected minute
              (should not happen in practice),
            * the first candle is LATER than the expected minute
              (truncated window — logged as WARNING),
            * the first candle's timestamp cannot be parsed.
        """
        candles = self.fetch_intraday_candles_for_entry(instrument_key)
        if not candles:
            return None

        if expected_first_minute:
            try:
                hh, mm = expected_first_minute.split(":")
                expected_time = dtime(int(hh), int(mm))
            except (ValueError, AttributeError):
                expected_time = _default_session_open_time()
        else:
            expected_time = _default_session_open_time()

        first = candles[0]
        first_dt = _parse_time(first.get("time"))
        if first_dt is None:
            return None

        first_time = first_dt.time()
        if first_time == expected_time:
            return first

        # Session-open bar is absent from the API response.
        logger.warning(
            "[live-sr-calc] get_first_intraday_candle: %s first "
            "intraday bar is at %s (expected %s) — returning None "
            "(truncated window)",
            instrument_key,
            first_time.strftime("%H:%M"),
            expected_time.strftime("%H:%M"),
        )
        return None

    # ------------------------------------------------------------------ #
    # Selector resolution                                                #
    # ------------------------------------------------------------------ #
    def _resolve_entry(
        self,
        instrument_key: str | None,
        underlying: str | None,
        strike: str | int | float | None,
        strike_type: str | None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        key = str(instrument_key).strip() if instrument_key else ""
        if key:
            entry = instrument_registry.get(key)
            if entry is None:
                entry = instrument_registry.register_adhoc(key)
            return entry, "instrument_key"

        u = normalize_underlying(underlying)
        s = normalize_strike(strike)
        t = normalize_strike_type(strike_type)
        if not (u and s and t):
            return None, None

        entry = instrument_registry.resolve(u, s, t)
        if entry is not None:
            return entry, "triple"

        for alt in (f"{u} 50", f"{u}50", u.split(" ", 1)[0]):
            alt_norm = normalize_underlying(alt)
            if not alt_norm or alt_norm == u:
                continue
            entry = instrument_registry.resolve(alt_norm, s, t)
            if entry is not None:
                return entry, "triple"

        return None, None

    # ------------------------------------------------------------------ #
    # Historical candles                                                 #
    # ------------------------------------------------------------------ #
    def _historical_paths(self, entry: dict[str, Any]) -> list[Path]:
        candidates: list[Path] = []

        raw = str(entry.get("candles_path") or "").strip()
        if raw:
            candidates.append(Path(raw))

        underlying = str(entry.get("underlying") or "").strip().lower()
        strike = entry.get("strike_price")
        strike_type = str(entry.get("strike_type") or "").upper()
        is_index = bool(entry.get("is_index")) or strike is None

        if is_index and underlying:
            candidates.append(
                Path(config.LIVE_DATA_DIR) / "index" / underlying / "candles.json"
            )
        elif underlying and strike is not None and strike_type:
            try:
                strike_int = int(float(strike))
            except (TypeError, ValueError):
                strike_int = str(strike)
            candidates.append(
                Path(config.LIVE_DATA_DIR)
                / underlying
                / f"{strike_int}_{strike_type}"
                / "candles.json"
            )

        seen: set[str] = set()
        out: list[Path] = []
        for p in candidates:
            key = str(p)
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
        return out

    def _extract_candles_from_payload(self, payload: Any) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                candles = data.get("candles")
                if isinstance(candles, list):
                    return candles
            if isinstance(data, list):
                return data
            candles = payload.get("candles")
            if isinstance(candles, list):
                return candles
        return []

    def _load_historical_candles(
        self,
        entry: dict[str, Any],
        lookback_days: int,
        debug: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        meta: dict[str, Any] = {
            "path": None,
            "candles": 0,
            "candles_raw": 0,
            "candles_rejected_format": 0,
            "candles_rejected_cutoff": 0,
            "first_candle": None,
            "last_candle": None,
            "error": None,
        }

        chosen: Path | None = None
        for candidate in self._historical_paths(entry):
            if candidate.exists() and candidate.is_file():
                chosen = candidate
                break

        if chosen is None:
            meta["error"] = "file_missing"
            return [], meta

        meta["path"] = str(chosen)

        try:
            payload = json.loads(chosen.read_text(encoding="utf-8"))
        except Exception as exc:
            meta["error"] = f"unreadable: {type(exc).__name__}"
            logger.warning(
                "[live-sr-calc] cannot parse %s: %s: %s",
                chosen, type(exc).__name__, exc,
            )
            return [], meta

        if isinstance(payload, dict):
            if payload.get("updated_at") is not None:
                meta["file_updated_at"] = str(payload["updated_at"])
            if payload.get("relative_path") is not None:
                meta["relative_path"] = str(payload["relative_path"])
            if payload.get("count") is not None:
                try:
                    meta["declared_count"] = int(payload["count"])
                except (TypeError, ValueError):
                    meta["declared_count"] = payload["count"]

        raw_list = self._extract_candles_from_payload(payload)
        meta["candles_raw"] = len(raw_list)

        if not raw_list:
            meta["error"] = "empty_candles_list"
            return [], meta

        declared = meta.get("declared_count")
        if isinstance(declared, int) and declared != len(raw_list):
            meta["count_mismatch"] = True

        cutoff = datetime.now(_market_tz()).timestamp() - (lookback_days * 86400)

        out: list[dict[str, Any]] = []
        rejected_format = 0
        rejected_cutoff = 0

        for raw in raw_list:
            c = _normalise_candle(raw)
            if c is None:
                rejected_format += 1
                continue
            if c["ts_ms"] / 1000.0 < cutoff:
                rejected_cutoff += 1
                continue
            out.append(c)

        out.sort(key=lambda x: x["ts_ms"])

        meta["candles"] = len(out)
        meta["candles_rejected_format"] = rejected_format
        meta["candles_rejected_cutoff"] = rejected_cutoff
        meta["first_candle"] = out[0]["time"] if out else None
        meta["last_candle"] = out[-1]["time"] if out else None

        if not out:
            meta["sample_structure"] = [
                _describe_raw(r) for r in raw_list[:3]
            ]
            if rejected_format == len(raw_list):
                meta["error"] = (
                    f"all_{rejected_format}_entries_failed_format_parsing"
                )
            elif rejected_cutoff == len(raw_list):
                meta["error"] = (
                    f"all_{rejected_cutoff}_entries_older_than_"
                    f"{lookback_days}d_cutoff"
                )
            elif rejected_format + rejected_cutoff == len(raw_list):
                meta["error"] = (
                    f"no_entries_passed_"
                    f"(format={rejected_format},cutoff={rejected_cutoff})"
                )
        elif debug:
            meta["sample_structure"] = [
                _describe_raw(r) for r in raw_list[:3]
            ]

        return out, meta

    # ------------------------------------------------------------------ #
    # Intraday candles                                                   #
    # ------------------------------------------------------------------ #
    def _fetch_intraday_candles(
        self,
        instrument_key: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        meta: dict[str, Any] = {
            "api": "HistoryV3Api.get_intra_day_candle_data",
            "candles": 0,
            "first_candle": None,
            "last_candle": None,
            "error": None,
        }

        try:
            import upstox_client  # type: ignore
        except ImportError:
            meta["error"] = "upstox_client_not_installed"
            return [], meta

        try:
            cfg = upstox_client.Configuration()
            try:
                from services.token_service import token_service
                token = token_service.get_access_token()
                if token:
                    cfg.access_token = token
            except Exception:
                pass

            api = upstox_client.HistoryV3Api(upstox_client.ApiClient(cfg))
            response = api.get_intra_day_candle_data(
                instrument_key, "minutes", "1",
            )
        except Exception as exc:
            meta["error"] = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "[live-sr-calc] intraday fetch failed for %s: %s: %s",
                instrument_key, type(exc).__name__, exc,
            )
            return [], meta

        body: Any = response
        if hasattr(response, "to_dict"):
            try:
                body = response.to_dict()
            except Exception:
                body = response
        elif hasattr(response, "data"):
            body = response.data

        if not isinstance(body, dict):
            meta["error"] = "uncoercible_response"
            return [], meta

        raw_candles = None
        data = body.get("data")
        if isinstance(data, dict):
            maybe = data.get("candles")
            if isinstance(maybe, list):
                raw_candles = maybe
        if not isinstance(raw_candles, list):
            top = body.get("candles")
            if isinstance(top, list):
                raw_candles = top
        if not isinstance(raw_candles, list):
            raw_candles = []

        out: list[dict[str, Any]] = []
        for raw in raw_candles:
            c = _normalise_candle(raw)
            if c is not None:
                out.append(c)

        out.sort(key=lambda x: x["ts_ms"])

        meta["candles"] = len(out)
        meta["first_candle"] = out[0]["time"] if out else None
        meta["last_candle"] = out[-1]["time"] if out else None
        return out, meta

    # ------------------------------------------------------------------ #
    # Optional live_sr block (opt-in, default false)                     #
    # ------------------------------------------------------------------ #
    def _live_sr_paths(self, entry: dict[str, Any]) -> list[Path]:
        """Return candidate live_sr_levels.json paths.

        The live-SR file lives next to the instrument's candles.json /
        ema_crosses.json in the same folder. Derive from
        ``entry["candles_path"]`` first, then fall back to the canonical
        layout (index vs option).
        """
        candidates: list[Path] = []

        raw = str(entry.get("candles_path") or "").strip()
        if raw:
            try:
                p = Path(raw)
                if p.name:
                    candidates.append(p.with_name(config.LIVE_SR_FILENAME))
            except Exception:
                pass

        underlying = str(entry.get("underlying") or "").strip().lower()
        strike = entry.get("strike_price")
        strike_type = str(entry.get("strike_type") or "").upper()
        is_index = bool(entry.get("is_index")) or strike is None

        if is_index and underlying:
            candidates.append(
                Path(config.LIVE_DATA_DIR)
                / "index" / underlying / config.LIVE_SR_FILENAME
            )
        elif underlying and strike is not None and strike_type:
            try:
                strike_int = int(float(strike))
            except (TypeError, ValueError):
                strike_int = str(strike)
            candidates.append(
                Path(config.LIVE_DATA_DIR)
                / underlying
                / f"{strike_int}_{strike_type}"
                / config.LIVE_SR_FILENAME
            )

        seen: set[str] = set()
        out: list[Path] = []
        for p in candidates:
            k = str(p)
            if k in seen:
                continue
            seen.add(k)
            out.append(p)
        return out

    def _load_live_sr_state(
        self,
        entry: dict[str, Any],
    ) -> dict[str, Any]:
        """Load the persisted ``live_sr_levels.json`` for this instrument.

        Never raises. Every failure mode lands in ``error`` while the rest
        of the block stays at its default "unavailable" values.

        The returned levels are the raw persisted records. Levels are
        sorted by price ascending so the block matches the ``levels``
        array order.
        """
        today = _today_iso()
        meta: dict[str, Any] = {
            "path": None,
            "available": False,
            "today_iso": today,
            "date": None,
            "is_today": False,
            "candle_count": 0,
            "level_count": 0,
            "counts": {
                "resistance": 0,
                "support": 0,
            },
            "first_level_time": None,
            "last_level_time": None,
            "last_candle": None,
            "updated_at": None,
            "levels": [],
            "error": None,
        }

        chosen: Path | None = None
        for candidate in self._live_sr_paths(entry):
            if candidate.exists() and candidate.is_file():
                chosen = candidate
                break

        if chosen is None:
            meta["error"] = "file_missing"
            return meta

        meta["path"] = str(chosen)

        try:
            payload = json.loads(chosen.read_text(encoding="utf-8"))
        except Exception as exc:
            meta["error"] = f"unreadable: {type(exc).__name__}"
            logger.warning(
                "[live-sr-calc] cannot parse %s: %s: %s",
                chosen, type(exc).__name__, exc,
            )
            return meta

        if not isinstance(payload, dict):
            meta["error"] = "not_a_dict"
            return meta

        meta["available"] = True
        meta["date"] = payload.get("date")
        meta["is_today"] = (str(payload.get("date") or "") == today)
        try:
            meta["candle_count"] = int(payload.get("candle_count") or 0)
        except (TypeError, ValueError):
            meta["candle_count"] = 0
        meta["updated_at"] = payload.get("updated_at")
        meta["last_candle"] = payload.get("last_candle")

        raw_levels = payload.get("levels") or []
        if isinstance(raw_levels, list):
            levels_copy = [dict(l) for l in raw_levels if isinstance(l, dict)]
            # Remove any residual broken fields so consumers see a clean shape.
            for lvl in levels_copy:
                lvl.pop("broken", None)
                lvl.pop("broken_at", None)
                lvl.pop("broken_price", None)
                lvl.pop("broken_bar_index", None)

            # Sort ascending by price to match the calc's own ordering.
            levels_copy.sort(key=lambda l: float(l.get("price") or 0.0))

            meta["levels"] = levels_copy
            meta["level_count"] = len(levels_copy)

            counts = {"resistance": 0, "support": 0}
            for l in levels_copy:
                t = str(l.get("level") or "").lower()
                if t == "resistance":
                    counts["resistance"] += 1
                elif t == "support":
                    counts["support"] += 1
            meta["counts"] = counts

            times: list[str] = []
            for l in levels_copy:
                t = str(l.get("pivot_time") or "")
                if t:
                    times.append(t)
            if times:
                meta["first_level_time"] = min(times)
                meta["last_level_time"] = max(times)

        return meta

    # ------------------------------------------------------------------ #
    # Merge                                                              #
    # ------------------------------------------------------------------ #
    def _merge_by_minute(
        self,
        historical: list[dict[str, Any]],
        intraday: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_bucket: dict[int, dict[str, Any]] = {}
        for c in historical:
            by_bucket[c["bucket"]] = c
        for c in intraday:
            by_bucket[c["bucket"]] = c
        return [by_bucket[b] for b in sorted(by_bucket.keys())]

    # ------------------------------------------------------------------ #
    # Pivot detection                                                    #
    # ------------------------------------------------------------------ #
    def _compute_pivots(
        self,
        candles: list[dict[str, Any]],
        left: int,
        right: int,
    ) -> list[dict[str, Any]]:
        """Return the confirmed pivots for one ``(left, right)`` window.

        Every emitted dict carries BOTH the historical field set
        (``price``, ``level`` [type], ``pivot_time``, ``confirmed_time``,
        ``confirmed_time_price``, ``pivot_bar_index``,
        ``confirmed_bar_index``, ``left_bars``, ``right_bars``,
        ``is_today``) AND the post-11:30 spec fields added on
        2026-11-05 (``level_price``, ``type``, ``confirmation_time``,
        ``status``) via ``_enrich_level``.  Keeping both field sets
        live in the same dict means existing consumers (``_mark_is_today``,
        sort comparators, ``nearest`` computation, the dashboard, and
        the ``/live-sr-calc`` HTTP route) continue to work without
        modification.
        """
        levels: list[dict[str, Any]] = []
        n = len(candles)
        if n < left + right + 1:
            return levels

        for i in range(left, n - right):
            c = candles[i]

            left_ok_res = all(
                candles[j]["high"] < c["high"]
                for j in range(i - left, i)
            )
            right_ok_res = all(
                candles[j]["high"] < c["high"]
                for j in range(i + 1, i + right + 1)
            )
            left_ok_sup = all(
                candles[j]["low"] > c["low"]
                for j in range(i - left, i)
            )
            right_ok_sup = all(
                candles[j]["low"] > c["low"]
                for j in range(i + 1, i + right + 1)
            )

            confirmed_idx = i + right
            confirmed_candle = candles[confirmed_idx]
            confirmed_time_price = float(
                confirmed_candle.get("close")
                if confirmed_candle.get("close") is not None
                else confirmed_candle.get("ltp", 0.0)
            )

            if left_ok_res and right_ok_res:
                levels.append(_enrich_level({
                    "level": "resistance",
                    "price": float(c["high"]),
                    "pivot_bar_index": i,
                    "pivot_time": c["time"],
                    "confirmed_bar_index": confirmed_idx,
                    "confirmed_time": confirmed_candle["time"],
                    "confirmed_time_price": confirmed_time_price,
                    "left_bars": left,
                    "right_bars": right,
                    "is_today": False,
                }))

            if left_ok_sup and right_ok_sup:
                levels.append(_enrich_level({
                    "level": "support",
                    "price": float(c["low"]),
                    "pivot_bar_index": i,
                    "pivot_time": c["time"],
                    "confirmed_bar_index": confirmed_idx,
                    "confirmed_time": confirmed_candle["time"],
                    "confirmed_time_price": confirmed_time_price,
                    "left_bars": left,
                    "right_bars": right,
                    "is_today": False,
                }))

        return levels

    # ------------------------------------------------------------------ #
    # is_today marking                                                   #
    # ------------------------------------------------------------------ #
    def _mark_is_today(self, levels: list[dict[str, Any]]) -> None:
        today = datetime.now(_market_tz()).date()
        for level in levels:
            try:
                dt = datetime.fromisoformat(
                    str(level["confirmed_time"]).replace("Z", "+00:00")
                )
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_market_tz())
                level["is_today"] = (dt.date() == today)
            except Exception:
                level["is_today"] = False

# --------------------------------------------------------------------------- #
# Singleton                                                                   #
# --------------------------------------------------------------------------- #
live_sr_calc_service = LiveSRCalcService()