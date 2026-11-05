# core/auth.py
"""Shared-secret authentication for sensitive FastAPI routes.

Three schemes are supported:

  1. Bearer / X-API-Key header
       A single shared secret. Best for machine clients (curl, CI,
       internal tools). Send either:

           X-API-Key: <ADMIN_API_KEY>
           Authorization: Bearer <ADMIN_API_KEY>

  2. HTTP Basic
       A username/password pair that a browser can prompt for. Best for
       humans opening /docs, /logs, /admin in a browser.

  3. Signed session cookie
       An HMAC-signed cookie set by the /login page. Best for browsers
       that should not have to re-enter Basic credentials on every
       navigation. Contains ``admin:<expiry>`` plus an HMAC-SHA256
       signature derived from ``ADMIN_SESSION_SECRET``.

Which scheme to require per route is decided by the dependency you
attach:

    Depends(require_api_key)     -> header auth only
    Depends(require_basic_auth)  -> browser-friendly Basic auth
    Depends(require_any_auth)    -> accepts header, Basic, OR cookie

The credentials are read from core/config.py:

    ADMIN_API_KEY             (empty = header auth disabled)
    ADMIN_BASIC_USER          (default "admin")
    ADMIN_BASIC_PASSWORD      (empty = Basic auth disabled)
    ADMIN_SESSION_SECRET      (empty = cookie auth disabled)
    ADMIN_COOKIE_NAME         (default "novag_admin")
    ADMIN_SESSION_TTL_SECONDS (default 8 hours)

All schemes use ``secrets.compare_digest`` / ``hmac.compare_digest`` so
they are timing-safe.

Wired into main.py in three places:

  * ``FastAPI(docs_url=..., redoc_url=..., openapi_url=...)`` is set to
    None unless at least one credential is configured, so the scanner
    cannot map the API from a public box.
  * ``_gate_docs`` middleware protects /docs, /redoc, /openapi.json
    when docs are enabled, so they require auth too.
  * Every admin / logs / isolated / simulation / dummy-order / sandbox
    / margin / daily-backup / readiness / api-explorer router is
    mounted with ``dependencies=[Depends(require_any_auth)]``.

  * ``AuthGuardMiddleware`` (core/auth_guard.py) is registered in
    main.py to protect **every** route not on the public whitelist,
    redirecting browser navigations to /login and returning 401 JSON
    to API/XHR callers. It relies on ``is_authenticated()`` below.
"""

import base64
import hashlib
import hmac
import secrets
import time

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

_basic = HTTPBasic(auto_error=False)


# --------------------------------------------------------------------------- #
# Internal probes                                                             #
# --------------------------------------------------------------------------- #
def _header_ok(request: Request) -> bool:
    """True if X-API-Key or ``Authorization: Bearer`` matches the secret."""
    expected = (getattr(config, "ADMIN_API_KEY", "") or "").strip()
    if not expected:
        return False

    # X-API-Key: <secret>
    key = request.headers.get("x-api-key")
    if key and secrets.compare_digest(key.strip(), expected):
        return True

    # Authorization: Bearer <secret>
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token and secrets.compare_digest(token, expected):
            return True

    return False


def _basic_ok(creds: HTTPBasicCredentials | None) -> bool:
    """True if the Basic username/password matches the configured pair."""
    if creds is None:
        return False

    expected_user = (getattr(config, "ADMIN_BASIC_USER", "") or "").strip()
    expected_pass = (getattr(config, "ADMIN_BASIC_PASSWORD", "") or "")
    if not expected_user or not expected_pass:
        return False

    ok_user = secrets.compare_digest(creds.username, expected_user)
    ok_pass = secrets.compare_digest(creds.password, expected_pass)
    return ok_user and ok_pass


def _basic_from_request(request: Request) -> bool:
    """Decode ``Authorization: Basic ...`` from a raw request.

    Used by the /docs middleware, which does not go through the normal
    ``Depends(_basic)`` path.
    """
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(auth[6:]).decode("utf-8")
    except Exception:
        return False
    user, _, pwd = raw.partition(":")
    if not user and not pwd:
        return False
    expected_user = (getattr(config, "ADMIN_BASIC_USER", "") or "").strip()
    expected_pass = (getattr(config, "ADMIN_BASIC_PASSWORD", "") or "")
    if not expected_user or not expected_pass:
        return False
    return secrets.compare_digest(user, expected_user) and secrets.compare_digest(
        pwd, expected_pass
    )


# --------------------------------------------------------------------------- #
# Cookie helpers                                                              #
# --------------------------------------------------------------------------- #
def _cookie_secret() -> bytes:
    return (getattr(config, "ADMIN_SESSION_SECRET", "") or "").encode()


def _cookie_name() -> str:
    return getattr(config, "ADMIN_COOKIE_NAME", "novag_admin")


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_login_credentials(username: str, password: str) -> bool:
    """Check a username/password pair against the configured Basic pair.

    Used by the /login form handler. Returns False when Basic auth is not
    configured (empty user or password).
    """
    exp_user = (getattr(config, "ADMIN_BASIC_USER", "") or "").strip()
    exp_pass = (getattr(config, "ADMIN_BASIC_PASSWORD", "") or "")
    if not exp_user or not exp_pass:
        return False
    return secrets.compare_digest(username, exp_user) and secrets.compare_digest(
        password, exp_pass
    )


def create_session_cookie_value() -> str:
    """Build a signed ``<payload>.<sig>`` cookie value for the admin user.

    Returns an empty string when ``ADMIN_SESSION_SECRET`` is not configured,
    which signals to the caller that cookie auth is disabled.
    """
    secret = _cookie_secret()
    if not secret:
        return ""
    exp = int(time.time()) + int(getattr(config, "ADMIN_SESSION_TTL_SECONDS", 8 * 3600))
    payload = f"admin:{exp}".encode()
    sig = hmac.new(secret, payload, hashlib.sha256).digest()
    return f"{_b64e(payload)}.{_b64e(sig)}"


def _cookie_ok(request: Request) -> bool:
    """True if the request carries a valid, unexpired session cookie."""
    secret = _cookie_secret()
    if not secret:
        return False
    raw = request.cookies.get(_cookie_name())
    if not raw or "." not in raw:
        return False
    try:
        p_b64, s_b64 = raw.split(".", 1)
        payload, sig = _b64d(p_b64), _b64d(s_b64)
    except Exception:
        return False
    if not hmac.compare_digest(
        sig, hmac.new(secret, payload, hashlib.sha256).digest()
    ):
        return False
    try:
        user, exp_s = payload.decode().split(":", 1)
    except Exception:
        return False
    return user == "admin" and int(exp_s) >= int(time.time())


# --------------------------------------------------------------------------- #
# Public dependencies                                                         #
# --------------------------------------------------------------------------- #
def require_api_key(request: Request) -> None:
    """Header-only auth (Bearer / X-API-Key). 401 otherwise."""
    if not _header_ok(request):
        logger.warning(
            "[auth] rejected api-key request path=%s client=%s",
            request.url.path,
            request.client.host if request.client else "?",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_basic_auth(
    creds: HTTPBasicCredentials | None = Depends(_basic),
) -> None:
    """Browser-friendly HTTP Basic auth. 401 otherwise."""
    if not _basic_ok(creds):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing Basic credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def require_any_auth(
    request: Request,
    creds: HTTPBasicCredentials | None = Depends(_basic),
) -> None:
    """Accept header auth, Basic auth, OR a signed session cookie.

    Raising 401 advertises only ``Basic`` in ``WWW-Authenticate`` so that
    browsers show the familiar login prompt rather than an ambiguous
    multi-scheme challenge.
    """
    if _header_ok(request) or _basic_ok(creds) or _cookie_ok(request):
        return

    logger.warning(
        "[auth] rejected unauth request path=%s client=%s",
        request.url.path,
        request.client.host if request.client else "?",
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": 'Basic realm="admin"'},
    )


# --------------------------------------------------------------------------- #
# Middleware-facing non-raising checks                                        #
# --------------------------------------------------------------------------- #
def is_authenticated(request: Request) -> bool:
    """
    Non-raising auth check for use inside middleware.

    Returns True if the request carries ANY valid credential:
      * X-API-Key / Bearer header
      * HTTP Basic
      * Signed, unexpired session cookie

    Never raises — always returns a bool. Consumed by
    ``AuthGuardMiddleware`` in ``core/auth_guard.py`` (registered in
    ``main.py``) to decide whether to redirect unauth browser navigations
    to /login, return 401 JSON to API/XHR callers, or pass the request
    through untouched.

    Delegates to the same internal probes used by ``require_any_auth`` so
    that middleware and dependency see an identical notion of "logged in"
    — no second source of truth, no drift.
    """
    try:
        return (
            _header_ok(request)
            or _basic_from_request(request)
            or _cookie_ok(request)
        )
    except Exception:
        # Never let a middleware-facing helper raise.
        return False


def docs_request_is_authenticated(request: Request) -> bool:
    """True if the request carries valid header, Basic, or cookie auth.

    Kept as a separately-named alias because ``_gate_docs`` in ``main.py``
    already imports it under this name. Delegates to ``is_authenticated``
    so both names stay permanently in sync.
    """
    return is_authenticated(request)