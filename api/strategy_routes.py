"""Read-only views over independently scoped underlying strategy contexts."""

from fastapi import APIRouter, HTTPException

from services.strategy_context import get_strategy_context, get_strategy_contexts

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


@router.get("")
def list_strategy_contexts():
    """Return the enabled index contexts and their current runtime summaries."""
    summaries = []
    for context in get_strategy_contexts().values():
        state = context.snapshot()
        summaries.append({
            "underlying": context.underlying,
            "display_name": context.display_name,
            "enabled": context.enabled,
            "trading_date": context.trading_date,
            "underlying_instrument_key": context.index_instrument_key,
            "runtime_config": {
                "strike_step": context.option_config.get("strike_step"),
                "strike_from": context.option_config.get("strike_from"),
                "strike_to": context.option_config.get("strike_to"),
            },
            "option_contract_count": len(context.option_universe),
            "opening_range": state["opening_range"],
            "ema": state["ema"],
            "touch_state": state["touch_state"],
            "touch_events": state["touch_events"],
            "candidates": state["candidates"],
            "selected_instrument": state["selected_instrument"],
            "isolation": state["isolation"],
            "alerts": state["alerts"],
            "metadata": state["metadata"],
        })
    return {"strategies": summaries, "count": len(summaries)}


@router.get("/{underlying}")
def get_strategy_context_snapshot(underlying: str):
    """Return one index strategy snapshot, including its option universe."""
    context = get_strategy_context(underlying, active_only=True)
    if not context:
        raise HTTPException(status_code=404, detail="Enabled strategy not found")
    snapshot = context.snapshot()
    snapshot["runtime_config"] = {
        "enabled": context.enabled,
        "strike_step": context.option_config.get("strike_step"),
        "strike_from": context.option_config.get("strike_from"),
        "strike_to": context.option_config.get("strike_to"),
    }
    return snapshot
