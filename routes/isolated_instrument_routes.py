# routes/isolated_instrument_routes.py
"""Admin routes for the level-isolation engine.

    GET  /admin/level-isolation/status                    — engine status + last isolated
    GET  /admin/level-isolation/isolated                  — just the isolated record
    GET  /admin/level-isolation/detail                    — UI-facing detail snapshot
                                                            (includes `sr_tracking`,
                                                            `active` if promoted)
    GET  /admin/level-isolation/sr-tracking               — SR tracker state only
    GET  /admin/level-isolation/strategy-signal           — entry-signal block
    POST /admin/level-isolation/apply-promotion           — promote the ARMED instrument
    POST /admin/level-isolation/clear-promotion           — remove today's promotion
    GET  /admin/level-isolation/search-instruments        — search the registry
    GET  /admin/level-isolation/instrument-sr/{key}       — SR levels for a searched
                                                            instrument, tagged against
                                                            the official isolation time
    POST /admin/level-isolation/backfill                  — re-run the intraday replay
    POST /admin/level-isolation/reset                     — clear the day's state
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
    (promoted) instrument alongside the original trigger instrument.
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

    return data


@router.get("/sr-tracking")
def get_sr_tracking() -> dict[str, Any]:
    return isolated_sr_tracker_service.get_detail()


# --------------------------------------------------------------------------- #
# Strategy entry signal                                                       #
# --------------------------------------------------------------------------- #
@router.get("/strategy-signal")
def get_strategy_signal() -> dict[str, Any]:
    """Pure compute — no side effects. Returns the promotion block so the
    caller can decide whether to POST to /apply-promotion.
    """
    return strategy_entry_signal_service.compute()


@router.post("/apply-promotion")
async def apply_promotion() -> dict[str, Any]:
    """Apply the promotion recommended by the current strategy signal.

    Recomputes the signal server-side and, if it recommends a promotion
    that has not been applied yet, promotes the target instrument to be
    the active isolated instrument.  Returns the promotion result plus
    the fresh detail (so a caller can reload the page with one round-trip).
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


@router.post("/reset")
async def reset() -> dict[str, Any]:
    # Clear the promotion first so the SR tracker resets to the base state.
    try:
        isolated_instrument_promotion_service.clear()
    except Exception:
        pass
    return await asyncio.to_thread(level_isolated_instrument_service.reset)