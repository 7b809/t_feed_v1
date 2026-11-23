"""Direct REST integrations for Upstox order-book and exit-position APIs."""
from __future__ import annotations

from typing import Any

import requests

from core.config import UPSTOX_HTTP_TIMEOUT_SECONDS, UPSTOX_SANDBOX
from core.logger import get_logger
from core.token_service import fetch_access_token

logger = get_logger(__file__)

ORDER_BOOK_URL = "https://api.upstox.com/v2/order/retrieve-all"
EXIT_POSITIONS_URL = "https://api.upstox.com/v2/order/positions/exit"


class UpstoxOrderAPIError(RuntimeError):
    """A direct Upstox order API request failed or returned an unusable response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def _response_payload(response: requests.Response, operation: str) -> Any:
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:1000]}

    if not response.ok:
        upstream_status = response.status_code
        status_code = upstream_status if 400 <= upstream_status < 500 else 502
        message = f"Upstox {operation} request failed with HTTP {upstream_status}."
        if isinstance(payload, dict):
            errors = payload.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                message = str(errors[0].get("message") or message)
            elif payload.get("message"):
                message = str(payload["message"])
        raise UpstoxOrderAPIError(
            message,
            status_code=status_code,
            payload={"http_status": upstream_status, "upstox_response": payload},
        )

    if isinstance(payload, dict) and str(payload.get("status") or "").lower() == "error":
        message = str(payload.get("message") or "Upstox rejected the request.")
        errors = payload.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            message = str(errors[0].get("message") or message)
        raise UpstoxOrderAPIError(
            message,
            status_code=502,
            payload={"http_status": response.status_code, "upstox_response": payload},
        )
    return payload


def _request(method: str, url: str, *, params: dict[str, str] | None = None, operation: str) -> tuple[requests.Response, Any]:
    if UPSTOX_SANDBOX:
        raise UpstoxOrderAPIError(
            f"Upstox {operation} REST endpoint is configured for the live account. "
            "Set UPSTOX_SANDBOX=false and use a live Upstox token.",
            status_code=409,
        )

    try:
        token = fetch_access_token()
        request_method = getattr(requests, method.lower())
        response = request_method(
            url,
            headers=_headers(token),
            params=params or None,
            timeout=UPSTOX_HTTP_TIMEOUT_SECONDS,
        )
    except UpstoxOrderAPIError:
        raise
    except requests.RequestException:
        logger.exception("Upstox %s transport outcome is unknown", operation)
        raise UpstoxOrderAPIError(
            f"Could not confirm the Upstox {operation} request outcome. "
            "Refresh broker data before retrying.",
            status_code=502,
        ) from None
    except Exception as exc:
        logger.exception("Upstox %s request setup failed: %s", operation, type(exc).__name__)
        raise UpstoxOrderAPIError(
            f"Could not prepare the Upstox {operation} request.",
            status_code=503,
        ) from None

    return response, _response_payload(response, operation)


def get_order_book() -> dict[str, Any]:
    """Fetch the official current-trading-day Upstox order book."""
    response, payload = _request(
        "GET",
        ORDER_BOOK_URL,
        operation="order-book",
    )

    if isinstance(payload, list):
        orders = payload
        broker_status = "success"
        metadata = None
    elif isinstance(payload, dict):
        broker_status = str(payload.get("status") or "success").lower()
        orders = payload.get("data")
        metadata = payload.get("metadata")
        if broker_status != "success" or not isinstance(orders, list):
            raise UpstoxOrderAPIError(
                "Upstox returned an invalid order-book response.",
                status_code=502,
                payload={"http_status": response.status_code, "upstox_response": payload},
            )
    else:
        raise UpstoxOrderAPIError(
            "Upstox returned an invalid order-book response.",
            status_code=502,
        )

    if any(not isinstance(order, dict) for order in orders):
        raise UpstoxOrderAPIError(
            "Upstox order book contains an invalid order row.",
            status_code=502,
        )

    return {
        "status": "success",
        "source": "upstox",
        "scope": "current_trading_day",
        "http_status": response.status_code,
        "count": len(orders),
        "data": orders,
        "metadata": metadata,
    }


def submit_exit_positions(*, tag: str | None = None, segment: str | None = None) -> dict[str, Any]:
    """Submit Upstox's native Exit All Positions REST request.

    Upstox accepts optional tag/segment query parameters and no request body.
    HTTP 207 partial-success responses are preserved for caller/audit handling.
    """
    params: dict[str, str] = {}
    if tag:
        params["tag"] = tag
    if segment:
        params["segment"] = segment

    response, payload = _request(
        "POST",
        EXIT_POSITIONS_URL,
        params=params,
        operation="exit-all-positions",
    )
    if not isinstance(payload, dict):
        raise UpstoxOrderAPIError(
            "Upstox returned an invalid Exit All Positions response.",
            status_code=502,
        )

    broker_status = str(payload.get("status") or "success").lower()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    order_ids = [str(value) for value in (data.get("order_ids") or []) if value]
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {
        "total": len(order_ids) + len(errors),
        "success": len(order_ids),
        "error": len(errors),
    }

    partial = response.status_code == 207 or broker_status == "partial_success" or bool(errors)
    if broker_status not in {"success", "partial_success"} and not partial:
        raise UpstoxOrderAPIError(
            "Upstox did not confirm the Exit All Positions request.",
            status_code=502,
            payload={"http_status": response.status_code, "upstox_response": payload},
        )

    return {
        "http_status": response.status_code,
        "broker_status": "partial_success" if partial else "success",
        "order_ids": order_ids,
        "errors": errors,
        "summary": summary,
        "broker_response": payload,
    }
