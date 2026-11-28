"""Fetch Upstox funds and always return a structured JSON result."""
from __future__ import annotations

import json
import os
import sys
from typing import Any

import upstox_client


FUND_FIELDS = (
    "available_margin", "used_margin", "payin_amount", "span_margin",
    "adhoc_margin", "exposure_margin", "notional_cash",
)


def _plain(value: Any) -> Any:
    """Convert SDK response/model objects recursively to JSON-compatible data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _plain(to_dict())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
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


def _emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


def _error(message: str, error_type: str, raw_response: Any = None) -> int:
    return _emit({
        "status": "error",
        "message": message,
        "error": {"type": error_type, "message": message},
        "segments": None,
        "raw_response": _plain(raw_response),
    })


def main() -> int:
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token:
        return _error(
            "UPSTOX_ACCESS_TOKEN is not set in the process environment.",
            "MissingAccessToken",
        )

    response = None
    try:
        configuration = upstox_client.Configuration()
        configuration.access_token = token
        api_client = upstox_client.ApiClient(configuration)
        response = upstox_client.UserApi(api_client).get_user_fund_margin_v3()

        data = _get(response, "data")
        if data is None:
            return _error(
                "Upstox response did not contain a data field.",
                "MissingResponseData",
                response,
            )

        # SDK versions may return generated model objects rather than dicts.
        # Convert the complete response data before trying to enumerate segments.
        normalized_data = _plain(data)
        if not isinstance(normalized_data, dict) or not normalized_data:
            return _error(
                "Upstox returned funds data in an unrecognized structure. "
                "The raw response is included for inspection.",
                "UnexpectedResponseStructure",
                response,
            )

        segments: dict[str, Any] = {}
        for segment, values in normalized_data.items():
            values_dict = values if isinstance(values, dict) else _plain(values)
            if not isinstance(values_dict, dict):
                values_dict = {"raw_value": values}
            # Keep known fund fields explicit and preserve all additional API fields.
            normalized = {field: values_dict.get(field) for field in FUND_FIELDS}
            normalized.update({k: v for k, v in values_dict.items() if k not in normalized})
            segments[str(segment)] = normalized

        if not segments:
            return _error(
                "Upstox returned an empty funds segment collection.",
                "EmptyFundsResponse",
                response,
            )

        return _emit({
            "status": "success",
            "message": "Funds data fetched successfully.",
            "error": None,
            "segments": segments,
            "raw_response": _plain(response),
        })
    except Exception as exc:
        # Do not print SDK request objects, headers, or credentials.
        message = getattr(exc, "reason", None) or getattr(exc, "status", None) or str(exc)
        return _error(
            f"Unable to fetch Upstox funds: {message}",
            type(exc).__name__,
            response,
        )


if __name__ == "__main__":
    sys.exit(main())
