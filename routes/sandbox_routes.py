# routes/sandbox_routes.py
"""FastAPI router for sandbox execution manual testing.

Exposes thin, query-parameter-driven endpoints so you can exercise the
sandbox execution service from Swagger UI (``/docs``) without waiting
for dummy_order_service ticks or the EOD scheduler.

Design notes
------------
* Endpoints are *thin wrappers* — all business logic stays in
  ``services/sandbox_execution_service.py``, ``upstox_services/place_order.py``,
  and ``upstox_services/exit_all_positions.py``.
* Every endpoint returns a plain dict. No HTTPException is raised for
  expected business failures (disabled / no_token / paused / etc.) —
  the caller gets HTTP 200 with ``{"status": "...", ...}`` just like
  the underlying functions. Only programmer errors bubble as 500.
* ``dry_run`` query param defaults to ``False`` for the two write
  endpoints (``/entry`` and ``/exit``) so hitting *Execute* in Swagger
  actually exercises the broker path. Set ``dry_run=true`` to shape-check.
* Query parameters — no request bodies — so Swagger shows a simple form.
* Fully compatible with the existing Telegram instrumentation in
  ``place_order`` / ``exit_all_positions`` / ``sandbox_execution_service``:
  those notifications fire automatically on real broker calls.
* Security: no endpoint returns a token, a bearer, or a provider body
  that has not already been sanitised by the underlying services.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query

from core.logger import get_logger
from services.sandbox_execution_service import (
    SANDBOX_EOD_HOUR,
    SANDBOX_EOD_MINUTE,
    SANDBOX_EXECUTION_ENABLED,
    SANDBOX_EXECUTION_MODE,
    SANDBOX_LOTS,
    sandbox_execution_service,
)
from upstox_services.exit_all_positions import exit_all_positions
from upstox_services.place_order import place_order

logger = get_logger(__file__)

router = APIRouter(prefix="/sandbox", tags=["sandbox"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _resp(status: str, **kw: Any) -> dict[str, Any]:
    """Uniform response envelope. Keeps Swagger schema stable."""
    base = {"status": status, "called_at": _now_iso()}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# GET /sandbox/status
# ---------------------------------------------------------------------------

@router.get(
    "/status",
    summary="Sandbox status snapshot",
    description=(
        "Returns the current sandbox execution state: mode, paused flag, "
        "exit_stuck flag, open position id, today's entry/exit counts, "
        "entry/exit latency stats, EOD schedule, and the ledger path. "
        "Read-only — never calls the broker."
    ),
)
def sandbox_status() -> dict[str, Any]:
    try:
        snap = sandbox_execution_service.status()
        snap["env"] = {
            "execution_enabled": SANDBOX_EXECUTION_ENABLED,
            "execution_mode": SANDBOX_EXECUTION_MODE,
            "lots": SANDBOX_LOTS,
            "eod_schedule": f"{SANDBOX_EOD_HOUR:02d}:{SANDBOX_EOD_MINUTE:02d}",
        }
        return _resp("ok", snapshot=snap)
    except Exception as exc:
        logger.exception("[sandbox-routes] status failed")
        return _resp("error", error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# POST /sandbox/entry
# ---------------------------------------------------------------------------

@router.post(
    "/entry",
    summary="Place a sandbox entry order",
    description=(
        "Places a single-lot (SANDBOX_LOTS) MARKET BUY on the given "
        "instrument_key, product='D', transaction_type='BUY'. Uses the "
        "exact same `upstox_services.place_order.place_order(...)` code "
        "path the sandbox execution service uses in live mode.\n\n"
        "- `dry_run=true`  → returns a preview without calling the broker.\n"
        "- `dry_run=false` → real broker order; Telegram 'ENTRY started' "
        "and 'ENTRY filled/failed' notifications fire automatically via "
        "`place_order` + the sandbox service.\n\n"
        "Requires `order_runtime_service` to be enabled. Returns "
        "`{\"status\": \"disabled\"}` otherwise."
    ),
)
def sandbox_entry(
    instrument_key: str = Query(
        ...,
        description="Instrument key, e.g. `NSE_FO|43919`",
        examples=["NSE_FO|43919"],
    ),
    lots: int = Query(
        default=None,
        ge=1,
        le=50,
        description="Number of lots. Defaults to SANDBOX_LOTS from env.",
    ),
    product: str = Query(
        default="D",
        description="Product code: D (intraday), I (delivery), MTF",
        pattern=r"^(D|I|MTF)$",
    ),
    order_type: str = Query(
        default="MARKET",
        description="Order type: MARKET, LIMIT, SL, SL-M",
        pattern=r"^(MARKET|LIMIT|SL|SL-M)$",
    ),
    price: float = Query(
        default=0.0,
        ge=0.0,
        description="Limit price (required for LIMIT orders)",
    ),
    trigger_price: float = Query(
        default=0.0,
        ge=0.0,
        description="Trigger price (required for SL / SL-M orders)",
    ),
    tag: str = Query(
        default="sandbox-test",
        max_length=20,
        description="Order tag (<=20 chars). Empty string omits the tag.",
    ),
    dry_run: bool = Query(
        default=False,
        description=(
            "true  → preview only, do NOT call the broker.\n"
            "false → real broker order (Telegram notifications fire)."
        ),
    ),
) -> dict[str, Any]:
    key = str(instrument_key or "").strip()
    if not key:
        return _resp("invalid_input", error="instrument_key is required")

    use_lots = int(lots) if lots is not None else int(SANDBOX_LOTS)

    if dry_run:
        return _resp(
            "dry_run",
            preview={
                "instrument_key": key,
                "lots": use_lots,
                "transaction_type": "BUY",
                "product": product,
                "order_type": order_type,
                "price": float(price or 0.0),
                "trigger_price": float(trigger_price or 0.0),
                "tag": tag or None,
            },
            note="No broker call made (dry_run=true).",
        )

    try:
        result = place_order(
            key,
            lots=use_lots,
            transaction_type="BUY",
            product=product,
            order_type=order_type,
            price=float(price or 0.0),
            trigger_price=float(trigger_price or 0.0),
            validity="DAY",
            tag=tag or "",
        )
    except Exception as exc:
        logger.exception("[sandbox-routes] entry raised")
        return _resp("error", error=f"{type(exc).__name__}: {exc}", instrument_key=key)

    return _resp("ok", result=result, instrument_key=key, lots=use_lots)


# ---------------------------------------------------------------------------
# POST /sandbox/exit
# ---------------------------------------------------------------------------

@router.post(
    "/exit",
    summary="Exit all open positions",
    description=(
        "Calls `upstox_services.exit_all_positions.exit_all_positions(...)` "
        "— the same code path the sandbox execution service uses on "
        "STOP_HIT / resistance exits / EOD.\n\n"
        "- `dry_run=true`  → returns runtime/token readiness without "
        "calling the broker.\n"
        "- `dry_run=false` → real broker exit of ALL open positions; "
        "Telegram 'EXIT started' and 'EXITED / EXIT failed' notifications "
        "fire automatically via `exit_all_positions`.\n\n"
        "Requires `order_runtime_service` to be enabled."
    ),
)
def sandbox_exit(
    dry_run: bool = Query(
        default=False,
        description=(
            "true  → readiness check only, do NOT call the broker.\n"
            "false → real broker exit (Telegram notifications fire)."
        ),
    ),
) -> dict[str, Any]:
    try:
        result = exit_all_positions(dry_run=bool(dry_run))
    except Exception as exc:
        logger.exception("[sandbox-routes] exit raised")
        return _resp("error", error=f"{type(exc).__name__}: {exc}")

    return _resp("ok", dry_run=bool(dry_run), result=result)


# ---------------------------------------------------------------------------
# POST /sandbox/simulate-transition
# ---------------------------------------------------------------------------

_ALLOWED_TRANSITIONS = {
    "CREATED",
    "STOP_HIT",
    "STAGE_2_RESISTANCE_EXIT",
    "NEXT_15_3_RESISTANCE_EXIT",
    "EOD_CLOSE",
    "STAGE_2_MILESTONE",
}


@router.post(
    "/simulate-transition",
    summary="Inject a synthetic dummy transition",
    description=(
        "Feeds a synthetic transition into "
        "`sandbox_execution_service.on_dummy_transition(...)`. Useful for "
        "end-to-end testing without waiting for `dummy_order_service` "
        "ticks.\n\n"
        "The gate chain is still applied: transitions with "
        "`reason != 'scheduled'` are skipped. Pass `reason=scheduled` to "
        "let the sandbox service actually run (which may place / exit a "
        "real order depending on `SANDBOX_EXECUTION_MODE`).\n\n"
        "`CREATED` needs `instrument_key` + `entry` price.\n"
        "`STOP_HIT` / resistance exits need `order_id` + optional "
        "`close_price`.\n"
        "`EOD_CLOSE` needs `order_id` only."
    ),
)
def sandbox_simulate_transition(
    transition_type: str = Query(
        ...,
        description=f"One of: {sorted(_ALLOWED_TRANSITIONS)}",
    ),
    reason: str = Query(
        default="scheduled",
        description=(
            "Transition reason. `scheduled` → sandbox service executes the "
            "gate chain. Anything else (e.g. `startup`, `manual`) → skipped."
        ),
    ),
    order_id: Optional[str] = Query(
        default=None,
        description=(
            "Dummy order_id, e.g. `NSE_FO|43919|2026-11-04T09:25:00+05:30`. "
            "Required for exit / EOD transitions; optional for CREATED."
        ),
    ),
    instrument_key: Optional[str] = Query(
        default=None,
        description="Instrument key. Required for CREATED.",
    ),
    bias: str = Query(default="", description="CE / PE (informational only)"),
    entry: Optional[float] = Query(
        default=None,
        description="Dummy entry price. Required for CREATED.",
    ),
    close_price: Optional[float] = Query(
        default=None,
        description="Dummy exit price (STOP_HIT / resistance exits).",
    ),
    stop: Optional[float] = Query(
        default=None,
        description="Dummy stop price (STOP_HIT fallback if close_price is absent).",
    ),
) -> dict[str, Any]:
    ttype = str(transition_type or "").strip().upper()
    if ttype not in _ALLOWED_TRANSITIONS:
        return _resp(
            "invalid_input",
            error=f"transition_type must be one of {sorted(_ALLOWED_TRANSITIONS)}",
        )

    if ttype == "CREATED" and not (instrument_key and entry):
        return _resp(
            "invalid_input",
            error="CREATED requires instrument_key and entry",
        )
    if ttype != "CREATED" and not order_id:
        return _resp(
            "invalid_input",
            error=f"{ttype} requires order_id",
        )

    record: dict[str, Any] = {
        "order_id": order_id,
        "instrument_key": instrument_key,
        "bias": bias,
        "entry": entry,
        "close_price": close_price,
        "stop": stop,
    }
    # Strip None so downstream `_record_time` / `_dummy_exit_price` fall
    # back to their default chain.
    record = {k: v for k, v in record.items() if v is not None}

    try:
        sandbox_execution_service.on_dummy_transition(
            transition_type=ttype,
            order_record=record,
            reason=str(reason or "scheduled"),
        )
    except Exception as exc:
        # on_dummy_transition itself catches everything; this is just
        # belt-and-suspenders in case a future refactor regresses that.
        logger.exception("[sandbox-routes] simulate-transition raised")
        return _resp("error", error=f"{type(exc).__name__}: {exc}")

    return _resp(
        "dispatched",
        transition_type=ttype,
        reason=reason,
        order_record=record,
        note="See /sandbox/status and data/sandbox_orders.json for effect.",
    )


# ---------------------------------------------------------------------------
# POST /sandbox/force-eod-exit
# ---------------------------------------------------------------------------

@router.post(
    "/force-eod-exit",
    summary="Force EOD exit of all open sandbox positions",
    description=(
        "Calls `sandbox_execution_service.force_eod_exit()`. This bypasses "
        "the normal-flow gate chain (safety EOD flatten). In live mode "
        "this triggers real `exit_all_positions` calls with full Telegram "
        "instrumentation. Returns `{status: noop}` if no open positions."
    ),
)
def sandbox_force_eod_exit() -> dict[str, Any]:
    try:
        result = sandbox_execution_service.force_eod_exit()
    except Exception as exc:
        logger.exception("[sandbox-routes] force-eod-exit raised")
        return _resp("error", error=f"{type(exc).__name__}: {exc}")
    return _resp("ok", result=result)


# ---------------------------------------------------------------------------
# POST /sandbox/reconcile
# ---------------------------------------------------------------------------

@router.post(
    "/reconcile",
    summary="Run startup reconcile now",
    description=(
        "Calls `sandbox_execution_service.reconcile_startup()`. Marks "
        "PENDING_ENTRY as ENTRY_FAILED(restart_race), retries EXIT_PENDING "
        "exits, and force-exits any today's still-ENTERED positions. "
        "Idempotent — safe to run manually. Telegram reconcile-summary "
        "notification fires."
    ),
)
def sandbox_reconcile() -> dict[str, Any]:
    try:
        summary = sandbox_execution_service.reconcile_startup()
    except Exception as exc:
        logger.exception("[sandbox-routes] reconcile raised")
        return _resp("error", error=f"{type(exc).__name__}: {exc}")
    return _resp("ok", summary=summary)


# ---------------------------------------------------------------------------
# POST /sandbox/resume
# ---------------------------------------------------------------------------

@router.post(
    "/resume",
    summary="Resume sandbox after a pause",
    description=(
        "Clears `_paused` state. Use after fixing the root cause of a "
        "`token_expired` / `runtime_disabled` pause. Fires a 'RESUMED' "
        "Telegram notification."
    ),
)
def sandbox_resume(
    source: str = Query(
        default="api",
        description="Free-form label recorded in the resume log + Telegram.",
        max_length=40,
    ),
) -> dict[str, Any]:
    try:
        status = sandbox_execution_service.resume(source=str(source or "api"))
    except Exception as exc:
        logger.exception("[sandbox-routes] resume raised")
        return _resp("error", error=f"{type(exc).__name__}: {exc}")
    return _resp("ok", status=status)