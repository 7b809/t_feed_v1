"""Shared sanitiser for Upstox order-side responses.

Any token, bearer header, or Authorization value that could leak
through an Upstox error body or a verbose SDK response is redacted by
``sanitise_payload`` before it leaves a service module or a route.

Used by:
    * upstox_services.margin_check
    * upstox_services.place_order
    * upstox_services.exit_all_positions
    * routes.margin_routes
    * routes.order_routes
"""

from __future__ import annotations

import re
from typing import Any

# Anything this long and unbroken is treated as a token-like value.
SECRET_MIN_LEN = 20

# Keys whose values are always redacted regardless of length.
SECRET_KEYS = {
    "access_token",
    "accesstoken",
    "token",
    "auth_token",
    "authorization",
    "bearer",
    "x-api-key",
    "api_key",
    "apikey",
}

_LONG_RUN_RE = re.compile(r"[A-Za-z0-9\-_\.]{%d,}" % SECRET_MIN_LEN)


def redact_long_runs(text: str) -> str:
    """Replace runs of SECRET_MIN_LEN+ token-like characters."""
    return _LONG_RUN_RE.sub("<redacted>", str(text))


def sanitise_payload(value: Any, _depth: int = 0) -> Any:
    """Recursively redact token-like data from a JSON-ish payload.

    * Dicts: any key in ``SECRET_KEYS`` has its value replaced with
      ``"<redacted>"`` regardless of type.
    * Strings: any run of ``SECRET_MIN_LEN`` or more token-like
      characters is replaced with ``"<redacted>"``.
    * Lists / tuples: recursed element-wise.
    * Depth is capped at 8 to avoid pathological nesting.
    """
    if _depth > 8:
        return "<truncated>"

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if key_str.lower() in SECRET_KEYS:
                out[key_str] = "<redacted>"
            else:
                out[key_str] = sanitise_payload(v, _depth + 1)
        return out

    if isinstance(value, (list, tuple)):
        return [sanitise_payload(v, _depth + 1) for v in value]

    if isinstance(value, str):
        return redact_long_runs(value)

    return value