"""Strategy dashboard routes.

    GET /dashboard                        -> HTML dashboard page
    GET /ui/dashboard/strategy            -> JSON payload (auto-refreshed)

The dashboard surfaces the current stage-4 pick (if any) together with
the pipeline timeline and per-stage summaries, so an operator can see
at a glance:

    * which strike was selected,
    * its strike / type / expiry / instrument key,
    * when it moved through each strategy stage,
    * the aggregate counters for stage 1 → 4,
    * the per-contract stage-2 → stage-3 decision tree (why each
      isolated contract was or was not activated).

Data is read from the on-disk summary files written by each stage:

    data/strategy/stage_1_initial/stage_1_summary.json
    data/strategy/stage_2_isolation/strategy_isolation.json
    data/strategy/stage_3_r2_activated/stage_3_summary.json
    data/strategy/stage_3_r2_activated/stage_3_decisions.json
    data/strategy/stage_4_order/stage_4_summary.json

If any stage has not run yet, its section reports "waiting".

Stage-3 decision basis
----------------------
Stage 3 runs in **per-contract** mode: each option contract is judged
against its own 09:15–09:20 window high vs its own R2 (derived from its
own 09:15 opening-range candle). The parent index's R2 / window high
are recorded **only as informational context**.

This route therefore forwards, alongside the compact per-contract
decision tree, the following per-index aggregate fields so the
dashboard can lead with them:

    decision_basis           : "per_contract"
    contracts_reached_r2     : number of contracts that hit their own R2
    best_contract_gap_to_r2  : max(contract_window_high - contract_R2)
    best_contract            : the contract that produced that gap

The legacy ``r2_value`` / ``window_high`` / ``gap_to_r2`` fields are
still forwarded (they carry the *index's* values), but the template
presents them on a secondary "index context" line so they cannot be
mistaken for the decision basis.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core import config
from services.instrument_registry import instrument_registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dashboard"])

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES_DIR = _PROJECT_ROOT / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

def _base() -> Path:
    return Path(config.STRATEGY_BASE_DIR)

def _enrich_with_registry(pick: dict[str, Any]) -> dict[str, Any]:
    """Look up the pick's instrument in the registry to fill in expiry /
    trading_symbol / underlying when the stage-4 order record did not
    carry them itself.
    """
    if not pick:
        return pick

    for key_field in ("upstox_instrument_key", "instrument_key"):
        key = pick.get(key_field)
        if not key:
            continue
        entry = instrument_registry.get(key)
        if entry:
            if not pick.get("expiry"):
                pick["expiry"] = entry.get("expiry")
            if not pick.get("trading_symbol"):
                pick["trading_symbol"] = entry.get("trading_symbol")
            if not pick.get("underlying"):
                pick["underlying"] = entry.get("underlying")
            break

    return pick

def _collect_dashboard_payload() -> dict[str, Any]:
    base = _base()

    stage1 = _read_json(
        base / config.STRATEGY_STAGE_1_NAME / "stage_1_summary.json"
    ) or {}
    stage2 = _read_json(
        base / config.STRATEGY_STAGE_2_NAME / "strategy_isolation.json"
    ) or {}
    stage3 = _read_json(
        base / config.STRATEGY_STAGE_3_NAME / "stage_3_summary.json"
    ) or {}
    stage3_decisions = _read_json(
        base / config.STRATEGY_STAGE_3_NAME / "stage_3_decisions.json"
    ) or {}
    stage4 = _read_json(
        base / config.STRATEGY_STAGE_4_NAME / "stage_4_summary.json"
    ) or {}

    # ---- Stage 4 — current pick --------------------------------------- #
    orders = stage4.get("orders") or []
    current_pick: dict[str, Any] | None = (
        _enrich_with_registry(dict(orders[0])) if orders else None
    )

    # ---- Stage 3 — activations ---------------------------------------- #
    activated = stage3.get("activated") or []
    first_activated: dict[str, Any] | None = None
    last_activated: dict[str, Any] | None = None
    if activated:
        ordered = sorted(
            activated, key=lambda a: str(a.get("touched_at") or "")
        )
        first_activated = ordered[0]
        last_activated = ordered[-1]

    # ---- Stage 1 counts ---------------------------------------------- #
    stage1_indexes = stage1.get("indexes") or []
    stage1_contracts = sum(
        int(i.get("contract_count") or 0) for i in stage1_indexes
    )
    stage1_strikes = sum(
        int(i.get("strike_count") or 0) for i in stage1_indexes
    )

    # ---- Timeline ---------------------------------------------------- #
    def _status(ok_value: Any, missing: str = "waiting") -> str:
        if ok_value is None:
            return missing
        return "ok"

    timeline = [
        {
            "stage": "stage_1",
            "name": "Selection",
            "at": stage1.get("generated_at"),
            "status": _status(stage1.get("generated_at")),
        },
        {
            "stage": "stage_2",
            "name": "Isolation",
            "at": stage2.get("generated_at"),
            "status": _status(stage2.get("generated_at")),
        },
        {
            "stage": "stage_3",
            "name": "R2 Activation",
            "at": first_activated.get("touched_at") if first_activated else None,
            "status": _status(
                first_activated.get("touched_at") if first_activated else None
            ),
        },
        {
            "stage": "stage_4",
            "name": "Order Placed",
            "at": current_pick.get("placed_at") if current_pick else None,
            "status": (
                "ok" if current_pick else "waiting"
            ),
        },
    ]

    # ---- Stage 3 decision tree (for the audit section) --------------- #
    # Compact representation: keep the fields the dashboard actually
    # renders; drop everything else so the payload stays small.
    #
    # NOTE: the *decision basis* is per-contract. The header of each
    # index card leads with per-contract aggregates
    # (``decision_basis``, ``contracts_reached_r2``,
    # ``best_contract_gap_to_r2``), and the index-level numbers
    # (``r2_value``, ``window_high``, ``gap_to_r2``) are demoted to a
    # secondary "index context" line in the template.
    decision_tree: list[dict[str, Any]] = []
    for idx in (stage3_decisions.get("indexes") or []):
        contracts_compact = []
        for c in (idx.get("contracts") or []):
            contracts_compact.append({
                "instrument_key": c.get("instrument_key"),
                "upstox_instrument_key": c.get("upstox_instrument_key"),
                "strike_price": c.get("strike_price"),
                "strike_type": c.get("strike_type"),
                "status": c.get("status"),
                "activated": c.get("activated"),
                "reason_code": c.get("reason_code"),
                "reason": c.get("reason"),
                # ---- per-contract numbers used by the audit table ---- #
                "contract_r2": c.get("contract_r2"),
                "contract_window_high": c.get("contract_window_high"),
                "contract_window_low": c.get("contract_window_low"),
                "contract_gap_to_r2": c.get("contract_gap_to_r2"),
                "contract_candles": c.get("contract_candles"),
                "contract_candles_source": c.get("contract_candles_source"),
            })

        decision_tree.append({
            "index_key": idx.get("index_key"),
            "ticker": idx.get("ticker"),
            "index_name": idx.get("index_name"),
            "status": idx.get("status"),
            "reason_code": idx.get("reason_code"),
            "reason": idx.get("reason"),

            # ---- per-contract basis (drives the header) --------------- #
            "decision_basis": idx.get("decision_basis", "per_contract"),
            "contracts_reached_r2": idx.get(
                "contracts_reached_r2",
                idx.get("contracts_activated", 0),
            ),
            "best_contract_gap_to_r2": idx.get("best_contract_gap_to_r2"),
            "best_contract": idx.get("best_contract"),

            # ---- index-level context (informational only) ------------ #
            "index_r2_value": idx.get("index_r2_value"),
            "index_window_high": idx.get("index_window_high"),
            "index_gap_to_r2": idx.get("index_gap_to_r2"),

            # ---- legacy aliases (still forwarded for compatibility) -- #
            "r2_value": idx.get("r2_value"),
            "window_high": idx.get("window_high"),
            "gap_to_r2": idx.get("gap_to_r2"),

            "window_start": idx.get("window_start"),
            "window_end": idx.get("window_end"),
            "contracts_total": idx.get("contracts_total"),
            "contracts_activated": idx.get("contracts_activated"),
            "contracts_skipped": idx.get("contracts_skipped"),
            "contracts": contracts_compact,
        })

    return {
        "current_pick": current_pick,
        "timeline": timeline,
        "stage_summaries": {
            "stage_1": {
                "indexes": len(stage1_indexes),
                "strikes": stage1_strikes,
                "contracts": stage1_contracts,
                "avg_buffer_points": stage1.get("avg_buffer_points"),
                "generated_at": stage1.get("generated_at"),
            },
            "stage_2": {
                "evaluated": stage2.get("evaluated", 0),
                "isolated": stage2.get("isolated", 0),
                "pairs_isolated": stage2.get("pairs_isolated", 0),
                "skipped": stage2.get("skipped", 0),
                "errors": stage2.get("errors", 0),
                "generated_at": stage2.get("generated_at"),
            },
            "stage_3": {
                "activated_count": stage3.get("activated_count", 0),
                "first_touch": (
                    first_activated.get("touched_at")
                    if first_activated else None
                ),
                "last_touch": (
                    last_activated.get("touched_at")
                    if last_activated else None
                ),
                "window_start": stage3.get("window_start"),
                "window_end": stage3.get("window_end"),
                "evaluation_mode": stage3.get("evaluation_mode", "per_contract"),
                "decisions_generated_at": stage3_decisions.get("generated_at"),
            },
            "stage_4": {
                "candidates": stage4.get("candidates", 0),
                "picks": stage4.get("picks", 0),
                "placed": stage4.get("placed", 0),
                "generated_at": stage4.get("generated_at"),
            },
        },
        "last_activated": last_activated,
        "latest_orders": orders[:5],
        "stage_3_decisions": {
            "evaluation_mode": stage3_decisions.get(
                "evaluation_mode", "per_contract"
            ),
            "window_rule": stage3_decisions.get("window_rule"),
            "generated_at": stage3_decisions.get("generated_at"),
            "day": stage3_decisions.get("day"),
            "mode": stage3_decisions.get("mode"),
            "catchup": stage3_decisions.get("catchup"),
            "post_market": stage3_decisions.get("post_market"),
            "window_start": stage3_decisions.get("window_start"),
            "window_end": stage3_decisions.get("window_end"),
            "opening_minute": stage3_decisions.get("opening_minute"),
            "activated_total": stage3_decisions.get("activated_total", 0),
            "contracts_total": stage3_decisions.get("contracts_total", 0),
            "contracts_skipped": stage3_decisions.get("contracts_skipped", 0),
            "indexes": decision_tree,
        },
    }

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request) -> HTMLResponse:
    """Render the strategy dashboard."""
    return templates.TemplateResponse(
        request=request,
        name="strategy_dashboard.html",
        context={
            "app_name": "Market Data Foundation",
            "page_title": "Strategy Dashboard",
        },
    )

@router.get("/ui/dashboard/strategy")
def dashboard_data() -> dict[str, Any]:
    """JSON payload consumed by the dashboard's auto-refresh loop."""
    payload = _collect_dashboard_payload()
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    return payload