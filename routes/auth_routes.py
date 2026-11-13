# routes/auth_routes.py
"""Administrator browser login, logout and identity-introspection routes.

Companion to `routes/auth.py` which owns member registration, member
sign-in / sign-out and the JSON admin user-management API.

This module is the *only* place that defines:

    GET  /login               -> 303 to /user/login
    GET  /admin/login         -> admin login form
    POST /admin/login         -> verify credentials, issue admin cookie
    GET  /logout              -> unified logout (HTML + JSON)
    POST /logout              -> same, for form/fetch clients
    GET  /auth/whoami         -> always 200, returns {role: "admin"|"member"|null}

`base.html` calls `/auth/whoami` on every page load and inspects the
`role` field to decide which nav items ("Admin Panel", "Logout" etc.) to
reveal. It uses a global fetch() interceptor that turns any HTTP 401
into a redirect to /user/login or /admin/login — which is why whoami
must never return 401 for anonymous visitors. See the docstring on
`whoami()` below.
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request  # noqa: F401  (Depends kept for API stability)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core import config
from core.auth import (
    create_session_cookie_value,
    verify_login_credentials,
    require_any_auth,   # noqa: F401  (retained for back-compat with prior imports)
    is_authenticated,
)
from core.user_accounts import get_user_session
from core.logger import get_logger

logger = get_logger(__file__)
router = APIRouter(tags=["auth"])


# ---------------------------------------------------------------------------
# Constants (kept in one place so both this file and routes/auth.py agree)
# ---------------------------------------------------------------------------

# Mirror of the member cookie name used by routes/auth.py::_cookie_name().
_MEMBER_COOKIE_NAME = os.getenv("USER_AUTH_COOKIE_NAME", "market_member")
_USER_LOGIN_PATH = "/user/login"
_ADMIN_LOGIN_PATH = "/admin/login"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_next(value: Optional[str], fallback: str = "/") -> str:
    """Reject anything that isn't a same-origin absolute path.

    Blocks:
      * protocol-relative URLs    //evil.com
      * backslash tricks          /\\evil.com  (some browsers coerce to //evil.com)
      * redirect loops            /user/login, /admin/login
      * non-absolute paths        evil.com, javascript:...
    """
    if not value or not isinstance(value, str):
        return fallback
    if not value.startswith("/"):
        return fallback
    if value.startswith("//"):
        return fallback
    if "\\" in value:
        return fallback
    if value.startswith(_USER_LOGIN_PATH) or value.startswith(_ADMIN_LOGIN_PATH):
        return fallback
    return value


def _no_store(response):
    """Attach no-store so proxies/CDNs don't cache auth responses."""
    response.headers["Cache-Control"] = "no-store"
    return response


def _render_admin_login(
    request: Request,
    next_path: str,
    error: Optional[str] = None,
    form_action: str = _ADMIN_LOGIN_PATH,
    status_code: int = 200,
):
    from routes.page_routes import templates

    response = templates.TemplateResponse(
        request=request,
        name="admin_login.html",
        context={
            "next": _safe_next(next_path),
            "error": error,
            "form_action": form_action,
        },
        status_code=status_code,
    )
    return _no_store(response)


# ---------------------------------------------------------------------------
# Default entry point
# ---------------------------------------------------------------------------

@router.get("/login")
def default_login():
    """Default browser login is for members; admin login has its own route."""
    return _no_store(RedirectResponse(_USER_LOGIN_PATH, status_code=303))


# ---------------------------------------------------------------------------
# Administrator browser login
# ---------------------------------------------------------------------------

@router.get("/admin/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/admin/users/panel"):
    return _render_admin_login(request, next, form_action=_ADMIN_LOGIN_PATH)


@router.post("/admin/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    if not verify_login_credentials(username, password):
        logger.warning(
            "[auth] admin login failed user=%s ip=%s",
            username,
            request.client.host if request.client else "?",
        )
        return _render_admin_login(
            request,
            next,
            error="Invalid username or password",
            form_action=request.url.path,
            status_code=401,
        )

    cookie_value = create_session_cookie_value()
    if not cookie_value:
        logger.error(
            "[auth] admin login blocked: ADMIN_SESSION_SECRET is not configured"
        )
        return _render_admin_login(
            request,
            next,
            error=(
                "Administrator session is not configured. "
                "Set ADMIN_SESSION_SECRET and restart the application."
            ),
            form_action=request.url.path,
            status_code=503,
        )

    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(
        key=config.ADMIN_COOKIE_NAME,
        value=cookie_value,
        max_age=int(config.ADMIN_SESSION_TTL_SECONDS),
        httponly=True,
        secure=bool(config.ADMIN_COOKIE_SECURE),
        samesite="lax",
        path="/",
    )
    logger.info(
        "[auth] admin login ok ip=%s",
        request.client.host if request.client else "?",
    )
    return _no_store(response)


# ---------------------------------------------------------------------------
# Unified logout — clears BOTH administrator and member cookies
# ---------------------------------------------------------------------------

@router.get("/logout")
@router.post("/logout")
def logout(request: Request):
    """Unified logout for both auth surfaces.

    `base.html` renders the nav Logout link as href="/logout" when the
    detected role is "admin"; `admin_users.html` also links to /logout.
    Content negotiation keeps existing JSON-API callers working:

      * Accept: text/html        -> 303 to the appropriate login page
      * Accept: application/json -> JSON {ok:true, logged_out:true, ...}

    Both the administrator cookie and the member cookie are cleared, so
    this endpoint always ends the browser session regardless of which
    surface the user reached it from.
    """
    accept = (request.headers.get("accept") or "").lower()
    wants_html = "text/html" in accept and "application/json" not in accept

    # Decide post-logout destination BEFORE clearing cookies so we can
    # send admins back to /admin/login and members back to /user/login.
    try:
        was_admin = bool(is_authenticated(request))
    except Exception:
        was_admin = False
    if not was_admin:
        try:
            was_admin = bool(request.cookies.get(config.ADMIN_COOKIE_NAME))
        except Exception:
            pass

    if wants_html:
        target = _ADMIN_LOGIN_PATH if was_admin else _USER_LOGIN_PATH
        response = RedirectResponse(target, status_code=303)
    else:
        response = JSONResponse({
            "ok": True,
            "logged_out": True,
            "login_url": _ADMIN_LOGIN_PATH if was_admin else _USER_LOGIN_PATH,
            "admin_login_url": _ADMIN_LOGIN_PATH,
            "member_login_url": _USER_LOGIN_PATH,
        })

    # Clear administrator cookie
    response.delete_cookie(
        key=config.ADMIN_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=bool(config.ADMIN_COOKIE_SECURE),
        samesite="lax",
    )
    # Also clear member cookie — /logout is the "log me out everywhere" exit.
    response.delete_cookie(
        key=_MEMBER_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=bool(config.ADMIN_COOKIE_SECURE),
        samesite="lax",
    )

    logger.info(
        "[auth] logout ip=%s mode=%s was_admin=%s",
        request.client.host if request.client else "?",
        "html" if wants_html else "json",
        was_admin,
    )
    return _no_store(response)


# ---------------------------------------------------------------------------
# Identity introspection — used by base.html role-aware navigation
# ---------------------------------------------------------------------------

@router.get("/auth/whoami")
def whoami(request: Request):
    """Return the active browser identity.

    **Always responds with HTTP 200** — even for anonymous visitors —
    because `base.html` installs a global `fetch()` interceptor that
    converts any 401 into a redirect to /user/login or /admin/login.
    If whoami 401'd on public pages, every anonymous homepage load would
    be bounced through a login redirect. Returning 200 with
    `{"role": null}` lets the interceptor distinguish "auth failure on
    a protected resource" (real 401 → redirect) from "not logged in but
    allowed to view this page" (200 + null role → show Login link).

    Precedence: administrator > member. If both cookies are somehow
    present, admin identity wins so the "Admin Panel" nav link appears
    correctly on the admin panel page.

    Response shapes (all 200):
        {"authenticated": true,  "role": "admin",  "user": {...}}
        {"authenticated": true,  "role": "member", "user": {name, email}}
        {"authenticated": false, "role": null,
         "login_url": "/user/login", "admin_login_url": "/admin/login"}
    """
    # Administrator session takes precedence.
    try:
        if is_authenticated(request):
            return _no_store(JSONResponse({
                "authenticated": True,
                "role": "admin",
                "user": {"role": "admin"},
            }))
    except Exception:
        logger.exception("[auth] whoami: admin check failed")

    # Fall back to member session.
    try:
        member = get_user_session(request)
    except Exception:
        member = None
    if member:
        return _no_store(JSONResponse({
            "authenticated": True,
            "role": "member",
            "user": {
                "name": member.get("name"),
                "email": member.get("email"),
            },
        }))

    # Anonymous — 200 + role:null (see docstring).
    return _no_store(JSONResponse({
        "authenticated": False,
        "role": None,
        "login_url": _USER_LOGIN_PATH,
        "admin_login_url": _ADMIN_LOGIN_PATH,
    }))