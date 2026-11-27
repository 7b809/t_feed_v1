"""Shared helpers for external API routes.

Centralises the conversion of service-layer results and raw exceptions into
FastAPI ``HTTPException`` responses, using the Upstox error registry to
surface accurate HTTP statuses, categories, and remediation hints.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from core.logger import get_logger
from core.upstox_errors import parse_api_exception

logger = get_logger(__file__)

_DEFAULT_FAILURE_STATUS = 502  # Bad Gateway for opaque service-level failures


def _extract_http_status(result: dict[str, Any], default: int = _DEFAULT_FAILURE_STATUS) -> int:
    error = result.get("error")
    if not isinstance(error, dict):
        return default
    status = error.get("http_status")
    if isinstance(status, int) and 100 <= status < 600:
        return status
    return default


def _build_error_detail(result: dict[str, Any], action: str) -> dict[str, Any]:
    error = result.get("error") or {}
    if not isinstance(error, dict):
        error = {"message": str(error) if error else "Unknown error."}

    return {
        "error": error.get("message") or error.get("title") or "Request failed.",
        "code": error.get("error_code") or "UNKNOWN",
        "category": error.get("category"),
        "title": error.get("title"),
        "step": result.get("step"),
        "property_path": error.get("property_path"),
        "invalid_value": error.get("invalid_value"),
        "remediation": error.get("remediation"),
        "broker_http_status": error.get("http_status"),
        "context": result.get("context"),
        "action": action,
    }


def raise_from_service_result(result: Any, *, action: str) -> None:
    """
    Inspect a sandbox-service result and raise an ``HTTPException`` for FAILED.

    SUCCESS and PARTIAL_SUCCESS results pass through unchanged (the caller
    decides whether to wrap them in a 207). FAILED results raise with the
    broker's HTTP status when available, falling back to 502 for opaque
    service-level failures.
    """
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Invalid service response for {action}.",
                "code": "INVALID_SERVICE_RESPONSE",
                "action": action,
            },
        )

    status = str(result.get("status") or "").upper()
    if status in {"SUCCESS", "PARTIAL_SUCCESS"}:
        return

    http_status = _extract_http_status(result)
    detail = _build_error_detail(result, action)

    logger.warning(
        "Service %s returned %s | code=%s | step=%s | http=%s",
        action, status or "FAILED", detail.get("code"), detail.get("step"), http_status,
    )
    raise HTTPException(status_code=http_status, detail=detail)


def handle_api_exception(exc: Exception, *, action: str) -> None:
    """Convert an exception raised inside a legacy route into an HTTPException."""
    error = parse_api_exception(exc)
    http_status = error.get("http_status") or 500

    logger.exception("Legacy route %s raised %s", action, type(exc).__name__)
    raise HTTPException(
        status_code=http_status,
        detail={
            "error": error.get("message") or str(exc),
            "code": error.get("error_code") or "UNKNOWN",
            "category": error.get("category"),
            "title": error.get("title"),
            "remediation": error.get("remediation"),
            "action": action,
        },
    )