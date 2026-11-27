"""Shared helpers for the external order-mutation API routes.

Centralises two things so every sub-router behaves identically:

1. ``raise_from_service_result`` – translates the structured ``FAILED`` payload
   produced by the new sandbox services (``place_order_service``,
   ``place_gtt_order``, ``place_dual_gtt_service``, ``gtt_modify_service``)
   into the correct ``HTTPException`` status and detail shape.

2. ``handle_api_exception`` – legacy ``ApiException`` translator used by the
   routes that still call the older ``services.*`` layer (which raises).

Callers importing from this module do not need to know which of the two
exception contracts the underlying service uses – the route code just calls
the appropriate helper based on how that service reports errors.
"""

from typing import Any, Dict

from fastapi import HTTPException
from upstox_client.rest import ApiException

from core.logger import get_logger

logger = get_logger(__file__)


# ---------------------------------------------------------------------------
# Step → HTTP status mapping
# ---------------------------------------------------------------------------
# The new services tag every failure with a ``step`` field. That step tells
# us whether the failure is the caller's fault (4xx), ours (5xx), or the
# broker's (5xx/4xx depending on the broker response).
STEP_TO_HTTP_STATUS: Dict[str, int] = {
    # Caller / input problems → 400
    "VALIDATION":          400,
    "PRICE_RESOLUTION":    400,
    "OFFSET_RESOLUTION":   400,
    "LOT_SIZE_RESOLUTION": 400,
    # Internal bugs → 500
    "REQUEST_BUILD":       500,
    "PERSISTENCE":         500,
    # Upstream failures → 502 (broker unreachable / bad response)
    "API_CLIENT_INIT":     502,
    "LTP_FETCH":           502,
    "OPTION_CHAIN":         502,
    "DAILY_PROFIT_GUARD":   403,
    "FETCH_STATE":         502,
    "ORDER_PLACEMENT":     502,
    "MODIFY_PLACEMENT":    502,
    # Fallback
    "UNKNOWN":             500,
}


def raise_from_service_result(
    result: Dict[str, Any],
    action: str = "order",
) -> None:
    """Raise an ``HTTPException`` if ``result['status'] == 'FAILED'``.

    Otherwise it is a no-op, so callers can write::

        result = place_order(...)
        raise_from_service_result(result, action="order")
        return result

    ``ORDER_PLACEMENT`` / ``MODIFY_PLACEMENT`` failures honour the broker's
    own HTTP status when it is a 4xx/5xx (e.g. Upstox returned 400 for a
    bad trigger price – we surface it as 400, not blanket 502).
    """
    if not isinstance(result, dict) or result.get("status") != "FAILED":
        return

    step = result.get("step", "UNKNOWN")
    err = result.get("error") or {}

    # Prefer the broker's HTTP status when placement failed and it was a
    # proper 4xx/5xx code – otherwise fall back to the step mapping.
    broker_http = err.get("http_status")
    if (
        step in ("ORDER_PLACEMENT", "MODIFY_PLACEMENT", "DAILY_PROFIT_GUARD")
        and isinstance(broker_http, int)
        and 400 <= broker_http <= 599
    ):
        http_status = broker_http
    else:
        http_status = STEP_TO_HTTP_STATUS.get(step, 500)

    detail: Dict[str, Any] = {
        "error":               err.get("message") or f"{action} failed",
        "code":                err.get("error_code"),
        "step":                step,
        "property_path":       err.get("property_path"),
        "invalid_value":       err.get("invalid_value"),
        "broker_http_status":  broker_http,
    }
    if result.get("context"):
        detail["context"] = result["context"]

    # Preserve the action label for logging / caller introspection
    detail["action"] = action

    raise HTTPException(status_code=http_status, detail=detail)


def handle_api_exception(exc: ApiException) -> None:
    """Legacy translator for the older ``services.*`` layer that still raises.

    Decodes the exception's HTTP status/reason/body into an ``HTTPException``
    that mirrors what the ``raise_from_service_result`` helper would produce.
    """
    status_code = getattr(exc, "status", None) or 502
    detail: Dict[str, Any] = {"error": getattr(exc, "reason", str(exc))}
    if getattr(exc, "body", None):
        detail["broker_error"] = exc.body
    raise HTTPException(status_code=status_code, detail=detail) from None