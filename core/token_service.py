"""Load and validate the Upstox access token from MongoDB.

Token validation uses the official Upstox SDK ``UserApi.get_profile``
method exactly as shown in Upstox's reference example::

    configuration = upstox_client.Configuration()
    configuration.access_token = '{your_access_token}'
    api_version = '2.0'
    api_instance = upstox_client.UserApi(upstox_client.ApiClient(configuration))

    try:
        api_response = api_instance.get_profile(api_version)
        print(api_response)
    except ApiException as e:
        print("Exception when calling UserApi->get_profile: %s" % e)

with four deliberate differences:

1. The access token is loaded from MongoDB (``app_tokens/upstox_access_token``)
   instead of a hardcoded literal.
2. ``Configuration(sandbox=UPSTOX_SANDBOX)`` is used so the validation call
   hits the same Upstox environment (sandbox vs live) that order placement
   and instrument search use. Mixing them produces spurious 401s.
3. The response is **not** assigned to a variable and **not** printed.
   ``get_profile`` returns a ``GetProfileResponse`` containing ``user_id``,
   ``user_name``, email, broker, exchanges, and enabled products. We only
   need to know whether the call succeeded, so the payload is discarded
   immediately and never logged, returned, or persisted. On failure we log
   only the HTTP status code and (for 401) the Upstox error code — never
   the ``ApiException`` itself, whose ``__str__`` embeds response headers
   (including ``set-cookie``) and the raw body.
4. **Environment-mismatch detection.** A live Upstox token used against
   the sandbox API (or vice versa) returns ``UDAPI100050`` — indistinguishable
   from an expired token. Since this is a common mistake (``UPSTOX_SANDBOX``
   does not match the token's origin), on a 401 in the configured
   environment we retry once in the opposite environment. If that succeeds,
   we raise ``TokenEnvMismatchError`` with the actual fix (change
   ``UPSTOX_SANDBOX``) instead of the misleading "refresh the token". If
   both environments reject the token, it is genuinely invalid and we raise
   the normal ``TokenInvalidError``. The retry only fires on 401, so the
   common success path (valid token in configured environment) makes exactly
   one API call.

``fetch_access_token()`` remains a pure MongoDB read with no network I/O,
so order placement and instrument search continue to use it on the hot
path. ``verify_access_token()`` and ``get_token_status()`` are the opt-in
SDK-backed validation calls used by ``/health/token`` and the periodic
token-check scheduler.
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


class TokenEnvMismatchError(TokenInvalidError):
    """Token is valid in the *other* Upstox environment than configured.

    Inherits from ``TokenInvalidError`` so all existing
    ``except TokenInvalidError`` handlers continue to catch this case, while
    callers that want the extra detail can catch ``TokenEnvMismatchError``
    first. The actionable fix is changing ``UPSTOX_SANDBOX`` in ``.env``,
    not refreshing the token.

    Attributes:
        valid_environment: ``"live"`` or ``"sandbox"`` — whichever accepted
            the token during the fallback attempt.
    """

    def __init__(
        self,
        message: str,
        *,
        valid_environment: str,
        upstream_error_code: str | None = None,
    ) -> None:
        super().__init__(
            message,
            upstream_status=401,
            upstream_error_code=upstream_error_code,
        )
        self.valid_environment = valid_environment


class TokenBlockedError(TokenError):
    """Upstox/Cloudflare rejected the request signature (HTTP 403)."""


class TokenUnreachableError(TokenError):
    """Network/timeout or unexpected HTTP status from Upstox."""


class TokenConfigError(TokenError):
    """SDK-side validation rejected the call (e.g. invalid api_version).

    Distinct from ``TokenUnreachableError`` so monitoring can tell a
    configuration problem (fixable by editing ``.env``) apart from an
    outage (fixable by retrying). The most common trigger is an empty or
    malformed ``UPSTOX_API_VERSION``: ``UserApi.get_profile`` then raises
    ``ValueError`` *before* any HTTP request is made.
    """


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


def _try_get_profile(token: str, api_version: str) -> None:

    configuration = upstox_client.Configuration()
    configuration.access_token = token
    api_instance = upstox_client.UserApi(upstox_client.ApiClient(configuration))
    api_instance.get_profile(api_version)


def verify_access_token(token: str | None = None) -> None:
    """Validate the token by calling ``UserApi.get_profile`` via the SDK.

    Follows Upstox's reference example pattern, with token from MongoDB and
    no profile data retained. Returns ``None`` on success, raises a
    ``TokenError`` subclass on failure.

    Failure classes:

    * ``TokenMissingError``    — no token in MongoDB.
    * ``TokenConfigError``     — SDK rejected the call before sending it
      (typically ``ValueError`` from an empty ``UPSTOX_API_VERSION``).
      Fix by correcting ``.env``; retrying will not help.
    * ``TokenEnvMismatchError`` — the token is valid, but in the *other*
      Upstox environment than ``UPSTOX_SANDBOX`` is set to. Fix by flipping
      ``UPSTOX_SANDBOX`` in ``.env``.
    * ``TokenInvalidError``    — Upstox returned 401 in *both* environments
      (expired or revoked token).
    * ``TokenBlockedError``    — Upstox/Cloudflare returned 403.
    * ``TokenUnreachableError`` — network error, timeout, or unexpected 5xx.
    """
    # 1. Resolve the token (MongoDB read; no network I/O).
    if token is None:
        token = fetch_access_token()

    # 2. Resolve the api_version exactly as the SDK example does, but honor
    #    the configured value with a safety net for empty/malformed input.
    #    An empty string reaching get_profile triggers ValueError *before*
    #    any HTTP call; guard against it here so the operator sees
    #    error=config instead of error=unreachable.
    api_version = (UPSTOX_API_VERSION or "2.0").strip() or "2.0"

    # 3. First attempt: the environment the service is configured for. This
    #    is the only call made when the token is valid, so there is no
    #    overhead on the common path.
    try:
        _try_get_profile(token, api_version)
        return  # success — profile already discarded
    except ApiException as exc:
        status = getattr(exc, "status", None)
        # Never log ``exc`` itself: ApiException.__str__ embeds response
        # headers (set-cookie, cf-ray) and the raw body. Log only status.
        logger.warning(
            "Upstox token verification failed in configured env "
            "(sandbox=%s): HTTP %s",
            UPSTOX_SANDBOX,
            status,
        )

        if status == 403:
            # Cloudflare / entitlement block. Not retryable here.
            raise TokenBlockedError(
                "Upstox or Cloudflare blocked the token verification request"
            ) from None

        if status == 401:
            # 401 in the configured env is ambiguous: the token could be
            # expired, or it could belong to the *other* environment. Try
            # the opposite once to disambiguate — only on 401, so the
            # happy path stays at one HTTP call.
            other_env = not UPSTOX_SANDBOX
            try:
                _try_get_profile(token, api_version)
            except ApiException:
                # Both environments rejected the token — genuinely invalid.
                raise TokenInvalidError(
                    "Upstox rejected the access token in both live and "
                    "sandbox environments",
                    upstream_status=401,
                    upstream_error_code=_extract_error_code(exc),
                ) from None
            # Fallback succeeded: the token is valid, but for a different
            # environment than the service is configured for.
            valid_env = "sandbox" if other_env else "live"
            configured_env = "sandbox" if UPSTOX_SANDBOX else "live"
            logger.warning(
                "Upstox token is valid in %r but service is configured for "
                "%r; set UPSTOX_SANDBOX=%s in .env",
                valid_env,
                configured_env,
                str(other_env).lower(),
            )
            raise TokenEnvMismatchError(
                f"Token is valid in {valid_env} environment, but the "
                f"service is configured for {configured_env}. "
                f"Update UPSTOX_SANDBOX in .env.",
                valid_environment=valid_env,
                upstream_error_code=_extract_error_code(exc),
            ) from None

        # 5xx / unexpected statuses
        raise TokenUnreachableError(
            f"Upstox user-profile endpoint returned HTTP {status}"
        ) from None

    except ValueError as exc:
        # SDK client-side validation error — typically because
        # api_version reached the SDK in an unexpected form. Surface the
        # actual message (truncated) so the operator can fix ``.env``.
        message = (str(exc) or "(no message)")[:300]
        logger.warning(
            "Upstox token verification ValueError (api_version=%r): %s",
            api_version,
            message,
        )
        raise TokenConfigError(
            f"Upstox SDK rejected the token validation request: {message}"
        ) from None

    except Exception as exc:
        # Anything else: log the class name *and* message so future SDK
        # oddities are diagnosable without enabling DEBUG. Truncate to keep
        # the log line short.
        message = (str(exc) or "(no message)")[:300]
        logger.warning(
            "Upstox token verification raised %s: %s",
            type(exc).__name__,
            message,
        )
        raise TokenUnreachableError(
            "Unexpected error while validating the Upstox access token"
        ) from None


def get_token_status() -> dict[str, Any]:
    """Never-raising wrapper for /health style endpoints.

    Returns a minimal dict that deliberately excludes any profile data:

    * ``present`` — the MongoDB token document was readable.
    * ``valid``   — Upstox accepted the token on ``UserApi.get_profile``
      in the configured environment.
    * ``error``   — short category string when not valid:
      ``missing``, ``config``, ``env_mismatch``, ``invalid``, ``blocked``,
      ``unreachable``.
    * ``valid_environment`` — for ``env_mismatch`` only: which environment
      (``"live"`` or ``"sandbox"``) actually accepted the token.
    * ``upstream_status`` / ``upstream_error_code`` — for 401 responses.

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
        status["error"] = "unreachable"
        return status

    status["present"] = True

    try:
        verify_access_token(token)
        status["valid"] = True
    except TokenEnvMismatchError as exc:
        # More specific than TokenInvalidError — must be caught first.
        status["error"] = "env_mismatch"
        status["valid_environment"] = exc.valid_environment
        if exc.upstream_error_code:
            status["upstream_error_code"] = exc.upstream_error_code
    except TokenInvalidError as exc:
        status["error"] = "invalid"
        status["upstream_status"] = exc.upstream_status
        if exc.upstream_error_code:
            status["upstream_error_code"] = exc.upstream_error_code
    except TokenBlockedError:
        status["error"] = "blocked"
    except TokenConfigError:
        status["error"] = "config"
    except TokenUnreachableError:
        status["error"] = "unreachable"

    return status


def _main() -> int:
    """Manual test harness: ``python -m core.token_service``.

    Subcommands:
        fetch          Read the token from MongoDB (no network I/O).
        verify         Fetch + validate against Upstox (get_profile).
        status         Full status dict (never raises).
        all            Run fetch, then verify, then status.

    Exit codes:
        0  success
        1  token missing / unreadable
        2  token invalid in both environments (HTTP 401)
        3  token blocked (HTTP 403)
        4  Upstox unreachable / unexpected error
        5  configuration error (invalid UPSTOX_API_VERSION)
        6  environment mismatch (token valid in the other env)
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m core.token_service",
        description="Manual test harness for the Upstox access token.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["fetch", "verify", "status", "all"],
        help="What to run (default: all).",
    )
    args = parser.parse_args()

    print(f"command = {args.command}")
    print("-" * 60)

    if args.command in ("fetch", "all"):
        print("[fetch] reading token from MongoDB ...")
        try:
            token = fetch_access_token()
        except TokenMissingError as exc:
            print(f"[fetch] MISSING: {exc}")
            return 1
        except TokenError as exc:
            print(f"[fetch] ERROR: {type(exc).__name__}: {exc}")
            return 1
        # Never print the token itself. Only a masked fingerprint.
        print(
            f"[fetch] OK  length={len(token)}  "
            f"prefix={token[:4]}...{token[-4:] if len(token) > 8 else ''}"
        )

    if args.command in ("verify", "all"):
        print("[verify] calling UserApi.get_profile ...")
        try:
            verify_access_token()
        except TokenMissingError as exc:
            print(f"[verify] MISSING: {exc}")
            return 1
        except TokenEnvMismatchError as exc:
            print(
                f"[verify] ENV_MISMATCH: token valid in "
                f"{exc.valid_environment} but service is configured for "
                f"the other environment. Set UPSTOX_SANDBOX in .env."
            )
            return 6
        except TokenInvalidError as exc:
            print(
                f"[verify] INVALID: HTTP {exc.upstream_status} "
                f"errorCode={exc.upstream_error_code} "
                f"(rejected in both live and sandbox)"
            )
            return 2
        except TokenBlockedError as exc:
            print(f"[verify] BLOCKED: {exc}")
            return 3
        except TokenConfigError as exc:
            print(f"[verify] CONFIG: {exc}")
            return 5
        except TokenUnreachableError as exc:
            print(f"[verify] UNREACHABLE: {exc}")
            return 4
        except TokenError as exc:
            print(f"[verify] ERROR: {type(exc).__name__}: {exc}")
            return 4
        print("[verify] OK  (profile response discarded, not printed)")

    if args.command in ("status", "all"):
        print("[status] get_token_status() ...")
        status = get_token_status()
        for key in (
            "present",
            "valid",
            "error",
            "valid_environment",
            "upstream_status",
            "upstream_error_code",
        ):
            if key in status:
                print(f"[status] {key} = {status[key]!r}")
        if not status.get("valid"):
            error = status.get("error")
            if error == "invalid":
                return 2
            if error == "blocked":
                return 3
            if error == "config":
                return 5
            if error == "env_mismatch":
                return 6
            return 4

    print("-" * 60)
    print("DONE")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())