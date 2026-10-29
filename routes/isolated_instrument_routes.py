# routes/isolated_instrument_routes.py
"""Admin routes for the level-isolation engine.

    GET  /admin/level-isolation/status                    — engine status + last isolated
    GET  /admin/level-isolation/isolated                  — just the isolated record
    GET  /admin/level-isolation/detail                    — UI-facing detail snapshot
                                                            (includes `sr_tracking`,
                                                            `active` if promoted,
                                                            with confirmed_time_price,
                                                            `active.frozen_entry`
                                                            after G1 v4.1 §5.E, and
                                                            `dummy_order_path_b_state`
                                                            after G1 v4.2 §7.1)
    GET  /admin/level-isolation/sr-tracking               — SR tracker state only
                                                            (pre & post isolation levels)
    GET  /admin/level-isolation/strategy-signal           — entry-signal block
                                                            (includes `frozen_entry`
                                                            after G1 v4.1 §5.E)
    GET  /admin/level-isolation/path-b-state              — Path B execution state
                                                            (multi-order SR watch,
                                                            lock flag, placed keys;
                                                            G1 v4.2 §7.2)
    POST /admin/level-isolation/apply-promotion           — promote the ARMED instrument
                                                            (result includes the
                                                            frozen entry block)
    POST /admin/level-isolation/clear-promotion           — remove today's promotion
    GET  /admin/level-isolation/search-instruments        — search the registry
    GET  /admin/level-isolation/instrument-sr/{key}       — SR levels for a searched
                                                            instrument, tagged against
                                                            the official isolation time
    POST /admin/level-isolation/backfill                  — re-run the intraday replay
    POST /admin/level-isolation/refresh-snapshot          — re-collect the isolated
                                                            instrument's own data at the
                                                            touch (no re-decision)
    POST /admin/level-isolation/reset                     — clear the day's state

G1 v4.1 (2026-10-29)
--------------------
The ``/isolated`` page analysis flagged that the strategy-entry card
was recomputing on every 10 s template poll, causing the "entry price
drifts" symptom (§3 / §4.4 / §4.5 of the analysis). Two route-level
changes fix this without touching the arbitration logic:

* ``/strategy-signal`` now overlays ``frozen_entry`` from the promotion
  record onto its response when a promotion has been applied (§5.E).
  The template reads ``signal.frozen_entry`` if present; otherwise it
  falls back to the computed per-side entries — which is the correct
  behaviour for *un-promoted* candidates, whose entry price should
  still be live-derived because the decision has not yet been frozen.

* ``/detail`` returns ``active.frozen_entry`` for free (it already
  returns the full ``active`` record; the promotion service now
  populates the ``frozen_entry`` key on that record, so this route
  surfaces it without any code change here).

Both changes are additive: when no promotion exists,
``signal.frozen_entry`` is ``None`` and ``active.frozen_entry`` is
absent — the template falls through to the existing computed path.

The §5.B fix in ``strategy_entry_signal_service._entry_price_for()``
(prefer candle close at ``confirmed_time_fast`` over live LTP) is what
makes the *frozen* entry a correct historical value; §5.E captures
that correct value once and pins it for the rest of the session.

Re-promotion caveat
-------------------
If ``STRATEGY_ENTRY_ALLOW_RE_PROMOTION`` is True (default: False), a
re-promotion overwrites ``frozen_entry`` with the new winner's block.
That is intentional: re-promotion is an explicit opt-in config that
says "a later win should override the earlier one". The default
single-promotion-per-day policy keeps the first frozen entry stable
for the rest of the session, matching what the dummy-order service
already does (create-once, update-status-only).

G1 v4.2 (2026-10-29)
--------------------
Path B visibility additions:

* ``/detail`` now embeds ``dummy_order_path_b_state`` — the session-
  level Path B snapshot (``execution_path``, ``strategy_locked_today``,
  ``placed_keys``, ``open_orders``, ``closed_orders``) so the
  ``/isolated`` template can render the multi-order badge, the lock
  indicator, and the per-order Rupee profit strip without a second
  round-trip. ``None`` when the dummy-order service import fails.

* New ``/path-b-state`` endpoint for admin tooling that wants only the
  Path B state (no full detail payload). Returns ``status: "ok"`` +
  ``state`` on success, ``status: "error"`` + ``error`` on failure.

Both additions are strictly read-only and defensive: any exception
within the Path B state lookup degrades the field to ``None`` (for
``/detail``) or to an error status (for ``/path-b-state``) without
affecting the rest of the response. Path A callers are unaffected —
``dummy_order_path_b_state`` will simply show an empty/no-isolation
snapshot for Path A sessions.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query

from services.isolated_instrument_promotion_service import (
    isolated_instrument_promotion_service,
)
from services.isolated_sr_tracker_service import (
    isolated_sr_tracker_service,
)
from services.level_isolated_instrument_service import (
    level_isolated_instrument_service,
)
from services.strategy_entry_signal_service import (
    strategy_entry_signal_service,
)

router = APIRouter(prefix="/admin/level-isolation", tags=["level-isolation"])


@router.get("/status")
def get_status() -> dict[str, Any]:
    payload = level_isolated_instrument_service.last_status()
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    return payload


@router.get("/isolated")
def get_isolated() -> dict[str, Any]:
    payload = level_isolated_instrument_service.last_status()
    return {
        "day": payload.get("day"),
        "isolated": payload.get("isolated"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/detail")
def get_detail() -> dict[str, Any]:
    """UI-facing detail snapshot — consumed by ``/isolated``.

    In addition to the base isolation detail, this endpoint attaches the
    current promotion record (if any) so the UI can show the *active*
    (promoted) instrument alongside the original trigger instrument,
    including confirmed_time_price across all tracked SR levels.

    After G1 v4.1 (§5.E) the promotion record carries a ``frozen_entry``
    block: ``{price, price_source, stop, target, time, frozen_at}``,
    captured at the moment the winning side was armed. The ``/isolated``
    template reads ``active.frozen_entry`` from this payload to render
    the entry-signal card without needing a separate ``/strategy-signal``
    round-trip — and without the entry price drifting on the 10 s poll
    (which was §3 / §4.4 of the analysis).

    After G1 v4.2 (§7.1) this endpoint also embeds
    ``dummy_order_path_b_state`` — the session-level Path B snapshot
    from the dummy-order service (execution_path, strategy_locked_today,
    placed_keys, open_orders, closed_orders). The ``/isolated`` template
    uses it to render the multi-order badge, lock indicator, and
    per-order Rupee profit strip without a second round-trip. ``None``
    when the dummy-order service is unavailable.
    """
    data = level_isolated_instrument_service.get_detail()

    try:
        active = isolated_instrument_promotion_service.read_active()
    except Exception:
        active = None

    if active:
        data["active"] = active
        try:
            target_key = str(active.get("to_instrument_key") or "").strip()
            if target_key:
                comparison = (
                    level_isolated_instrument_service
                    .get_instrument_sr_comparison(target_key)
                )
                data["active_live_sr_levels"] = comparison.get(
                    "live_sr_levels"
                )
            else:
                data["active_live_sr_levels"] = None
        except Exception:
            data["active_live_sr_levels"] = None
    else:
        data["active"] = None
        data["active_live_sr_levels"] = None

    # G1 v4.1 — expose Path B state so the /isolated page can show
    # the multi-order badge, lock state, and per-order Rupee profit.
    try:
        from services.dummy_order_service import dummy_order_service
        data["dummy_order_path_b_state"] = (
            dummy_order_service.get_path_b_state()
        )
    except Exception:
        data["dummy_order_path_b_state"] = None

    return data


@router.get("/sr-tracking")
def get_sr_tracking() -> dict[str, Any]:
    """Return SR tracker state containing pre_isolation_levels and
    post_isolation_levels, each carrying confirmed_time_price.
    """
    return isolated_sr_tracker_service.get_detail()


# --------------------------------------------------------------------------- #
# Strategy entry signal                                                       #
# --------------------------------------------------------------------------- #
@router.get("/strategy-signal")
def get_strategy_signal() -> dict[str, Any]:
    """Return the entry-signal block.

    Pure compute — no side effects — but with one G1 v4.1 (§5.E)
    addition: when a promotion has been applied, the response includes a
    top-level ``frozen_entry`` block copied from the promotion record:

        {
            "frozen_entry": {
                "price":        <float>,
                "price_source": "candle_close_at_confirmed_fast" | "live_ltp",
                "stop":         <float>,
                "target":       <float>,
                "time":         <ISO-8601>,
                "frozen_at":    <ISO-8601>,
            },
            "frozen": true,
            ...  # computed signal fields below, unchanged
        }

    The ``/isolated`` template renders the entry-signal card from
    ``frozen_entry`` when present, and from the computed per-side
    entries otherwise. This pins the card to the value captured at
    arming rather than the value that would be recomputed against
    the current live LTP — the drifting behaviour flagged in analysis
    §3 / §4.4 / §4.5.

    When no promotion has been applied, ``frozen_entry`` is ``None``
    and ``frozen`` is ``False`` — the caller should then render the
    computed per-side entries (which is correct because an un-promoted
    candidate's entry price is still being live-derived).

    The compute() call is still performed unconditionally so that the
    live arm / reject status, promotion recommendation, and per-side
    level snapshot stay fresh on the template's 10 s poll. Only the
    three "settled" fields — entry price, stop, target — come from the
    frozen entry when one exists.

    G1 v4.2 note: when the session is in Path B, ``compute()`` itself
    returns ``status="path_b_mode"`` with empty ``candidates`` and a
    ``path_b`` block. That response is passed through unchanged; the
    frozen-entry overlay below only applies when a promotion exists,
    which (correctly) never happens in Path B.
    """
    signal = strategy_entry_signal_service.compute()

    # G1 v4.1 §5.E — overlay the frozen entry from the active promotion
    # record when one exists. The frozen values are the authoritative
    # entry / stop / target captured at arming time.
    frozen: dict[str, Any] | None = None
    try:
        active = isolated_instrument_promotion_service.read_active()
    except Exception:
        active = None

    if active and isinstance(active.get("frozen_entry"), dict):
        frozen = dict(active["frozen_entry"])

    signal["frozen_entry"] = frozen
    signal["frozen"] = frozen is not None
    return signal


# --------------------------------------------------------------------------- #
# NEW (G1 v4.2 §7.2): Path B state endpoint                                   #
# --------------------------------------------------------------------------- #
@router.get("/path-b-state")
def get_path_b_state() -> dict[str, Any]:
    """Return the Path B execution state (multi-order SR watch).

    Read-only endpoint used by admin tooling that wants only the Path B
    snapshot without the heavier ``/detail`` payload. Fields:

        execution_path          — "A_self_trigger" | "B_external_trigger" | None
        strategy_locked_today   — bool (set once a Path B trade hits target)
        placed_keys             — dedup keys of already-placed Path B orders
        open_orders             — count of OPEN Path B orders
        closed_orders           — count of closed Path B orders

    Returns ``{"status": "ok", "state": {...}, "generated_at": ...}`` on
    success, or ``{"status": "error", "error": ..., "generated_at": ...}``
    if the dummy-order service is unavailable. Never raises.
    """
    try:
        from services.dummy_order_service import dummy_order_service
        return {
            "status": "ok",
            "state": dummy_order_service.get_path_b_state(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }


@router.post("/apply-promotion")
async def apply_promotion() -> dict[str, Any]:
    """Apply the promotion recommended by the current strategy signal.

    Recomputes the signal server-side and, if it recommends a promotion
    that has not been applied yet, promotes the target instrument to be
    the active isolated instrument. Returns the promotion result plus
    the fresh detail (so a caller can reload the page with one round-trip).

    After G1 v4.1 (§5.E) the returned ``result`` block carries a
    ``frozen_entry`` key (populated by
    ``isolated_instrument_promotion_service.apply_promotion()``) with
    the price / stop / target / source captured at the moment of
    promotion. ``result["detail"]["active"]["frozen_entry"]`` carries
    the same block via the refreshed detail snapshot, so both paths
    surface the frozen entry consistently.

    G1 v4.2 note: when the session is in Path B, both the promotion
    service (via its own Path B guard) and the strategy-entry service
    (which returns ``status="path_b_mode"``) will decline to recommend
    a promotion. This endpoint therefore returns a ``promoted: False``
    result with ``reason="path_b_external_trigger"`` (or
    ``"no_valid_signal"``, depending on which guard fires first) without
    changing anything. That is the intended behaviour — Path B never
    promotes.
    """
    def _run() -> dict[str, Any]:
        signal = strategy_entry_signal_service.compute()
        result = isolated_instrument_promotion_service.apply_promotion(signal)
        result["signal"] = signal
        try:
            result["detail"] = get_detail()
        except Exception as exc:
            result["detail_error"] = f"{type(exc).__name__}: {exc}"
        return result

    return await asyncio.to_thread(_run)


@router.post("/clear-promotion")
async def clear_promotion() -> dict[str, Any]:
    """Remove today's promotion record.  Used by tests / manual reset."""
    return await asyncio.to_thread(
        isolated_instrument_promotion_service.clear
    )


# --------------------------------------------------------------------------- #
# Instrument search + comparison                                              #
# --------------------------------------------------------------------------- #
@router.get("/search-instruments")
def search_instruments(
    query: str = Query(..., min_length=1, max_length=50),
    limit: int = Query(default=15, ge=1, le=50),
) -> dict[str, Any]:
    q = str(query or "").strip()
    if not q:
        return {
            "query": query,
            "count": 0,
            "total_matches": 0,
            "results": [],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    q_lower = q.lower()
    q_upper = q.upper()

    from services.instrument_registry import instrument_registry

    scored: list[dict[str, Any]] = []
    for e in instrument_registry.all():
        sym = str(e.get("trading_symbol") or "")
        sym_lower = sym.lower()
        und = str(e.get("underlying") or "").lower()
        stk = str(e.get("strike_price") or "")
        typ = str(e.get("strike_type") or "").upper()

        score = 0
        if q_lower and q_lower in sym_lower:
            score += 10
            if sym_lower.startswith(q_lower):
                score += 5
        if q_lower and q_lower in und:
            score += 3
        if stk and (q_lower == stk or stk.startswith(q_lower)):
            score += 4
        if typ and q_upper == typ:
            score += 2

        if score > 0:
            scored.append({
                "_score": score,
                "instrument_key": e.get("instrument_key"),
                "trading_symbol": e.get("trading_symbol"),
                "underlying": e.get("underlying"),
                "strike": e.get("strike_price"),
                "type": e.get("strike_type"),
                "expiry": e.get("expiry"),
                "is_index": bool(e.get("is_index")),
            })

    scored.sort(key=lambda x: (
        -int(x.get("_score") or 0),
        str(x.get("trading_symbol") or ""),
    ))

    trimmed = scored[:limit]
    for r in trimmed:
        r.pop("_score", None)

    return {
        "query": query,
        "count": len(trimmed),
        "total_matches": len(scored),
        "results": trimmed,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/instrument-sr/{instrument_key:path}")
def get_instrument_sr(instrument_key: str) -> dict[str, Any]:
    return level_isolated_instrument_service.get_instrument_sr_comparison(
        instrument_key
    )


@router.post("/backfill")
async def run_backfill() -> dict[str, Any]:
    from token_tasks.level_isolated_instrument_task import (
        run_level_isolation_backfill_manual,
    )
    return await asyncio.to_thread(run_level_isolation_backfill_manual)


@router.post("/refresh-snapshot")
async def refresh_snapshot() -> dict[str, Any]:
    """Re-collect the isolated instrument's own data at the touch.

    The ``/isolated`` page's "Refresh Snapshot" button targets this
    endpoint.  It is a snapshot-only backfill — the isolation decision
    itself is never changed.  It exists for records created before the
    snapshot feature was deployed, whose ``instrument_ltp`` /
    ``instrument_touch_ohlc`` / ``instrument_first_candle`` /
    ``instrument_or_levels`` fields are empty.  The 5-minute recheck
    job performs the same operation on its own, but this gives the
    operator an immediate way to populate the fields without waiting.
    """
    return await asyncio.to_thread(
        level_isolated_instrument_service.refresh_snapshot
    )


@router.post("/reset")
async def reset() -> dict[str, Any]:
    # Clear the promotion first so the SR tracker resets to the base state.
    try:
        isolated_instrument_promotion_service.clear()
    except Exception:
        pass
    return await asyncio.to_thread(level_isolated_instrument_service.reset)