"""
Centralized Upstox API error code registry.

Maps raw Upstox error codes (UDAPI*) to structured metadata (HTTP status,
category, user-facing title, message, remediation hint) so every caller —
legacy routes, sandbox services, and external routers — renders a consistent
error payload AND logs compactly.

Logging policy
--------------
* Known codes  → single WARNING line with the prebuilt ``title``.
                ``raw`` is trimmed to "(status) reason" — no headers, no body.
* Unknown codes → single ERROR line with the trimmed-or-full ``raw`` payload
                (truncated to 2000 chars) so the issue can be diagnosed.

Use ``log_api_exception(exc_or_error, action=..., log=...)`` from any caller
that receives an ApiException or a pre-parsed error dict. Never interpolate
the full ``str(ApiException)`` into a log line — it dumps the entire
HTTPHeaderDict and body.

Reference: https://upstox.com/developer/api-documentation/error-codes
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class UpstoxErrorMeta:
    code: str
    http_status: int
    category: str                       # request | auth | network | market_hours |
                                        # rate_limit | server | broker
    title: str
    message: str
    remediation: Optional[str] = None


_REGISTRY: dict[str, UpstoxErrorMeta] = {
    "UDAPI10000": UpstoxErrorMeta(
        code="UDAPI10000",
        http_status=400,
        category="request",
        title="Unsupported API request",
        message="This request is not supported by the Upstox API.",
        remediation="Check the URL, HTTP method, and API version.",
    ),
    "UDAPI100016": UpstoxErrorMeta(
        code="UDAPI100016",
        http_status=401,
        category="auth",
        title="Invalid credentials",
        message="One of the credentials passed to the API is invalid.",
        remediation="Regenerate the Upstox access token and retry.",
    ),
    "UDAPI10005": UpstoxErrorMeta(
        code="UDAPI10005",
        http_status=429,
        category="rate_limit",
        title="Rate limit exceeded",
        message="Too many requests sent to Upstox.",
        remediation="Back off and retry after the rate-limit window resets.",
    ),
    "UDAPI100015": UpstoxErrorMeta(
        code="UDAPI100015",
        http_status=400,
        category="request",
        title="API version missing",
        message="API version is not present in the request headers.",
        remediation="Send the 'Api-Version' header (for example '2.0').",
    ),
    "UDAPI100050": UpstoxErrorMeta(
        code="UDAPI100050",
        http_status=401,
        category="auth",
        title="Invalid access token",
        message="The access token used to access the API is invalid.",
        remediation="Refresh the access token and retry.",
    ),
    "UDAPI100067": UpstoxErrorMeta(
        code="UDAPI100067",
        http_status=403,
        category="auth",
        title="API not permitted with extended token",
        message="This API is not allowed with the extended_token you are using.",
        remediation="Switch to a regular access token for this endpoint.",
    ),
    "UDAPI100036": UpstoxErrorMeta(
        code="UDAPI100036",
        http_status=400,
        category="request",
        title="Invalid input",
        message="An invalid input was passed to the API.",
        remediation="Review the request payload against the API contract.",
    ),
    "UDAPI100038": UpstoxErrorMeta(
        code="UDAPI100038",
        http_status=400,
        category="request",
        title="Invalid input",
        message="An invalid input was passed to the API.",
        remediation="Review the request payload against the API contract.",
    ),
    "UDAPI100073": UpstoxErrorMeta(
        code="UDAPI100073",
        http_status=403,
        category="auth",
        title="Client ID inactive",
        message="Your 'client_id' is inactive.",
        remediation="Contact Upstox support to reactivate the app.",
    ),
    # ---- Order-placement window (05:30–00:00 IST daily) ---------------
    # The Place / Modify / Cancel order APIs are gated by Upstox's daily
    # order-window. Requests outside that window return HTTP 423 Locked
    # with these codes. AMO orders bypass the gate — the remediation hint
    # points callers at is_amo=True.
    "UDAPI100074": UpstoxErrorMeta(
        code="UDAPI100074",
        http_status=423,
        category="market_hours",
        title="Outside order-placement hours",
        message=(
            "The Place order API is accessible only between 05:30 and 00:00 IST."
        ),
        remediation=(
            "Retry during the Upstox order-placement window (05:30–00:00 IST), "
            "or submit the request with is_amo=True to queue it as an "
            "after-market order."
        ),
    ),
    "UDAPI100075": UpstoxErrorMeta(
        code="UDAPI100075",
        http_status=423,
        category="market_hours",
        title="Outside order-modification hours",
        message=(
            "The Modify order API is accessible only between 05:30 and 00:00 IST."
        ),
        remediation=(
            "Retry during the Upstox order-modification window (05:30–00:00 IST)."
        ),
    ),
    "UDAPI100076": UpstoxErrorMeta(
        code="UDAPI100076",
        http_status=423,
        category="market_hours",
        title="Outside order-cancellation hours",
        message=(
            "The Cancel order API is accessible only between 05:30 and 00:00 IST."
        ),
        remediation=(
            "Retry during the Upstox order-cancellation window (05:30–00:00 IST)."
        ),
    ),
    "UDAPI100500": UpstoxErrorMeta(
        code="UDAPI100500",
        http_status=500,
        category="server",
        title="Upstox internal error",
        message="Something went wrong on Upstox's side.",
        remediation="Retry later; contact Upstox support if it persists.",
    ),
    "UDAPI1154": UpstoxErrorMeta(
        code="UDAPI1154",
        http_status=403,
        category="network",
        title="Static IP restriction",
        message=(
            "Access to this API is blocked due to static-IP restrictions. "
            "The request origin IP does not match the configured static IP."
        ),
        remediation=(
            "Add this server's public IP to the Upstox developer console, or "
            "force IPv4 egress (disable IPv6) so the request leaves through a "
            "whitelisted address."
        ),
    ),
    "UDAPI1175": UpstoxErrorMeta(
        code="UDAPI1175",
        http_status=400,
        category="request",
        title="Invalid expiry",
        message="The expiry supplied is not valid for this contract.",
        remediation="Use the YYYY-MM-DD expiry returned by /v2/option/contract.",
    ),
}


def lookup(code: Optional[str]) -> Optional[UpstoxErrorMeta]:
    if not code:
        return None
    return _REGISTRY.get(code)


def is_known(code: Optional[str]) -> bool:
    return lookup(code) is not None


def _compact_raw(http_status: Optional[int], reason: Optional[str]) -> str:
    """Short, human-readable description used for known error codes.

    Drops the entire HTTPHeaderDict and body dump that
    ``ApiException.__str__`` normally emits. The structured fields on the
    error dict (message, title, remediation) already carry the meaningful
    detail; the raw string only needs to point at the wire-level response.
    """
    parts: list[str] = []
    if http_status is not None:
        parts.append(f"({http_status})")
    if reason:
        parts.append(str(reason).strip())
    return " ".join(parts) if parts else ""


def _fallback_title(code: Optional[str], reason: Optional[str]) -> str:
    """Title for an unregistered error code.

    Upstox's HTTP ``reason`` phrase ("Locked", "Forbidden") is too generic to
    serve as a useful title — the same phrase covers multiple codes. When the
    code is known but unregistered, surface it verbatim so the developer can
    add it to the registry. Only fall back to ``reason`` when we have no code
    at all.
    """
    if code:
        return f"Upstox error {code}"
    return reason or "Upstox API error"


def parse_api_exception(exc: BaseException) -> dict[str, Any]:
    """
    Convert an Upstox SDK exception (typically ``ApiException``) into a
    structured error dict with a resolved error code, category, HTTP status,
    remediation hint, and a *trimmed* ``raw`` field for known codes.

    Handles both documented response shapes:

        {"status": "error", "errors": [{"errorCode": "...", "message": "..."}]}
        {"error_code": "...", "message": "..."}                 (flat form)

    ``raw`` behaviour:
        * Known code   → "(status) reason" (no header/body dump)
        * Unknown code → full ``str(exc)`` so the payload is still recoverable
    """
    http_status = getattr(exc, "status", None)
    reason = getattr(exc, "reason", None) or str(exc)
    raw_body = getattr(exc, "body", None)

    code: Optional[str] = None
    upstream_message: Optional[str] = None
    property_path: Any = None
    invalid_value: Any = None

    if raw_body:
        try:
            if isinstance(raw_body, (str, bytes, bytearray)):
                decoded = json.loads(raw_body)
            else:
                decoded = raw_body
            if isinstance(decoded, dict):
                errors = decoded.get("errors") or []
                if errors and isinstance(errors, list) and isinstance(errors[0], dict):
                    first = errors[0]
                    code = first.get("errorCode") or first.get("error_code")
                    upstream_message = first.get("message")
                    property_path = (
                        first.get("propertyPath") or first.get("property_path")
                    )
                    invalid_value = (
                        first.get("invalidValue") or first.get("invalid_value")
                    )
                elif decoded.get("error_code") or decoded.get("errorCode"):
                    code = decoded.get("error_code") or decoded.get("errorCode")
                    upstream_message = decoded.get("message")
                    property_path = (
                        decoded.get("propertyPath") or decoded.get("property_path")
                    )
                    invalid_value = (
                        decoded.get("invalidValue") or decoded.get("invalid_value")
                    )
        except Exception:
            pass

    meta = lookup(code)
    raw = _compact_raw(http_status, reason) if meta else str(exc)

    return {
        "error_code": code or "UNKNOWN",
        "category": meta.category if meta else "unknown",
        "http_status": http_status or (meta.http_status if meta else None),
        "title": meta.title if meta else _fallback_title(code, reason),
        "message": upstream_message or (meta.message if meta else reason),
        "remediation": meta.remediation if meta else None,
        "property_path": property_path,
        "invalid_value": invalid_value,
        "raw": raw,
    }


def log_api_exception(
    exc_or_error: Any,
    *,
    action: str,
    log: Optional[logging.Logger] = None,
) -> None:
    """
    Log an Upstox API failure compactly.

    Known codes (present in ``_REGISTRY``) get a single WARNING line with the
    prebuilt title. No raw body, no headers, no traceback.

    Unknown codes get a single ERROR line at ``raw[:2000]`` — bounded but
    still diagnostic. Callers should pass the *already-parsed* error dict if
    they have one (avoids a redundant parse).
    """
    if log is None:
        log = logging.getLogger("upstox")

    if isinstance(exc_or_error, dict):
        error = exc_or_error
    else:
        error = parse_api_exception(exc_or_error)

    code = error.get("error_code") or "UNKNOWN"
    meta = lookup(code)
    http_status = error.get("http_status") or (meta.http_status if meta else None)

    if meta is not None:
        log.warning(
            "%s failed | %s (%s) http=%s | %s",
            action,
            code,
            meta.category,
            http_status,
            meta.title,
        )
        return

    raw = (error.get("raw") or "")[:2000]
    log.error(
        "%s failed | %s http=%s | %s | raw=%s",
        action,
        code,
        http_status,
        error.get("message") or "Unknown error",
        raw,
    )