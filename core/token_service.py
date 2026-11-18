"""Load and validate the Upstox access token from MongoDB.

Token validation uses the official Upstox SDK (``UserApi.get_profile``).
The profile response is deliberately discarded: this module never logs,
returns, or persists ``user_id``, ``user_name``, ``broker``, ``exchanges``,
or any other field from the profile response. The only observable outcome
of validation is "valid" or "invalid".

``fetch_access_token()`` remains a pure MongoDB read with no network I/O,
so order placement and instrument search continue to use it on the hot
path. ``verify_access_token()`` and ``get_token_status()`` are the opt-in
SDK-backed validation calls used by ``/health/token``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import upstox_client
from upstox_client.rest import ApiException

from core.config import (
    APP_TOKENS_COLLECTION,
    TOKEN_DOC_ID,
    UPSTOX_API_VERSION,
    UPSTOX_SANDBOX,
)
from core.logger import get_logger
from core.mongo_client import get_mongo_db

logger = get_logger(__file__)

# Warn only once per process about an expired token document. If the token
# is refreshed in MongoDB while the process is running, /health/token will
# correctly report valid=true; the earlier warning is historical.
_expiry_warned = False


class TokenError(RuntimeError):
    """Base class for token problems. Subclasses RuntimeError on purpose."""


class TokenMissingError(TokenError):
    """The token document, or its access_token field, is missing/empty."""


class TokenInvalidError(TokenError):
    """Upstox rejected the token (HTTP 401)."""

    def __init__(
        self,
        message: str,
        *,
        upstream_status: int = 401,
        upstream_error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.upstream_status = upstream_status
        self.upstream_error_code = upstream_error_code


class TokenBlockedError(TokenError):
    """Upstox/Cloudflare rejected the request signature (HTTP 403)."""


class TokenUnreachableError(TokenError):
    """Network/timeout or unexpected HTTP status from Upstox."""


def _load_document() -> dict:
    """Read the single token document. Raises TokenMissingError if absent."""
    collection = get_mongo_db()[APP_TOKENS_COLLECTION]
    document = collection.find_one({"_id": TOKEN_DOC_ID})
    if not document:
        raise TokenMissingError(
            f"MongoDB token document not found: "
            f"{APP_TOKENS_COLLECTION}/{TOKEN_DOC_ID}"
        )
    return document


def _warn_if_expired(document: dict) -> None:
    """Log a single WARNING per process if the token document is expired.

    The ``expires_at`` field is optional; absent or unparseable values are
    silently skipped. The value itself is metadata about the token lifetime,
    not profile data, so logging it is safe.
    """
    global _expiry_warned
    if _expiry_warned:
        return

    raw = document.get("expires_at")
    if raw is None:
        return

    try:
        if isinstance(raw, datetime):
            parsed = raw
        else:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        logger.debug("Ignoring unparseable token expires_at=%r", raw)
        return

    if parsed <= datetime.now(timezone.utc):
        logger.warning(
            "Upstox access token in %s/%s appears expired (expires_at=%s). "
            "Refresh it in MongoDB; order placement and instrument search "
            "will otherwise fail with HTTP 401.",
            APP_TOKENS_COLLECTION,
            TOKEN_DOC_ID,
            parsed.isoformat(),
        )
        _expiry_warned = True


def fetch_access_token() -> str:
    """Read app_tokens._id=upstox_access_token and return access_token.

    Pure MongoDB read; no network I/O. Raises TokenMissingError when the
    document or its access_token field is unusable.
    """
    document = _load_document()
    token = document.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise TokenMissingError(
            "MongoDB token document has no usable access_token"
        )
    token = token.strip()

    _warn_if_expired(document)

    logger.debug(
        "Upstox access token loaded from MongoDB (updated_at=%s)",
        document.get("updated_at"),
    )
    return token


def _extract_error_code(exc: ApiException) -> str | None:
    """Best-effort extraction of the Upstox ``errorCode`` from an ApiException.

    Returns None when the body is not JSON or does not contain an errorCode.
    Never raises. Never returns any profile-related field — only the error
    envelope: ``{"errors":[{"errorCode":"UDAPI100050","message":"..."}]}``.
    """
    body = getattr(exc, "body", None)
    if not body:
        return None
    try:
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8", errors="replace")
        payload = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        first = errors[0]
        code = first.get("errorCode") or first.get("error_code")
        if isinstance(code, str):
            return code
    return None


def verify_access_token(token: str | None = None) -> None:
    """Validate the token by calling ``UserApi.get_profile`` via the SDK.

    Returns ``None`` on success. Raises a ``TokenError`` subclass on failure.

    The profile response is **discarded immediately**: this function does
    not assign the result, log it, return it, or persist it. The only
    observable outcomes are "success" (no exception) and "failure" (typed
    exception carrying only the HTTP status and Upstox error code — never
    the response body).
    """
    if token is None:
        token = fetch_access_token()

    configuration = upstox_client.Configuration(sandbox=UPSTOX_SANDBOX)
    configuration.access_token = token
    client = upstox_client.ApiClient(configuration)
    user_api = upstox_client.UserApi(client)

    try:
        # Response intentionally not assigned: the profile object should not
        # outlive this call. We only care that the call succeeded.
        user_api.get_profile(UPSTOX_API_VERSION)
    except ApiException as exc:
        status = getattr(exc, "status", None)
        # Never log ``exc`` itself: ApiException.__str__ embeds response
        # headers (set-cookie, cf-ray) and the raw body, both of which may
        # contain PII or session cookies. Log only the status code.
        logger.warning("Upstox token verification failed: HTTP %s", status)

        if status == 401:
            raise TokenInvalidError(
                "Upstox rejected the access token",
                upstream_status=401,
                upstream_error_code=_extract_error_code(exc),
            ) from None
        if status == 403:
            # 403 from Upstox usually means either an entitlement problem
            # or a Cloudflare access block. Both are operator-actionable
            # and neither is retryable here.
            raise TokenBlockedError(
                "Upstox or Cloudflare blocked the token verification request"
            ) from None
        raise TokenUnreachableError(
            f"Upstox user-profile endpoint returned HTTP {status}"
        ) from None
    except Exception as exc:
        # Do not include the message (could echo malformed profile data).
        logger.warning(
            "Upstox token verification raised %s",
            type(exc).__name__,
        )
        raise TokenUnreachableError(
            "Unexpected error while validating the Upstox access token"
        ) from None
    # Success: profile data already garbage-collected.


def get_token_status() -> dict[str, Any]:
    """Never-raising wrapper for /health style endpoints.

    Returns a minimal dict that deliberately excludes any profile data:

    * ``present`` — the MongoDB token document was readable.
    * ``valid``   — Upstox accepted the token on ``UserApi.get_profile``.
    * ``error``   — short category string (``missing``, ``unavailable``,
      ``invalid``, ``blocked``, ``unreachable``) when not valid.
    * ``upstream_status`` / ``upstream_error_code`` — for TokenInvalidError.

    No user identifiers, broker names, exchanges, or account details are
    included under any circumstances.
    """
    status: dict[str, Any] = {"present": False, "valid": False}

    try:
        token = fetch_access_token()
    except TokenMissingError:
        status["error"] = "missing"
        return status
    except TokenError:
        status["error"] = "unavailable"
        return status

    status["present"] = True

    try:
        verify_access_token(token)
        status["valid"] = True
    except TokenInvalidError as exc:
        status["error"] = "invalid"
        status["upstream_status"] = exc.upstream_status
        if exc.upstream_error_code:
            status["upstream_error_code"] = exc.upstream_error_code
    except TokenBlockedError:
        status["error"] = "blocked"
    except TokenUnreachableError:
        status["error"] = "unreachable"

    return status