"""Shared safe primitives for Upstox order cleanup skills.

No credentials are read from files here; the existing shared client factory is used.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import upstox_client
from upstox_helpers import order_api, order_api_v3, portfolio_api


def plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    method = getattr(value, "to_dict", None)
    if callable(method):
        return plain(method())
    if hasattr(value, "__dict__"):
        return {k: plain(v) for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def data_records(response: Any) -> list[dict]:
    payload = plain(getattr(response, "data", response))
    if payload is None:
        return []
    if isinstance(payload, dict):
        # Some SDK responses wrap arrays in a named field.
        for key in ("orders", "positions", "trades", "data"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            return [payload]
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def get_snapshot(include_gtt: bool = True) -> dict:
    normal = data_records(order_api().get_order_book(api_version="2.0"))
    positions = data_records(portfolio_api().get_positions(api_version="2.0"))
    gtt = data_records(order_api_v3().get_gtt_order_details()) if include_gtt else []
    return {"normal_orders": normal, "positions": positions, "gtt_orders": gtt}


def trades_for_order(order_id: str) -> list[dict]:
    return data_records(order_api().get_trades_by_order(order_id=order_id, api_version="2.0"))


def signed_trade_quantity(trades: list[dict]) -> int:
    total = 0
    for trade in trades:
        try:
            qty = int(trade.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        side = str(trade.get("transaction_type") or "").upper()
        if side == "BUY":
            total += qty
        elif side == "SELL":
            total -= qty
    return total


def position_index(positions: list[dict]) -> dict[tuple[str, str], dict]:
    result = {}
    for position in positions:
        key = (
            str(position.get("instrument_token") or ""),
            str(position.get("product") or "").upper(),
        )
        if key[0]:
            result[key] = position
    return result


def position_quantity(position: dict | None) -> int:
    if not position:
        return 0
    try:
        return int(position.get("quantity") or 0)
    except (TypeError, ValueError):
        return 0


def active_gtt_keys(gtt_orders: list[dict]) -> set[tuple[str, str]]:
    """Keys for GTTs whose entry has fired or whose lifecycle is otherwise active."""
    active = set()
    for item in gtt_orders:
        rules = item.get("rules") or []
        entry = next((r for r in rules if str(r.get("strategy", "")).upper() == "ENTRY"), {})
        state = str(entry.get("status") or "").upper()
        if entry.get("order_id") or state in {"TRIGGERED", "OPEN", "COMPLETED"}:
            active.add((
                str(item.get("instrument_token") or ""),
                str(item.get("product") or "").upper(),
            ))
    return active


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return plain(value)


def fingerprint(snapshot: dict, trades: dict, plan: dict) -> str:
    # Tuple keys are used internally for (instrument, product) trade groups;
    # convert them to stable strings before canonical JSON serialization.
    payload = _json_safe({"snapshot": snapshot, "trades": trades, "plan": plan})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def market_exit(instrument: str, product: str, quantity: int, net_quantity: int, tag: str):
    if quantity <= 0 or net_quantity == 0:
        raise ValueError("A market exit requires a positive quantity and non-zero position.")
    body = upstox_client.PlaceOrderV3Request(
        quantity=quantity,
        product=product,
        validity="DAY",
        price=0.0,
        tag=tag[:40],
        instrument_token=instrument,
        order_type="MARKET",
        transaction_type="SELL" if net_quantity > 0 else "BUY",
        disclosed_quantity=0,
        trigger_price=0.0,
        is_amo=False,
    )
    return order_api_v3().place_order(body)


def verify_order(order_id: str) -> dict:
    response = order_api().get_order_details(api_version="2.0", order_id=order_id)
    rows = data_records(response)
    if not rows:
        return {"verified": False, "reason": "No order details returned after submission."}
    latest = rows[-1]
    return {
        "verified": str(latest.get("status") or "").lower() in {"open", "complete", "trigger pending"},
        "status": latest.get("status"),
        "filled_quantity": latest.get("filled_quantity"),
        "average_price": latest.get("average_price"),
        "order_id": latest.get("order_id") or order_id,
    }


def cancel_normal_order(order_id: str):
    return order_api_v3().cancel_order(order_id)


def cancel_gtt_order(gtt_order_id: str):
    body = upstox_client.GttCancelOrderRequest(gtt_order_id=gtt_order_id)
    return order_api_v3().cancel_gtt_order(body=body)
