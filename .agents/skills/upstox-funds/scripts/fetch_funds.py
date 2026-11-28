"""Fetch Upstox funds with normalized nested data and structured errors."""
from __future__ import annotations

import json
import os
import sys
from typing import Any

import upstox_client


def _plain(value: Any) -> Any:
    """Convert SDK response/model objects recursively to JSON-compatible data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _plain(to_dict())
        except Exception:
            pass

    if hasattr(value, "__dict__"):
        return {
            str(key): _plain(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return str(value)


def _get(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _emit(payload: dict[str, Any]) -> int:
    # All expected API/response errors are JSON results, so stdout remains
    # machine-readable and the CLI can validate the same contract every time.
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


def _error(message: str, error_type: str, raw_response: Any = None) -> int:
    return _emit({
        "status": "error",
        "message": message,
        "error": {"type": error_type, "message": message},
        "funds": None,
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

        # Keep the complete funds object exactly as returned, including all
        # nested cash, pledge, margin-used and unavailable-to-trade details.
        funds = _plain(data)
        if not isinstance(funds, dict) or not funds:
            return _error(
                "Upstox returned funds data in an unrecognized structure.",
                "UnexpectedResponseStructure",
                response,
            )

        # This endpoint's current v3 response is organized around these two
        # top-level groups. Preserve extra top-level groups if Upstox adds any.
        if not any(key in funds for key in ("available_to_trade", "unavailable_to_trade")):
            return _error(
                "Upstox funds data did not contain available_to_trade or "
                "unavailable_to_trade. The raw response is included for inspection.",
                "UnexpectedFundsPayload",
                response,
            )

        return _emit({
            "status": "success",
            "message": "Funds data fetched successfully.",
            "error": None,
            "funds": funds,
            "raw_response": _plain(response),
        })
    except Exception as exc:
        # Avoid dumping SDK request objects, headers or credentials.
        message = (
            getattr(exc, "reason", None)
            or getattr(exc, "status", None)
            or str(exc)
        )
        return _error(
            f"Unable to fetch Upstox funds: {message}",
            type(exc).__name__,
            response,
        )


if __name__ == "__main__":
    sys.exit(main())
