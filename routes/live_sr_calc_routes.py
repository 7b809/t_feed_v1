# routes/live_sr_calc_routes.py
"""On-demand SR calculation routes.

    GET  /live-sr/calc
        Query-based lookup. Accepts either:
            ?instrument_key=NSE_FO|51327
        or:
            ?underlying=NIFTY&strike=23500&type=CE

    GET  /live-sr/calc/{underlying}/{strike}/{strike_type}
        Path-based lookup. Mirrors the requirement's URL pattern:
            /live-sr/calc/nifty/23500/ce

    POST /live-sr/calc/batch
        Batch selector resolution — up to 50 instruments in one call.
        Each result is the raw per-instrument ``calculate()`` payload.

    POST /live-sr/calc/multi
        Multi-instrument SR — compact per-instrument response.
        Returns each instrument's ``supports`` and ``resistances`` as
        flat ascending price arrays (plus counts and last confirmed
        time). Pass ``compact=false`` to fall back to the raw payload
        shape produced by ``/batch``.

    GET  /live-sr/calc/isolation
        Read today's pre-computed SR snapshots straight from the
        isolation store — no recomputation, no Upstox call.

    GET  /live-sr/calc/isolation/{trading_symbol}
        Read one instrument's isolation-store snapshot.

    GET  /live-sr/calc/active
        Return the current active tracked set (config indexes + the
        dynamically-registered isolated/opposite pair + the promotion
        sidecar). Same shape as ``/live-sr/active``.

2026-11-06 — isolation-store fast path
--------------------------------------
The ``calculate()`` call always recomputes from fresh data (that is
its contract) — but operators and dashboards frequently want the
**already-computed** snapshot that the scheduler minute tick writes to
``isolation/{symbol}_live_sr.json``. Reading that file is
dramatically cheaper than a fresh Upstox fetch + pivot scan.

Two mechanisms expose it:

  * The existing ``/calc`` and ``/calc/{underlying}/{strike}/{type}``
    endpoints accept an optional ``source=`` query parameter:

        - ``recompute`` (default) — always recompute (current behaviour)
        - ``isolation``             — read the isolation store; return
                                      ``status="not_found"`` if the
                                      instrument has no snapshot
        - ``auto``                  — try the isolation store first
                                      (only when ``only_today=true``,
                                      matching what the store contains),
                                      fall back to recompute

    The response gains ``source`` (``"recompute"`` or ``"isolation"``)
    and, when the isolation store serves the data, a warning
    ``served_from_isolation_snapshot`` so callers know the levels are
    at most ~60s stale.

  * The new ``/calc/isolation`` (batch) and
    ``/calc/isolation/{trading_symbol}`` endpoints always read from
    the isolation store — no recompute path at all. Useful for
    dashboards that need to render the isolated/opposite pair's levels
    without triggering an Upstox fetch.

``/calc/active`` mirrors ``/live-sr/active`` — it imports the shared
``_get_active_tracked_set()`` helper from ``routes.live_sr_routes`` so
both modules return identical shapes.

MODE — intraday by default
--------------------------
``only_today`` defaults to **True**. The endpoint behaves like an
"intraday SR" API: it returns only the levels whose ``confirmed_time``
falls on today's IST calendar date. Levels from prior sessions are
**not** included.

To include historical levels, pass ``only_today=false`` explicitly.
The response then carries ``mode = "full"`` instead of ``"intraday"``.

Historical candles are still fetched in intraday mode — a pivot that
forms at 09:15 needs 15 bars of context before it, and those come from
yesterday's session. The filter runs on the OUTPUT, not the INPUT.

OPT-IN live_sr block
--------------------
``include_live_sr`` defaults to **False**. When set to ``true``, the
endpoint ALSO reads the persisted ``data/<...>/live_sr_levels.json``
(maintained by ``live_sr_levels_service``) and attaches it to the
response under a new top-level ``live_sr`` key.

The persisted levels are **NOT merged into ``levels``**:

    * ``levels`` stays pure — always recomputed from fresh historical
      + intraday data on every call.
    * ``live_sr`` is the running engine's actual persisted state for
      today.

Both ``levels`` and ``live_sr.levels`` are returned **sorted by price
ascending** (supports and resistances intermixed, matching TradingView).
Neither carries any "broken" / "intact" fields.

Common query parameters (all optional):

    left_bars        int, 1..100          default: config.LIVE_SR_LEFT_BARS
    right_bars       int, 1..100          default: config.LIVE_SR_RIGHT_BARS
    lookback_days    int, 1..30           default: config.LIVE_SR_CALC_DEFAULT_LOOKBACK_DAYS
    only_today       bool                 default: TRUE   <-- intraday by default
    include_candles  bool                 default: false  (include merged candle list)
    include_live_sr  bool                 default: FALSE  <-- opt-in live_sr block
    debug            bool                 default: false
                                          forces sample_structure on the
                                          historical source even when
                                          candles parse successfully.
    source           str                  default: "recompute"
                                          {"recompute","isolation","auto"}

Every endpoint returns HTTP 200 with a JSON body whose ``status`` field
carries the outcome:

    "ok"                — levels computed (or served from isolation)
    "not_found"         — selector does not resolve
    "no_data"           — no candles from either source
    "insufficient_data" — fewer candles than left + right + 1
    "invalid_params"    — left/right exceed LIVE_SR_MAX_CANDLES
    "error"             — unexpected exception during calc

The ``mode`` field in every "ok"/"no_data"/"insufficient_data" response
tells the caller which filter was applied: ``"intraday"`` (default) or
``"full"``.

The ``live_sr`` field is ``null`` when ``include_live_sr`` is false. When
true, it carries:

    path             — resolved live_sr_levels.json path (or null)
    available        — true when the file exists and parsed
    today_iso        — today's IST date (for comparison)
    date             — the file's own "date" field
    is_today         — true when the file's date == today_iso
    candle_count     — the file's own "candle_count" field
    level_count      — number of levels persisted
    counts           — {resistance, support}
    first_level_time — earliest pivot_time
    last_level_time  — latest pivot_time
    last_candle      — the file's own "last_candle" object
    updated_at       — the file's own "updated_at" field
    levels           — the raw persisted level records (sorted by price)
    error            — "file_missing" / "unreadable: ..." / null

The underlying service (``services.live_sr_calc_service``) is
STATELESS — it never reads ``data/<...>/live_sr_levels.json`` unless
``include_live_sr=true`` and never writes to disk. It always fetches
fresh data:

    1. Historical 1-min candles from ``data/<...>/candles.json``
    2. Today's intraday 1-min candles from Upstox HistoryV3Api
    3. (OPTIONAL) ``data/<...>/live_sr_levels.json`` when
       ``include_live_sr=true`` — attached under ``live_sr``.

Results are deterministic for the same underlying data, so callers can
poll this endpoint safely.

Diagnostics
-----------
Every response includes ``sources.historical`` with:

    path                       — resolved file path (or null)
    candles                    — entries that survived normalisation
    candles_raw                — entries in the file
    candles_rejected_format    — entries the parser couldn't normalise
    candles_rejected_cutoff    — entries filtered by lookback_days
    declared_count             — the file's own "count" field, if present
    count_mismatch             — true when declared_count != candles_raw
    file_updated_at            — the file's own "updated_at" field
    relative_path              — the file's own "relative_path" field
    sample_structure           — shapes of the first 3 raw entries
                                 (present when parsing failed OR debug=true)
    error                      — human-readable reason on failure

The top-level ``warnings`` array flags intraday-mode filtering and any
``live_sr`` anomalies (file missing, stale date, engine disabled).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from core.logger import get_logger
from services.live_sr_calc_service import live_sr_calc_service

# 2026-11-06 — shared helpers for the isolation-store fast path and the
# active-tracked-set endpoint. Imported from ``routes.live_sr_routes``
# (no circular dependency: ``live_sr_routes`` does not import this
# module).
from routes.live_sr_routes import (
    _get_active_tracked_set,
    _isolation_live_sr_path,
    _now_iso,
    _read_isolation_live_sr,
)

logger = get_logger(__file__)

router = APIRouter(prefix="/live-sr/calc", tags=["live-sr-calc"])

_VALID_SOURCES = frozenset({"recompute", "isolation", "auto"})


# --------------------------------------------------------------------------- #
# Isolation-store helpers (2026-11-06)                                        #
# --------------------------------------------------------------------------- #
def _read_isolation_live_sr_by_key(
    instrument_key: str,
) -> dict[str, Any] | None:
    """Resolve ``instrument_key`` to a trading_symbol, then read its
    ``isolation/{symbol}_live_sr.json`` snapshot.

    Returns ``None`` when:
      * the registry has no entry for the key,
      * the entry has no trading_symbol,
      * the isolation file does not exist or cannot be parsed.
    """
    key = str(instrument_key or "").strip()
    if not key:
        return None
    try:
        from services.instrument_registry import instrument_registry
        entry = instrument_registry.get(key)
        if not entry:
            return None
        ts = entry.get("trading_symbol")
        if not ts:
            return None
    except Exception:
        return None
    return _read_isolation_live_sr(ts)


def _normalize_isolation_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Map an ``isolation/*_live_sr.json`` payload to the ``calculate()``
    response shape.

    The isolation store does not compute ``nearest`` hints, ``live_sr``,
    or per-source diagnostics — those are set to their null/empty
    defaults. Every field the dashboard or downstream consumer needs
    (``levels``, ``counts``, ``merged.candle_count``, ``params``,
    ``trading_symbol``, etc.) is carried through verbatim.
    """
    levels = payload.get("levels") or []
    counts = payload.get("counts") or {}
    refreshed = payload.get("refreshed_at") or payload.get("generated_at")

    return {
        "status": "ok",
        "mode": "intraday",
        "today_iso": payload.get("day"),
        "instrument_key": payload.get("instrument_key"),
        "trading_symbol": payload.get("trading_symbol"),
        "underlying": payload.get("underlying"),
        "strike": payload.get("strike_price"),
        "type": payload.get("strike_type"),
        "expiry": payload.get("expiry"),
        "is_index": False,
        "selector_source": "isolation_store",
        "params": payload.get("params") or {},
        "sources": {
            "historical": None,
            "intraday": None,
            "isolation": {
                "source": payload.get("source"),
                "refreshed_at": refreshed,
                "path": None,
            },
        },
        "merged": {
            "candle_count": payload.get("candle_count", 0),
            "first_candle": payload.get("first_candle"),
            "last_candle": payload.get("last_candle"),
            "current_price": None,
        },
        "levels": levels,
        "counts": {
            "total": len(levels),
            "total_unfiltered": len(levels),
            "resistance": int(counts.get("resistance") or 0),
            "support": int(counts.get("support") or 0),
            "today": len(levels),
        },
        "nearest": {"resistance_above": None, "support_below": None},
        "live_sr": None,
        "warnings": ["served_from_isolation_snapshot"],
        "candles": None,
        "source": "isolation",
        "generated_at": refreshed
                        or datetime.now(timezone.utc).isoformat(),
    }


def _read_isolation_result(
    instrument_key: str,
    source_mode: str,
) -> dict[str, Any] | None:
    """Try to serve a calculate() response from the isolation store.

    Returns the normalized response on success, or ``None`` when the
    isolation store has no snapshot for this key (the caller then
    either falls back to recompute, or returns a 404-style error for
    ``source="isolation"``).
    """
    payload = _read_isolation_live_sr_by_key(instrument_key)
    if payload is None:
        return None
    result = _normalize_isolation_response(payload)
    result["source_mode"] = source_mode
    return result


# --------------------------------------------------------------------------- #
# Shared plumbing                                                             #
# --------------------------------------------------------------------------- #
def _run_calc(
    instrument_key: str | None,
    underlying: str | None,
    strike: str | None,
    strike_type: str | None,
    left_bars: int | None,
    right_bars: int | None,
    lookback_days: int | None,
    only_today: bool,
    include_candles: bool,
    include_live_sr: bool,
    debug: bool,
    source: str = "recompute",
) -> dict[str, Any]:
    """Shared calc entry point.

    2026-11-06: ``source`` controls where the SR levels come from.

      * ``recompute`` (default) — call ``live_sr_calc_service.calculate()``
        (existing behaviour — always fresh from API/disk).
      * ``isolation`` — read the isolation store snapshot; return
        ``status="not_found"`` when no snapshot exists.
      * ``auto`` — try isolation store first, fall back to recompute.

    The isolation store only contains **today-only** levels (written by
    the scheduler minute tick with ``only_today=True``). When
    ``only_today=False`` is requested, both ``isolation`` and ``auto``
    are coerced to ``recompute`` — the isolation store cannot satisfy a
    "full merged window" request. ``source="isolation"`` in that case
    returns ``invalid_params``.
    """
    source_mode = (source or "recompute").strip().lower()
    if source_mode not in _VALID_SOURCES:
        return {
            "status": "invalid_params",
            "error": (
                f"invalid source={source!r}; expected one of "
                f"{sorted(_VALID_SOURCES)}"
            ),
            "source": source_mode,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    # ---- Isolation-store fast path -------------------------------- #
    if source_mode in ("isolation", "auto"):
        if not only_today:
            if source_mode == "isolation":
                return {
                    "status": "invalid_params",
                    "error": (
                        "source='isolation' requires only_today=true — "
                        "the isolation store only carries today's "
                        "confirmed levels"
                    ),
                    "source": "isolation",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }
            # auto with only_today=false → plain recompute
        else:
            key = str(instrument_key or "").strip()
            if not key and underlying and strike and strike_type:
                # Best-effort: resolve the triple to a key so the
                # isolation store can be consulted.
                try:
                    from services.instrument_registry import (
                        instrument_registry,
                    )
                    from services.normalization import (
                        normalize_strike,
                        normalize_strike_type,
                        normalize_underlying,
                    )
                    u = normalize_underlying(underlying)
                    s = normalize_strike(strike)
                    t = normalize_strike_type(strike_type)
                    if u and s and t:
                        entry = instrument_registry.resolve(u, s, t)
                        if entry:
                            key = str(entry.get("instrument_key") or "")
                except Exception:
                    logger.exception(
                        "[live-sr-calc] triple→key resolution failed "
                        "in isolation fast path",
                    )

            if key:
                iso_result = _read_isolation_result(key, source_mode)
                if iso_result is not None:
                    return iso_result
                if source_mode == "isolation":
                    return {
                        "status": "not_found",
                        "error": "no_isolation_snapshot",
                        "instrument_key": key,
                        "source": "isolation",
                        "warnings": [
                            f"no isolation snapshot for {key}; "
                            f"either the instrument has not been "
                            f"isolated today or the scheduler minute "
                            f"tick has not yet produced one"
                        ],
                        "generated_at": (
                            datetime.now(timezone.utc).isoformat()
                        ),
                    }
            elif source_mode == "isolation":
                return {
                    "status": "invalid_params",
                    "error": (
                        "source='isolation' requires instrument_key "
                        "or a resolvable (underlying, strike, type) "
                        "triple"
                    ),
                    "source": "isolation",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }

    # ---- Default: recompute -------------------------------------- #
    result = live_sr_calc_service.calculate(
        instrument_key=instrument_key,
        underlying=underlying,
        strike=strike,
        strike_type=strike_type,
        left_bars=left_bars,
        right_bars=right_bars,
        lookback_days=lookback_days,
        only_today=only_today,
        include_candles=include_candles,
        include_live_sr=include_live_sr,
        debug=debug,
    )
    # Annotate which source produced the data (new in 2026-11-06).
    if isinstance(result, dict):
        result.setdefault("source", "recompute")
        result["source_mode"] = source_mode
    return result


def _to_float_price(value: Any) -> Any:
    """Best-effort numeric conversion for level prices."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _compact_result(sel: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce a raw ``calculate()`` payload to just the essential SR levels.

    Produces two flat, price-ascending arrays (``supports`` and
    ``resistances``) plus counts and the latest confirmation timestamp.
    This is the shape the multi-instrument endpoint returns by default.
    """
    levels = raw.get("levels") or []

    supports_raw = [lv for lv in levels if lv.get("level") == "support"]
    resistances_raw = [lv for lv in levels if lv.get("level") == "resistance"]

    supports_raw.sort(key=lambda lv: _to_float_price(lv.get("price")) or 0)
    resistances_raw.sort(key=lambda lv: _to_float_price(lv.get("price")) or 0)

    last_confirmed = None
    for lv in levels:
        ct = lv.get("confirmed_time")
        if ct and (last_confirmed is None or str(ct) > str(last_confirmed)):
            last_confirmed = ct

    return {
        "instrument_key": raw.get("instrument_key") or sel.get("instrument_key"),
        "underlying": raw.get("underlying") or sel.get("underlying"),
        "strike": raw.get("strike") or sel.get("strike"),
        "type": raw.get("type") or sel.get("type"),
        "status": raw.get("status"),
        "mode": raw.get("mode"),
        "source": raw.get("source"),
        "supports": [_to_float_price(lv.get("price")) for lv in supports_raw],
        "resistances": [_to_float_price(lv.get("price")) for lv in resistances_raw],
        "support_count": len(supports_raw),
        "resistance_count": len(resistances_raw),
        "level_count": len(levels),
        "last_confirmed_time": last_confirmed,
        "warnings": raw.get("warnings") or [],
        "error": raw.get("error"),
    }


# --------------------------------------------------------------------------- #
# GET /live-sr/calc  — query-based                                            #
# --------------------------------------------------------------------------- #
@router.get("")
async def calc_by_query(
    instrument_key: str | None = Query(default=None),
    underlying: str | None = Query(default=None),
    strike: str | None = Query(default=None),
    strike_type: str | None = Query(default=None, alias="type"),
    left_bars: int | None = Query(default=None, ge=1, le=100),
    right_bars: int | None = Query(default=None, ge=1, le=100),
    lookback_days: int | None = Query(default=None, ge=1, le=30),
    only_today: bool = Query(default=True),        # <-- intraday by default
    include_candles: bool = Query(default=False),
    include_live_sr: bool = Query(default=False),  # <-- opt-in live_sr block
    debug: bool = Query(default=False),
    source: str = Query(default="recompute"),
) -> dict[str, Any]:
    if not instrument_key and not (underlying and strike and strike_type):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "selector_required",
                "message": (
                    "Pass either `instrument_key` or all three of "
                    "`underlying` + `strike` + `type`."
                ),
            },
        )
    return await asyncio.to_thread(
        _run_calc,
        instrument_key,
        underlying,
        strike,
        strike_type,
        left_bars,
        right_bars,
        lookback_days,
        only_today,
        include_candles,
        include_live_sr,
        debug,
        source,
    )


# --------------------------------------------------------------------------- #
# GET /live-sr/calc/{underlying}/{strike}/{strike_type}  — path-based         #
# --------------------------------------------------------------------------- #
@router.get("/{underlying}/{strike}/{strike_type}")
async def calc_by_path(
    underlying: str,
    strike: str,
    strike_type: str,
    left_bars: int | None = Query(default=None, ge=1, le=100),
    right_bars: int | None = Query(default=None, ge=1, le=100),
    lookback_days: int | None = Query(default=None, ge=1, le=30),
    only_today: bool = Query(default=True),        # <-- intraday by default
    include_candles: bool = Query(default=False),
    include_live_sr: bool = Query(default=False),  # <-- opt-in live_sr block
    debug: bool = Query(default=False),
    source: str = Query(default="recompute"),
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _run_calc,
        None,
        underlying,
        strike,
        strike_type,
        left_bars,
        right_bars,
        lookback_days,
        only_today,
        include_candles,
        include_live_sr,
        debug,
        source,
    )


# --------------------------------------------------------------------------- #
# POST /live-sr/calc/batch                                                    #
# --------------------------------------------------------------------------- #
@router.post("/batch")
async def calc_batch(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    selectors = payload.get("selectors") or []
    if not isinstance(selectors, list) or not selectors:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "selectors_required",
                "message": "`selectors` must be a non-empty list.",
            },
        )
    if len(selectors) > 50:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "too_many_selectors",
                "message": "Maximum 50 selectors per batch.",
            },
        )

    params_global = payload.get("params") or {}

    def _run() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for sel in selectors:
            if not isinstance(sel, dict):
                results.append({
                    "status": "invalid_input",
                    "error": "each selector must be an object",
                })
                continue

            params_item = sel.get("params") or {}
            merged_params = {**params_global, **params_item}

            try:
                results.append(
                    _run_calc(
                        instrument_key=sel.get("instrument_key"),
                        underlying=sel.get("underlying"),
                        strike=sel.get("strike"),
                        strike_type=sel.get("type"),
                        left_bars=merged_params.get("left_bars"),
                        right_bars=merged_params.get("right_bars"),
                        lookback_days=merged_params.get("lookback_days"),
                        only_today=merged_params.get("only_today", True),
                        include_candles=merged_params.get(
                            "include_candles", False,
                        ),
                        include_live_sr=merged_params.get(
                            "include_live_sr", False,
                        ),
                        debug=merged_params.get("debug", False),
                        source=merged_params.get("source", "recompute"),
                    )
                )
            except Exception as exc:
                logger.exception(
                    "[live-sr-calc] batch item failed for selector=%s", sel
                )
                results.append({
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "selector": sel,
                })
        return results

    results = await asyncio.to_thread(_run)

    return {
        "count": len(results),
        "results": results,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------- #
# POST /live-sr/calc/multi                                                    #
# --------------------------------------------------------------------------- #
@router.post("/multi")
async def calc_multi(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    instruments = payload.get("instruments") or []
    if not isinstance(instruments, list) or not instruments:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "instruments_required",
                "message": "`instruments` must be a non-empty list.",
            },
        )
    if len(instruments) > 50:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "too_many_instruments",
                "message": "Maximum 50 instruments per multi-call.",
            },
        )

    params_global = payload.get("params") or {}
    compact = bool(payload.get("compact", True))

    def _run() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for sel in instruments:
            if not isinstance(sel, dict):
                results.append({
                    "status": "invalid_input",
                    "error": "each instrument must be an object",
                    "instrument_key": None,
                    "underlying": None,
                    "strike": None,
                    "type": None,
                })
                continue

            params_item = sel.get("params") or {}
            merged_params = {**params_global, **params_item}

            try:
                raw = _run_calc(
                    instrument_key=sel.get("instrument_key"),
                    underlying=sel.get("underlying"),
                    strike=sel.get("strike"),
                    strike_type=sel.get("type"),
                    left_bars=merged_params.get("left_bars"),
                    right_bars=merged_params.get("right_bars"),
                    lookback_days=merged_params.get("lookback_days"),
                    only_today=merged_params.get("only_today", True),
                    include_candles=False,
                    include_live_sr=False,
                    debug=merged_params.get("debug", False),
                    source=merged_params.get("source", "recompute"),
                )
                if compact:
                    results.append(_compact_result(sel, raw))
                else:
                    results.append(raw)
            except Exception as exc:
                logger.exception(
                    "[live-sr-calc] multi item failed for selector=%s", sel
                )
                results.append({
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "instrument_key": sel.get("instrument_key"),
                    "underlying": sel.get("underlying"),
                    "strike": sel.get("strike"),
                    "type": sel.get("type"),
                    "supports": [],
                    "resistances": [],
                    "support_count": 0,
                    "resistance_count": 0,
                    "level_count": 0,
                    "last_confirmed_time": None,
                    "warnings": [],
                })
        return results

    results = await asyncio.to_thread(_run)

    return {
        "count": len(results),
        "compact": compact,
        "results": results,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------- #
# GET /live-sr/calc/active   (2026-11-06)                                     #
# --------------------------------------------------------------------------- #
@router.get("/active")
def active_tracked_set() -> dict[str, Any]:
    """Return the current active tracked set of SR instruments.

    Mirrors ``GET /live-sr/active`` — same underlying helper — so both
    modules expose identical shapes. The aggregation is:

      * ``config.LEVEL_FILTER_INDEXES`` — statically-configured indexes.
      * ``live_sr_calc_service.active_sr_instruments`` — dynamically
        registered isolated/opposite pairs.
      * Isolation store metadata + promotion sidecar.

    Includes ``per_instrument_snapshots`` — one entry per
    ``isolation/*_live_sr.json`` file currently on disk — so a caller
    can see exactly which instruments have a fresh (≤60s-old) SR
    snapshot.
    """
    return {
        "status": "ok",
        **_get_active_tracked_set(),
        "generated_at": _now_iso(),
    }


# --------------------------------------------------------------------------- #
# GET /live-sr/calc/isolation   (batch, 2026-11-06)                           #
# --------------------------------------------------------------------------- #
@router.get("/isolation")
async def calc_isolation_batch(
    all_instruments: bool = Query(default=False, alias="all"),
    trading_symbols: str | None = Query(default=None),
    instrument_keys: str | None = Query(default=None),
) -> dict[str, Any]:
    """Return the isolation-store snapshots for a set of instruments.

    Unlike ``/calc`` this endpoint never recomputes — it reads
    ``isolation/{safe(symbol)}_live_sr.json`` files directly. Each
    snapshot is returned in its native shape (not the ``calculate()``
    response shape) with the ``source="isolation"`` field added.

    Query parameters (mutually exclusive / composable):

        all=true                — return every snapshot in the store
        trading_symbols=a,b,c   — comma-separated trading symbols
        instrument_keys=k1,k2   — comma-separated instrument keys
                                  (resolved to trading symbols via
                                  ``instrument_registry``)

    When none are supplied the entire store is returned (same as
    ``all=true``) — it's a small set (isolated + opposite only).
    """
    iso_dir = _isolation_live_sr_path("__probe__").parent

    wanted_symbols: set[str] = set()
    if trading_symbols:
        wanted_symbols |= {
            s.strip() for s in trading_symbols.split(",") if s.strip()
        }
    if instrument_keys:
        try:
            from services.instrument_registry import instrument_registry
            for raw in instrument_keys.split(","):
                key = raw.strip()
                if not key:
                    continue
                entry = instrument_registry.get(key)
                if entry and entry.get("trading_symbol"):
                    wanted_symbols.add(entry["trading_symbol"])
        except Exception:
            logger.exception(
                "[live-sr-calc] isolation-batch: instrument_key lookup "
                "failed — ignoring instrument_keys filter",
            )

    # No filters or explicit all=true → return the whole store.
    return_all = bool(all_instruments) or not wanted_symbols

    snapshots: list[dict[str, Any]] = []
    if iso_dir.exists():
        for f in sorted(iso_dir.glob("*_live_sr.json")):
            data = _read_isolation_live_sr_from_path(f)
            if data is None:
                continue
            sym = data.get("trading_symbol")
            if not return_all and sym not in wanted_symbols:
                continue
            data.setdefault("source", "isolation")
            snapshots.append(data)

    return {
        "status": "ok",
        "isolation_dir": str(iso_dir),
        "count": len(snapshots),
        "snapshots": snapshots,
        "generated_at": _now_iso(),
    }


def _read_isolation_live_sr_from_path(path) -> dict[str, Any] | None:
    """Internal — read one isolation file by absolute path.

    Kept local so the batch endpoint does not depend on the
    trading-symbol→filename mapping (which the single-instrument
    endpoint already applies).
    """
    try:
        import json as _json
        if not path.exists():
            return None
        return _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception(
            "[live-sr-calc] unreadable isolation snapshot %s", path,
        )
        return None


# --------------------------------------------------------------------------- #
# GET /live-sr/calc/isolation/{trading_symbol}   (single, 2026-11-06)         #
# --------------------------------------------------------------------------- #
@router.get("/isolation/{trading_symbol}")
async def calc_isolation_single(trading_symbol: str) -> dict[str, Any]:
    """Return one instrument's isolation-store snapshot.

    Reads ``isolation/{safe(trading_symbol)}_live_sr.json``. Returns
    404 when the file is absent — this endpoint never falls back to
    recompute (that is what ``/calc?source=recompute`` is for).
    """
    path = _isolation_live_sr_path(trading_symbol)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "trading_symbol": trading_symbol,
                "resolved_path": str(path),
                "hint": (
                    "No isolation snapshot for this trading_symbol. "
                    "Either the instrument has not been isolated today, "
                    "or the scheduler minute tick has not produced a "
                    "snapshot yet."
                ),
            },
        )

    payload = _read_isolation_live_sr(trading_symbol)
    if payload is None:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "unreadable",
                "resolved_path": str(path),
            },
        )

    payload.setdefault("source", "isolation")
    return payload