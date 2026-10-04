"""
Isolation layer for EMA crossovers.

Picks one option contract per enabled index per day from the
opening-range touch pool, enriches that contract's EMA crosses into a
full alert payload, and — depending on the order-mode flag — either
places a market order on the isolated instrument itself or on the
budget-filtered shortlist from the payload.

The base `ema_app` continues broadcasting every cross over its own
WebSocket topic. This package is additive: it consumes those crosses,
applies the isolation rules, and produces the richer alerts plus orders.

Persistence (MongoDB)
---------------------
Two collections, both keyed `_id = YYYY-MM-DD` (one document per day):

    ema_isolated_alerts   ->  { "alerts": { "HH_MM_SS": {...} }, ... }
    ema_isolated_orders   ->  { "orders": { "HH_MM_SS": {...} }, ... }

Every order sub-document carries `alert_key` — the sub-document key of
the alert that triggered it in the same-day alert document. This lets
you join orders back to their source alert without scanning.

Both write paths are fail-open: a Mongo failure never blocks the alert
flow or order placement.
"""

from ema_app.isolation.alert_storage import (
    IsolatedAlertStorage,
    isolated_alert_storage,
)
from ema_app.isolation.config import isolation_config
from ema_app.isolation.order_service import (
    place_orders_for_isolated_ema_alert,
    place_orders_for_payload,
)
from ema_app.isolation.order_storage import (
    IsolatedOrderStorage,
    isolated_order_storage,
)

__all__ = [
    # Order placement
    "place_orders_for_isolated_ema_alert",
    "place_orders_for_payload",
    # Config
    "isolation_config",
    # Persistence — alerts
    "IsolatedAlertStorage",
    "isolated_alert_storage",
    # Persistence — orders
    "IsolatedOrderStorage",
    "isolated_order_storage",
]