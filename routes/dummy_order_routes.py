# routes/dummy_order_routes.py
"""Admin routes for the live-tracked dummy orders.

    GET  /admin/dummy-orders                  → full state (all today's orders)
    GET  /admin/dummy-orders/summary          → aggregates only (lightweight)
    GET  /admin/dummy-orders/path-b           → Path B execution state only
    GET  /admin/dummy-orders/candles          → intraday 1-min candles for one
                                                instrument (chart-popup data feed;
                                                G1 v4.4)
    POST /admin/dummy-orders/scan             → force a scan now (returns summary)
    POST /admin/dummy-orders/reset            → clear today's orders (testing)

No credentials, no tokens, no raw candle lists appear in any response
except the ``/candles`` endpoint, which is intentional: it powers the
``/isolated`` page's order-detail chart popup and only ever returns the
fields that popup actually renders (time, O/H/L/C/V per bar plus the
instrument's SR levels).

Every order carries ``dummy: true`` so a consumer cannot confuse a
dummy order with a real Upstox order.

G1 v4.2 (2026-10-29)
--------------------
Path B execution-path visibility:

* ``/dummy-orders`` and ``/dummy-orders/summary`` now embed a
  ``path_b_state`` block (``execution_path``, ``strategy_locked_today``,
  ``placed_keys``, ``open_orders``, ``closed_orders``) so the
  ``/isolated`` UI can render the multi-order badge, lock indicator,
  and per-order Rupee profit strip without a second round-trip.
  ``None`` when the dummy-order service's Path B state lookup fails.

* New ``/dummy-orders/path-b`` endpoint for admin tooling that wants
  only the Path B state (no full state payload). Returns
  ``status: "ok"`` + ``state`` on success, ``status: "error"`` +
  ``error`` on failure.

G1 v4.4 (2026-10-29)
--------------------
Order-detail chart popup data feed:

* New ``/dummy-orders/candles`` endpoint returns today's intraday
  1-minute candles for a single instrument key, plus that instrument's
  SR levels (bias-side post-isolation pivots). The ``/isolated``
  page's "Order chart" popup calls this endpoint when the operator
  clicks the chart icon next to any dummy order, then renders the
  candles with the order's entry / stop / target as horizontal price
  lines and the SR levels as markers.

  Query params:

      instrument_key   — required. The ``NSE_FO|…`` key of the order's
                         instrument. The response never echoes any
                         other field back except the instrument key.
      include_sr       — optional boolean, default ``true``. When
                         ``false``, the response omits the ``sr_levels``
                         block (useful for a faster fetch when the
                         caller only needs candles).

  Response shape::

      {
        "status": "ok",
        "instrument_key": "NSE_FO|53792",
        "candle_count": 375,
        "candles": [
          {"time": "2026-10-29T09:15:00+05:30",
           "open": 141.5, "high": 150.1,
           "low": 115.1, "close": 140.15, "volume": 0.0},
          ...
        ],
        "sr_levels": [
          {"level": "support",
           "price": 143.2,
           "pivot_time": "…",
           "confirmed_time": "…",
           "left_bars": 15, "right_bars": 3},
          ...
        ]
      }

  Failure shape::

      {"status": "error", "error": "…"}
      {"status": "empty", "instrument_key": "…", "candles": []}

  Never raises.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query

from services.dummy_order_service import dummy_order_service

router = APIRouter(prefix="/admin", tags=["dummy-orders"])


@router.get("/dummy-orders")
def get_dummy_orders() -> dict[str, Any]:
    """Return the full state of today's dummy orders.

    The persisted state already carries ``execution_path``,
    ``strategy_locked_today``, and ``path_b_placed_keys`` at the top
    level (see ``DummyOrderService._empty_state``). This route layers
    an additional ``path_b_state`` block on top — the aggregated
    Path B debug snapshot — so the ``/isolated`` UI can render the
    multi-order badge and per-order Rupee profit strip without a
    second round-trip to ``/dummy-orders/path-b``.
    """
    state = dummy_order_service.get_state()
    try:
        state["path_b_state"] = dummy_order_service.get_path_b_state()
    except Exception:
        state["path_b_state"] = None
    return state


@router.get("/dummy-orders/summary")
def get_dummy_orders_summary() -> dict[str, Any]:
    """Return aggregate stats only (for a lightweight poll).

    After G1 v4.2 (§10.2) this response also embeds ``path_b_state``
    so a lightweight poll can drive both the aggregate summary tiles
    and the Path B lock badge / placed-count indicator in one call.
    """
    summary = dummy_order_service.get_summary()
    try:
        summary["path_b_state"] = dummy_order_service.get_path_b_state()
    except Exception:
        summary["path_b_state"] = None
    return summary


# --------------------------------------------------------------------------- #
# NEW (G1 v4.2 §10.3): Path B state endpoint                                  #
# --------------------------------------------------------------------------- #
@router.get("/dummy-orders/path-b")
def get_dummy_orders_path_b() -> dict[str, Any]:
    """Return only the Path B execution state.

    Read-only endpoint used by admin tooling that wants only the Path B
    snapshot without the heavier ``/dummy-orders`` state payload. Fields:

        execution_path          — "A_self_trigger" | "B_external_trigger" | None
        strategy_locked_today   — bool (set once a Path B trade hits target)
        placed_keys             — dedup keys of already-placed Path B orders
        open_orders             — count of OPEN Path B orders
        closed_orders           — count of closed Path B orders

    Returns ``{"status": "ok", "state": {...}}`` on success, or
    ``{"status": "error", "error": ...}`` if the dummy-order service is
    unavailable. Never raises.
    """
    try:
        return {
            "status": "ok",
            "state": dummy_order_service.get_path_b_state(),
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }


# --------------------------------------------------------------------------- #
# NEW (G1 v4.4): Chart-popup candles + SR levels                              #
# --------------------------------------------------------------------------- #
@router.get("/dummy-orders/candles")
def get_dummy_orders_candles(
    instrument_key: str = Query(..., min_length=1),
    include_sr: bool = Query(default=True),
) -> dict[str, Any]:
    """Return today's intraday 1-min candles for the chart popup.

    The ``/isolated`` page's "Order chart" popup calls this endpoint
    when the operator clicks the chart icon next to any dummy order.
    The response carries every 1-minute bar for the given instrument
    (09:15 → 15:29 IST) plus, when ``include_sr`` is true, the bias-
    side SR levels that spawned (or could spawn) orders at that strike.

    The response is deliberately narrow: only fields the popup actually
    renders are returned. Nothing else from the internal SR engine
    state leaks through this endpoint.

    Never raises. Every failure mode lands in the returned ``status``
    field:

        ok     — candles + optional SR levels returned
        empty  — no candles for that instrument yet (pre-open, or dead key)
        error  — unexpected exception during fetch
    """
    key = str(instrument_key or "").strip()
    if not key:
        return {"status": "error", "error": "instrument_key is required"}

    try:
        from services.live_sr_calc_service import live_sr_calc_service
        raw_candles = live_sr_calc_service.fetch_intraday_candles_for_entry(key)
    except Exception as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "instrument_key": key,
        }

    # Normalise to the popup's shape, dropping anything unparseable.
    candles: list[dict[str, Any]] = []
    for c in raw_candles or []:
        if not isinstance(c, dict):
            continue
        try:
            candles.append({
                "time": str(c.get("time") or ""),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": float(c.get("volume") or 0.0),
            })
        except (KeyError, TypeError, ValueError):
            continue

    if not candles:
        return {
            "status": "empty",
            "instrument_key": key,
            "candle_count": 0,
            "candles": [],
        }

    result: dict[str, Any] = {
        "status": "ok",
        "instrument_key": key,
        "candle_count": len(candles),
        "candles": candles,
    }

    # ---- Optional SR levels ------------------------------------------- #
    if include_sr:
        sr_levels: list[dict[str, Any]] = []
        try:
            from services.live_sr_levels_service import live_sr_levels_service
            sr_payload = live_sr_levels_service.get_levels(key)
            if isinstance(sr_payload, dict):
                for lvl in sr_payload.get("levels") or []:
                    if not isinstance(lvl, dict):
                        continue
                    try:
                        sr_levels.append({
                            "level": str(lvl.get("level") or ""),
                            "price": float(lvl["price"]),
                            "pivot_time": str(lvl.get("pivot_time") or ""),
                            "confirmed_time": str(lvl.get("confirmed_time") or ""),
                            "left_bars": lvl.get("left_bars"),
                            "right_bars": lvl.get("right_bars"),
                        })
                    except (KeyError, TypeError, ValueError):
                        continue
        except Exception:
            sr_levels = []
        result["sr_levels"] = sr_levels

    return result


@router.post("/dummy-orders/scan")
async def scan_dummy_orders() -> dict[str, Any]:
    """Force a scan now (create-if-new + update + save)."""
    return await asyncio.to_thread(
        dummy_order_service.run_once, "manual",
    )


@router.post("/dummy-orders/reset")
async def reset_dummy_orders() -> dict[str, Any]:
    """Clear today's orders. Admin-only; used for testing."""
    return await asyncio.to_thread(dummy_order_service.reset)