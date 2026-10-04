"""
Order placement service for isolated EMA alerts.

Public API
----------
    place_orders_for_payload(payload)               -> dict
    place_orders_for_isolated_ema_alert(payload)    -> dict   (alias)

Decides which instruments to order based on the config flag
`ISOLATED_INSTRUMENT_ORDERS_ONLY`:

    true  -> order the isolated instrument itself on its EMA cross
             (bullish => BUY; bearish => SELL only if
              `place_sell_on_bearish` is true, otherwise skip)
    false -> order the budget-filtered instruments from the payload
             (BUY up to `max_orders_per_alert` of them)

The service is thread-safe: a per-key in-memory dedupe window
prevents duplicate orders within `dedupe_window_sec` for the same
(instrument_key, direction) pair.

Returned result shape
---------------------
    {
        "mode": "isolated_instrument_only" | "budget_range" | "skipped",
        "success": bool,
        "orders": [ <per-order result dicts from place_order.py> ],
        "errors": [ str, ... ],
        "message": str,
        "skipped_reason": str | None,
        "placed_at": iso timestamp,
    }
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from ema_app.isolation.config import isolation_config
from ema_app.isolation.place_order import (
    place_budget_instrument,
    place_selected_instrument,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Internal state — dedupe window
# ---------------------------------------------------------------------------
_dedupe_lock = threading.Lock()
_recent_orders: Dict[str, float] = {}   # key -> monotonic ts of last order


def _dedupe_key(instrument_key: str, direction: str) -> str:
    return f"{instrument_key}::{direction}"


def _is_duplicate(instrument_key: str, direction: str) -> bool:
    now = time.monotonic()
    window = max(0, isolation_config.dedupe_window_sec)
    if window <= 0:
        return False

    key = _dedupe_key(instrument_key, direction)
    with _dedupe_lock:
        # Prune stale entries while we're here
        stale_cutoff = now - window * 4
        for k in [k for k, ts in _recent_orders.items() if ts < stale_cutoff]:
            _recent_orders.pop(k, None)

        last = _recent_orders.get(key)
        if last is not None and (now - last) < window:
            return True
        _recent_orders[key] = now
        return False


# ---------------------------------------------------------------------------
# Payload extractors
# ---------------------------------------------------------------------------
def _extract_instrument(payload: Dict[str, Any]) -> Dict[str, Any]:
    instrument = payload.get("instrument") or {}
    return instrument if isinstance(instrument, dict) else {}


def _extract_direction(payload: Dict[str, Any]) -> str:
    duplicate_control = payload.get("duplicate_control") or {}
    if isinstance(duplicate_control, dict):
        direction = str(duplicate_control.get("direction") or "").strip().lower()
        if direction in ("bullish", "bearish"):
            return direction

    ema = payload.get("ema") or {}
    if isinstance(ema, dict):
        direction = str(ema.get("direction") or "").strip().lower()
        if direction in ("bullish", "bearish"):
            return direction

        cross_type = str(ema.get("cross_type") or "").strip().lower()
        if "bullish" in cross_type:
            return "bullish"
        if "bearish" in cross_type:
            return "bearish"

    return "unknown"


def _extract_budget_instruments(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    order_suggestion = payload.get("order_suggestion") or {}
    if not isinstance(order_suggestion, dict):
        return []

    budget = order_suggestion.get("budget_filter") or {}
    if not isinstance(budget, dict):
        return []

    instruments = budget.get("instruments") or []
    if not isinstance(instruments, list):
        return []

    return [i for i in instruments if isinstance(i, dict)]


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------
def _empty_result(
    mode: str,
    message: str,
    skipped_reason: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "mode": mode,
        "success": False,
        "orders": [],
        "errors": [],
        "message": message,
        "skipped_reason": skipped_reason,
        "placed_at": datetime.now().isoformat(),
    }


def _aggregate(orders: List[Dict[str, Any]]) -> Dict[str, Any]:
    errors = [o.get("error") for o in orders if not o.get("success") and o.get("error")]
    success = bool(orders) and all(o.get("success") for o in orders)
    return {
        "success": success,
        "orders": orders,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Mode: isolated instrument only
# ---------------------------------------------------------------------------
def _place_isolated_only(payload: Dict[str, Any]) -> Dict[str, Any]:
    instrument = _extract_instrument(payload)
    direction = _extract_direction(payload)

    instrument_key = str(instrument.get("instrument_key") or "").strip()
    if not instrument_key:
        return _empty_result(
            mode="isolated_instrument_only",
            message="No instrument_key on payload.instrument.",
            skipped_reason="missing_instrument_key",
        )

    if direction == "unknown":
        return _empty_result(
            mode="isolated_instrument_only",
            message="Unknown cross direction.",
            skipped_reason="unknown_direction",
        )

    # --- Bullish cross -> BUY the isolated instrument ---------------------
    if direction == "bullish":
        if _is_duplicate(instrument_key, "BUY"):
            return _empty_result(
                mode="isolated_instrument_only",
                message=f"Duplicate BUY for {instrument_key} within dedupe window.",
                skipped_reason="duplicate_within_window",
            )

        order = place_selected_instrument(
            selected_instrument=instrument,
            transaction_type="BUY",
            product=isolation_config.product,
            validity=isolation_config.validity,
            tag=isolation_config.tag,
            default_quantity=isolation_config.default_quantity,
        )
        result = _empty_result(
            mode="isolated_instrument_only",
            message="BUY placed for isolated instrument."
            if order.get("success") else "BUY failed for isolated instrument.",
        )
        result.update(_aggregate([order]))
        return result

    # --- Bearish cross -> SELL only if enabled ---------------------------
    if direction == "bearish":
        if not isolation_config.place_sell_on_bearish:
            return _empty_result(
                mode="isolated_instrument_only",
                message="Bearish cross ignored (place_sell_on_bearish=false).",
                skipped_reason="sell_on_bearish_disabled",
            )

        if _is_duplicate(instrument_key, "SELL"):
            return _empty_result(
                mode="isolated_instrument_only",
                message=f"Duplicate SELL for {instrument_key} within dedupe window.",
                skipped_reason="duplicate_within_window",
            )

        order = place_selected_instrument(
            selected_instrument=instrument,
            transaction_type="SELL",
            product=isolation_config.product,
            validity=isolation_config.validity,
            tag=isolation_config.tag,
            default_quantity=isolation_config.default_quantity,
        )
        result = _empty_result(
            mode="isolated_instrument_only",
            message="SELL placed for isolated instrument."
            if order.get("success") else "SELL failed for isolated instrument.",
        )
        result.update(_aggregate([order]))
        return result

    return _empty_result(
        mode="isolated_instrument_only",
        message="Unhandled direction.",
        skipped_reason="unhandled_direction",
    )


# ---------------------------------------------------------------------------
# Mode: budget range
# ---------------------------------------------------------------------------
def _place_budget_range(payload: Dict[str, Any]) -> Dict[str, Any]:
    instruments = _extract_budget_instruments(payload)
    if not instruments:
        return _empty_result(
            mode="budget_range",
            message="No budget instruments on payload.",
            skipped_reason="no_budget_instruments",
        )

    direction = _extract_direction(payload)
    if direction == "unknown":
        return _empty_result(
            mode="budget_range",
            message="Unknown cross direction.",
            skipped_reason="unknown_direction",
        )

    # Budget mode places BUY orders on the suggested-side instruments.
    max_orders = max(1, isolation_config.max_orders_per_alert)
    to_order = instruments[:max_orders]

    orders: List[Dict[str, Any]] = []
    for inst in to_order:
        key = str(inst.get("instrument_key") or "").strip()
        if not key:
            continue
        if _is_duplicate(key, "BUY"):
            logger.debug(
                "Skipping duplicate BUY for %s within dedupe window.", key
            )
            continue

        order = place_budget_instrument(
            budget_instrument=inst,
            transaction_type="BUY",
            product=isolation_config.product,
            validity=isolation_config.validity,
            tag=isolation_config.tag,
            default_quantity=isolation_config.default_quantity,
        )
        orders.append(order)

    if not orders:
        return _empty_result(
            mode="budget_range",
            message="No new budget instruments to order (dedupe or empty keys).",
            skipped_reason="no_new_instruments",
        )

    result = _empty_result(
        mode="budget_range",
        message=f"Placed {sum(1 for o in orders if o.get('success'))}/{len(orders)} budget orders.",
    )
    result.update(_aggregate(orders))
    return result


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------
def place_orders_for_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Decide and place orders for an isolated EMA alert payload.

    Returns a structured result dict (see module docstring).
    """
    if not isinstance(payload, dict):
        return _empty_result(
            mode="skipped",
            message="Payload must be a dict.",
            skipped_reason="invalid_payload",
        )

    if not isolation_config.enabled:
        return _empty_result(
            mode="skipped",
            message="Isolation layer disabled.",
            skipped_reason="isolation_disabled",
        )

    if not isolation_config.orders_enabled:
        return _empty_result(
            mode="skipped",
            message="Order placement disabled (EMA_ALERT_ORDERS_ENABLED=false).",
            skipped_reason="orders_disabled",
        )

    # Log the routing decision so it's traceable
    mode = (
        "isolated_instrument_only"
        if isolation_config.isolated_instrument_orders_only
        else "budget_range"
    )
    logger.info(
        "Routing isolated EMA alert | mode=%s | dry_run=%s | event_id=%s",
        mode,
        isolation_config.dry_run,
        payload.get("event_id"),
    )

    if isolation_config.dry_run:
        return _empty_result(
            mode=mode,
            message=(
                f"DRY RUN — would place orders in mode={mode}. "
                "Set EMA_ALERT_ORDER_DRY_RUN=false to actually place."
            ),
            skipped_reason="dry_run",
        )

    try:
        if isolation_config.isolated_instrument_orders_only:
            result = _place_isolated_only(payload)
        else:
            result = _place_budget_range(payload)
    except Exception as exc:
        logger.exception(
            "Order placement failed | mode=%s | event_id=%s",
            mode, payload.get("event_id"),
        )
        return _empty_result(
            mode=mode,
            message=f"Order placement raised: {type(exc).__name__}: {exc}",
            skipped_reason="exception",
        )

    logger.info(
        "Order placement result | mode=%s | success=%s | orders=%d | errors=%d | event_id=%s",
        result.get("mode"),
        result.get("success"),
        len(result.get("orders") or []),
        len(result.get("errors") or []),
        payload.get("event_id"),
    )
    return result


# Alias — the name the isolation service will call
place_orders_for_isolated_ema_alert = place_orders_for_payload


__all__ = [
    "place_orders_for_payload",
    "place_orders_for_isolated_ema_alert",
]