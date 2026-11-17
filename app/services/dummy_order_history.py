"""Pure helpers for reading and presenting daily dummy-order archives."""
from __future__ import annotations

from typing import Any


def _normalise_strategy(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    if "supertrend" in raw:
        return "SUPERTREND"
    if raw == "sr" or raw.startswith("sr_") or raw.endswith("_sr"):
        return "SR"
    return str(value or "UNKNOWN").strip().upper() or "UNKNOWN"


def _order_identity(order: dict[str, Any], isolation_id: str, strategy: str) -> str:
    explicit = order.get("order_id")
    if explicit not in (None, ""):
        return f"{isolation_id}:{explicit}"
    started = order.get("started_at_ist") or order.get("entry_time_ist") or order.get("placed_at") or ""
    return f"{isolation_id}:{strategy}:{started}"


def flatten_daily_orders(document: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Merge event history and latest per-isolation snapshots without duplicates."""
    if not isinstance(document, dict):
        return []

    date_key = str(document.get("_id") or document.get("trade_date_ist") or "")[:10]
    orders_data = document.get("orders_data") or {}
    if not isinstance(orders_data, dict):
        return []

    flattened: dict[str, tuple[int, str, dict[str, Any]]] = {}

    def add(order_value: Any, isolation_id: Any, indicator: Any,
            recorded_at: Any = None, *, live_snapshot: bool = False) -> None:
        if not isinstance(order_value, dict):
            return
        strategy = _normalise_strategy(
            order_value.get("indicator") or order_value.get("strategy") or indicator
        )
        if strategy not in {"SR", "SUPERTREND"}:
            return
        if str(order_value.get("status") or "").upper() == "DISABLED":
            return

        iso = str(order_value.get("isolation_id") or isolation_id or "")
        item = dict(order_value)
        item["isolation_id"] = iso or None
        item["strategy"] = strategy
        item.setdefault("trade_date_ist", date_key or None)
        identity = _order_identity(item, iso, strategy)
        item["history_key"] = identity
        marker = str(
            item.get("completed_at_ist")
            or item.get("updated_at")
            or item.get("high_time_ist")
            or item.get("low_time_ist")
            or recorded_at
            or item.get("entry_time_ist")
            or item.get("started_at_ist")
            or ""
        )
        priority = 1 if live_snapshot else 0
        previous = flattened.get(identity)
        if previous is None or (priority, marker) >= (previous[0], previous[1]):
            flattened[identity] = (priority, marker, item)

    for event in orders_data.get("orders_history") or []:
        if not isinstance(event, dict):
            continue
        order = event.get("order") if isinstance(event.get("order"), dict) else event
        add(
            order,
            event.get("isolation_id"),
            event.get("indicator"),
            event.get("recorded_at"),
        )

    by_isolation = orders_data.get("by_isolation") or {}
    if isinstance(by_isolation, dict):
        for entry in by_isolation.values():
            if not isinstance(entry, dict):
                continue
            isolation_id = entry.get("isolation_id")
            add(entry.get("sr_order"), isolation_id, "SR", live_snapshot=True)
            add(entry.get("supertrend_order"), isolation_id, "SUPERTREND", live_snapshot=True)

    # Older daily documents may predate by_isolation; retain their root snapshots.
    # When per-isolation data exists, the root fields are only a last-writer
    # compatibility mirror and must not override or duplicate other isolations.
    if not by_isolation:
        add(orders_data.get("sr_order"), None, "SR", live_snapshot=True)
        add(orders_data.get("supertrend_order"), None, "SUPERTREND", live_snapshot=True)

    result = [row[2] for row in flattened.values()]
    result.sort(
        key=lambda order: (
            str(order.get("entry_time_ist") or order.get("started_at_ist")
                or order.get("placed_at") or order.get("completed_at_ist") or ""),
            str(order.get("order_id") or ""),
        ),
        reverse=True,
    )
    return result


def summarize_orders(orders: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [
        order for order in orders
        if str(order.get("status") or "").upper().startswith("COMPLETED")
    ]
    pnl_values: list[float] = []
    for order in completed:
        try:
            value = order.get("pnl_net")
            if value is not None:
                pnl_values.append(float(value))
        except (TypeError, ValueError):
            continue

    return {
        "total_orders": len(orders),
        "open_orders": sum(order.get("status") == "DUMMY_PLACED" for order in orders),
        "completed_orders": len(completed),
        "target_hits": sum(order.get("exit_reason") == "TARGET" for order in completed),
        "stop_losses": sum(order.get("exit_reason") == "STOP_LOSS" for order in completed),
        "realized_pnl": round(sum(pnl_values), 2) if pnl_values else None,
    }


def filter_orders(
    orders: list[dict[str, Any]],
    *,
    strategy: str | None = None,
    status: str | None = None,
    search: str | None = None,
) -> list[dict[str, Any]]:
    strategy_value = _normalise_strategy(strategy) if strategy else None
    status_value = status.strip().upper() if status else None
    search_value = search.strip().casefold() if search else None

    result = []
    for order in orders:
        if strategy_value and order.get("strategy") != strategy_value:
            continue
        if status_value and str(order.get("status") or "").upper() != status_value:
            continue
        if search_value:
            haystack = " ".join(
                str(order.get(field) or "")
                for field in ("trading_symbol", "instrument_key", "target_instrument_key",
                              "isolation_id", "order_id", "strategy")
            ).casefold()
            if search_value not in haystack:
                continue
        result.append(order)
    return result
