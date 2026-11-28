"""Fetch Upstox funds/margin balances and emit schema-compatible JSON."""
from __future__ import annotations

import json
import os
import sys
from typing import Any

import upstox_client


FUND_FIELDS = (
    "available_margin",
    "used_margin",
    "payin_amount",
    "span_margin",
    "adhoc_margin",
    "exposure_margin",
    "notional_cash",
)


def _plain(value: Any) -> Any:
    """Convert SDK model objects into JSON-compatible values without secrets."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "to_dict"):
        return _plain(value.to_dict())
    if hasattr(value, "to_dict") is False and hasattr(value, "__dict__"):
        return {
            str(k): _plain(v)
            for k, v in vars(value).items()
            if not str(k).startswith("_")
        }
    return str(value)


def _get(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def main() -> int:
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token:
        print("ERROR: UPSTOX_ACCESS_TOKEN is not set in environment.", file=sys.stderr)
        return 1

    try:
        configuration = upstox_client.Configuration()
        configuration.access_token = token
        with upstox_client.ApiClient(configuration) as api_client:
            response = upstox_client.UserApi(api_client).get_user_fund_margin_v3()
        data = _get(response, "data")
        if data is None:
            raise RuntimeError("Upstox funds response did not contain data.")

        if isinstance(data, dict):
            segment_items = data.items()
        elif hasattr(data, "items"):
            segment_items = data.items()
        else:
            raise RuntimeError("Unexpected Upstox funds response structure.")

        segments = {}
        for segment, values in segment_items:
            normalized = {}
            for field in FUND_FIELDS:
                value = _get(values, field)
                normalized[field] = _plain(value)
            # Preserve any additional fields exposed by the SDK/API.
            raw_values = _plain(values)
            if isinstance(raw_values, dict):
                normalized.update({k: v for k, v in raw_values.items() if k not in normalized})
            segments[str(segment)] = normalized

        print(json.dumps({"status": "success", "segments": segments}, indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:
        # SDK exceptions may contain request details; avoid dumping headers/configuration.
        message = getattr(exc, "reason", None) or getattr(exc, "status", None) or str(exc)
        print(f"ERROR: Unable to fetch Upstox funds: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
