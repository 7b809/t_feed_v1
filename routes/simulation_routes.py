# routes/simulation_routes.py
"""Admin routes for the post-close dummy-order simulation.

    GET  /admin/simulation/run
        Run the simulation for the current isolated instrument.
        Stateless; no disk writes, no background jobs, no Telegram.

    GET  /admin/simulation/regime-check
        Diagnostic: report the regime classification for the current
        isolated instrument without fetching intraday candles or
        replaying. Useful when the operator wants to see how the
        regime logic classifies the entry price without paying for
        the intraday API call.

Response shape
--------------
The `/run` endpoint returns the payload produced by
``services.simulation_service.SimulationService.run()`` — see that
module's docstring for the shape. On any failure, the response still
returns HTTP 200 with ``status`` in:

    "ok" / "disabled" / "no_signal" / "no_or_levels" /
    "no_candles" / "error"

No credentials, no tokens, no raw candle lists appear in any response.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter

from services.simulation_service import simulation_service
from services.strategy_entry_signal_service import (
    strategy_entry_signal_service,
)

router = APIRouter(prefix="/admin/simulation", tags=["simulation"])

@router.get("/run")
async def run_simulation() -> dict[str, Any]:
    """Run the dummy-order replay for the current isolated instrument."""
    return await asyncio.to_thread(simulation_service.run)

@router.get("/regime-check")
def regime_check() -> dict[str, Any]:
    """Report the regime classification without fetching candles.

    Lightweight diagnostic. Reuses the entry-signal service to resolve
    the current entry price and the simulation's classifier to determine
    the regime bucket. No replay, no API call.
    """
    from core import config
    from services.simulation_service import _classify_regime, _safe_float

    if not getattr(config, "SIMULATION_ENABLED", True):
        return {"status": "disabled"}

    signal = strategy_entry_signal_service.compute()
    if not isinstance(signal, dict) or signal.get("status") != "ok":
        return {
            "status": "no_signal",
            "reason": (signal or {}).get("status") or "no_signal",
        }

    chosen = None
    for c in (signal.get("candidates") or []):
        if c.get("chosen"):
            chosen = c
            break
    if chosen is None or chosen.get("state") != "armed":
        return {"status": "no_signal", "reason": "no_candidate_armed"}

    entry = chosen.get("entry") or {}
    entry_price = _safe_float(entry.get("price"))
    if entry_price is None:
        return {"status": "no_signal", "reason": "incomplete_entry_block"}

    instrument_key = str(chosen.get("instrument_key") or "").strip()
    side = chosen.get("side")
    or_levels = simulation_service._read_option_or_levels(instrument_key, side)
    if or_levels is None:
        return {
            "status": "no_or_levels",
            "instrument_key": instrument_key,
        }

    regime = _classify_regime(entry_price, or_levels, None)

    return {
        "status": "ok",
        "instrument_key": instrument_key,
        "trading_symbol": chosen.get("trading_symbol"),
        "side": side,
        "entry_price": entry_price,
        "or_levels": or_levels,
        "regime": regime,
    }