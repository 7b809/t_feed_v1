"""Validation and normalization helpers for safe GTT modifications.

This module does not call the Upstox API and never handles credentials.
"""
from __future__ import annotations

import math
from typing import Any

RULE_FIELDS = ("strategy", "trigger_type", "trigger_price", "trailing_gap", "market_protection")
SUPPORTED_LEGS = {"ENTRY", "STOPLOSS", "TARGET"}


def to_plain(value: Any) -> Any:
    """Convert SDK models and nested values to JSON-serializable Python values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    if isinstance(value, tuple):
        return [to_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        return to_plain(converter())
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return {
            key: to_plain(item)
            for key, item in attributes.items()
            if not key.startswith("_")
        }
    raise TypeError(f"Unsupported Upstox response value: {type(value).__name__}")


def unwrap_order(response: Any, requested_id: str) -> dict:
    """Extract one GTT order from an SDK response, failing closed if ambiguous."""
    payload = to_plain(getattr(response, "data", response))
    if isinstance(payload, dict) and "data" in payload and not payload.get("gtt_order_id"):
        payload = payload["data"]
    if isinstance(payload, list):
        matches = [
            item for item in payload
            if isinstance(item, dict) and item.get("gtt_order_id") == requested_id
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Expected exactly one GTT order matching {requested_id}; found {len(matches)}."
            )
        payload = matches[0]
    if not isinstance(payload, dict):
        raise ValueError("Upstox returned an unexpected GTT order response.")
    if payload.get("gtt_order_id") and payload["gtt_order_id"] != requested_id:
        raise ValueError("Upstox returned a different GTT order ID than requested.")
    if not payload.get("rules") or not isinstance(payload["rules"], list):
        raise ValueError("The selected GTT order has no readable rules; refusing to modify it.")
    if not payload.get("type"):
        raise ValueError("The selected GTT order type is missing; refusing to modify it.")
    if payload.get("quantity") is None:
        raise ValueError("The selected GTT order quantity is missing; refusing to modify it.")
    return payload


def build_modified_rules(existing_rules: list, leg: str, new_price: float) -> tuple[list, float]:
    """Copy all existing rules and change only the selected leg's trigger price."""
    leg = leg.upper()
    if leg not in SUPPORTED_LEGS:
        raise ValueError(f"Unsupported GTT leg {leg!r}. Choose ENTRY, STOPLOSS, or TARGET.")
    try:
        price = float(new_price)
    except (TypeError, ValueError):
        raise ValueError("New trigger price must be a valid number.") from None
    if not math.isfinite(price) or price <= 0:
        raise ValueError("New trigger price must be a finite number greater than zero.")

    matches = [
        index for index, rule in enumerate(existing_rules)
        if str(rule.get("strategy", "")).upper() == leg
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one {leg} rule in this GTT order; found {len(matches)}. "
            "No changes were made."
        )

    updated = [dict(rule) for rule in existing_rules]
    index = matches[0]
    old_price = float(updated[index]["trigger_price"])
    updated[index]["trigger_price"] = price

    # Keep the API request limited to fields supported by the documented GttRule model.
    # Preserve optional trailing-stop and market-protection settings when present.
    request_rules = []
    for rule in updated:
        missing = {"strategy", "trigger_type", "trigger_price"} - set(rule)
        if missing:
            raise ValueError(
                f"Existing GTT rule is missing required fields: {', '.join(sorted(missing))}. "
                "No changes were made."
            )
        request_rules.append({key: rule[key] for key in RULE_FIELDS if key in rule})
    return request_rules, old_price


def validate_gtt_modification(order: dict, leg: str, new_price: float) -> dict:
    """Build a preview and a complete modify request without making an API call."""
    rules, old_price = build_modified_rules(order["rules"], leg, new_price)
    return {
        "gtt_order_id": order.get("gtt_order_id"),
        "type": order["type"],
        "quantity": order["quantity"],
        "leg": leg.upper(),
        "old_trigger_price": old_price,
        "new_trigger_price": float(new_price),
        "rules": rules,
        "modify_request": {
            "type": order["type"],
            "gtt_order_id": order.get("gtt_order_id"),
            "quantity": order["quantity"],
            "rules": rules,
        },
    }
