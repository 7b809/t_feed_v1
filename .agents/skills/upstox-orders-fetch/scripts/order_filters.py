"""Normalization and filtering helpers for Upstox normal and GTT order reads."""
from __future__ import annotations

from typing import Any


def to_plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
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
    return str(value)


def response_data(response: Any) -> Any:
    """Unwrap generated SDK response objects and common nested data envelopes."""
    payload = to_plain(getattr(response, "data", response))
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def as_records(payload: Any) -> list[dict]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        # Some APIs wrap collections in a named list.
        for key in ("orders", "gtt_orders", "data"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        return [payload]
    raise ValueError(f"Unexpected order response payload: {type(payload).__name__}")


def _text(record: dict, *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if value is not None:
            return str(value).strip().lower()
    return ""


def filter_records(
    records: list[dict],
    *,
    order_id: str | None = None,
    status: str | None = None,
    symbol: str | None = None,
    tag: str | None = None,
    segment: str | None = None,
) -> list[dict]:
    """Apply case-insensitive exact ID/status/tag/segment and partial symbol filters."""
    result = records
    if order_id:
        result = [
            item for item in result
            if order_id.lower() in _text(item, "order_id", "gtt_order_id")
        ]
    if status:
        result = [item for item in result if _text(item, "status") == status.lower()]
    if symbol:
        result = [
            item for item in result
            if symbol.lower() in _text(item, "trading_symbol", "instrument_token", "instrument_key")
        ]
    if tag:
        result = [item for item in result if _text(item, "tag") == tag.lower()]
    if segment:
        result = [
            item for item in result
            if _text(item, "exchange", "segment", "instrument_token", "instrument_key")
            .startswith(segment.lower())
        ]
    return result


def normalize_record(record: dict, kind: str) -> dict:
    """Add a stable order_kind field without discarding original Upstox fields."""
    result = dict(record)
    result["order_kind"] = kind
    return result
