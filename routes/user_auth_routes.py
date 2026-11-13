"""Member registration, sign-in and administrator account management."""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core.user_accounts import (
    authenticate_user, create_user_session, delete_user, get_security_revocation_notice,
    get_user_session, list_users, register_user, revoke_user_sessions, set_user_login_enabled,
    update_user_profile, update_user_status,
)

# ---------------------------------------------------------------------------
# NOTE for maintainers
# ---------------------------------------------------------------------------
# If /auth/whoami and/or /logout are already defined in another router (for
# example routes/admin_auth.py), delete the corresponding definitions further
# down in this file to avoid FastAPI "duplicate path" errors at startup.
# ---------------------------------------------------------------------------

router = APIRouter(tags=["platform-accounts"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _templates():
    from routes.page_routes import templates
    return templates


def _cookie_name() -> str:
    return os.getenv("USER_AUTH_COOKIE_NAME", "market_member")


def _admin_cookie_name() -> str:
    return os.getenv("ADMIN_AUTH_COOKIE_NAME", "market_admin")


def _cookie_max_age() -> int:
    return max(900, int(os.getenv("USER_AUTH_SESSION_TTL_SECONDS", "28800")))


def _set_cookie(response, token):
    from core import config
    response.set_cookie(
        key=_cookie_name(),
        value=token,
        max_age=_cookie_max_age(),
        httponly=True,
        secure=bool(config.ADMIN_COOKIE_SECURE),
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _safe_next(next_url: Optional[str], default: str = "/") -> str:
    """Sanitise a ?next= redirect target.

    Only same-origin, absolute-path URLs are allowed. Blocks:
      * protocol-relative URLs (//evil.com)
      * backslash tricks (/\\evil.com which some browsers treat as //evil.com)
      * redirect loops (/user/login, /admin/login)
    """
    if not next_url:
        return default
    if not isinstance(next_url, str):
        return default
    if not next_url.startswith("/"):
        return default
    if next_url.startswith("//"):
        return default
    if "\\" in next_url:
        return default
    if next_url.startswith("/user/login") or next_url.startswith("/admin/login"):
        return default
    return next_url


def _try_admin_session(request: Request):
    """Best-effort admin session lookup.

    Tries several mechanisms so this works regardless of how admin auth is
    wired up in the rest of the project:
        1. request.state (set by AuthGuardMiddleware)
        2. common helper import paths (core.admin_accounts / core.admin_auth)
        3. presence of the admin auth cookie (UI-only signal — the middleware
           is still responsible for enforcing admin access)
    """
    # 1. Middleware-set state
    for attr in ("admin", "admin_user", "admin_session"):
        val = getattr(request.state, attr, None)
        if val:
            return val

    # 2. Standard import paths
    for module_path, func_name in (
        ("core.admin_accounts", "get_admin_session"),
        ("core.admin_auth", "get_admin_session"),
        ("core.auth", "get_admin_session"),
    ):
        try:
            module = __import__(module_path, fromlist=[func_name])
            func = getattr(module, func_name, None)
            if callable(func):
                result = func(request)
                if result:
                    return result
        except Exception:
            continue

    # 3. Cookie presence as a UI-only signal
    if request.cookies.get(_admin_cookie_name()):
        return {"cookie_present": True}

    return None


def _clamp_limit(limit: int, default: int = 300, maximum: int = 1000) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return min(n, maximum)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return _templates().TemplateResponse(
        request=request,
        name="register.html",
        context={"error": None, "success": None, "name": "", "email": ""},
    )


@router.post("/register", response_class=HTMLResponse)
def register_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
):
    try:
        result = register_user(name, email, password)
    except HTTPException as exc:
        return _templates().TemplateResponse(
            request=request,
            name="register.html",
            context={"error": exc.detail, "success": None, "name": name, "email": email},
            status_code=exc.status_code,
        )
    return _templates().TemplateResponse(
        request=request,
        name="register.html",
        context={
            "error": None,
            "success": (
                "Registration received. Your account is pending administrator "
                "approval. You can sign in after approval."
            ),
            "name": result.get("name", name),
            "email": result.get("email", email),
        },
        status_code=201,
    )


# ---------------------------------------------------------------------------
# Member sign-in / sign-out
# ---------------------------------------------------------------------------

@router.get("/user/login", response_class=HTMLResponse)
def member_login_page(
    request: Request,
    next: Optional[str] = None,
    security_logout: Optional[str] = None,
):
    # The "next" value is rendered into the template so a hidden form field
    # can carry it through to the POST. Existing templates that ignore
    # {"next"} are unaffected.
    return _templates().TemplateResponse(
        request=request,
        name="user_login.html",
        context={
            "error": None,
            "message": None,
            "next": _safe_next(next),
            "security_logout": security_logout == "1",
        },
    )


@router.post("/user/login", response_class=HTMLResponse)
def member_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: Optional[str] = Form(None),
):
    redirect_to = _safe_next(next)
    try:
        user = authenticate_user(email, password)
        token = create_user_session(user)
    except HTTPException as exc:
        return _templates().TemplateResponse(
            request=request,
            name="user_login.html",
            context={"error": exc.detail, "message": None, "next": redirect_to},
            status_code=exc.status_code,
        )
    except RuntimeError as exc:
        return _templates().TemplateResponse(
            request=request,
            name="user_login.html",
            context={"error": str(exc), "message": None, "next": redirect_to},
            status_code=503,
        )
    return _set_cookie(RedirectResponse(redirect_to, status_code=303), token)


@router.get("/user/logout")
@router.post("/user/logout")
def member_logout():
    """End the member session and always return the browser to member sign-in.

    GET is supported for compatibility with existing navbar links; POST is
    also supported for form-based logout controls.
    """
    from core import config
    response = RedirectResponse("/user/login", status_code=303)
    response.delete_cookie(
        _cookie_name(),
        path="/",
        httponly=True,
        secure=bool(config.ADMIN_COOKIE_SECURE),
        samesite="lax",
    )
    return response


@router.get("/user/me")
def member_me(request: Request):
    user = get_user_session(request)
    if not user:
        raise HTTPException(401, "Member session required")
    return {"authenticated": True, "user": user}


# ---------------------------------------------------------------------------
# Session introspection — used by base.html role-aware navigation
# ---------------------------------------------------------------------------

@router.get("/auth/whoami")
def auth_whoami(request: Request):
    """Return the current authenticated role for navbar rendering.

    Always responds with 200 so the global 401 fetch interceptor in
    base.html does NOT try to redirect the user to a login page. When the
    caller is unauthenticated we simply return ``role: null`` and the JS
    treats that identically to a failed check.

    Response shape:
        {"role": "admin"|"member"|null, "user": {...}|null}
    """
    # 1. Admin session
    try:
        admin = _try_admin_session(request)
    except Exception:
        admin = None
    if admin:
        return JSONResponse(
            {"role": "admin", "user": admin},
            headers={"Cache-Control": "no-store"},
        )

    # 2. Member session
    try:
        member = get_user_session(request)
    except Exception:
        member = None
    if member:
        return JSONResponse(
            {"role": "member", "user": member},
            headers={"Cache-Control": "no-store"},
        )

    # 3. Unauthenticated — include the reason when a security incident
    # invalidated the member's session.
    security_logout = get_security_revocation_notice(request)
    return JSONResponse(
        {"role": None, "authenticated": False, "security_logout": security_logout},
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Unified logout — used by admin panel + base.html when role === "admin"
# ---------------------------------------------------------------------------

@router.get("/logout")
@router.post("/logout")
def unified_logout(request: Request):
    """Clear both member and admin cookies and redirect to sign-in."""
    from core import config
    response = RedirectResponse("/user/login", status_code=303)
    response.delete_cookie(
        _cookie_name(),
        path="/",
        httponly=True,
        secure=bool(config.ADMIN_COOKIE_SECURE),
        samesite="lax",
    )
    response.delete_cookie(
        _admin_cookie_name(),
        path="/",
        httponly=True,
        secure=bool(config.ADMIN_COOKIE_SECURE),
        samesite="lax",
    )
    return response


# ---------------------------------------------------------------------------
# Admin panel (HTML)
# ---------------------------------------------------------------------------

# This HTML route is protected by AuthGuardMiddleware; member sessions are
# explicitly denied access to /admin/* by that middleware.
@router.get("/admin/users/panel", response_class=HTMLResponse)
def admin_users_panel(request: Request):
    return _templates().TemplateResponse(
        request=request,
        name="admin_users.html",
        context={"app_name": "NG7"},
    )


# ---------------------------------------------------------------------------
# Admin API (JSON) — consumed by admin_users.html
# ---------------------------------------------------------------------------

@router.get("/admin/users")
def admin_list_users(status: Optional[str] = None, limit: int = 300):
    try:
        return {"users": list_users(status=status, limit=_clamp_limit(limit))}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Failed to list users: {exc}")


@router.post("/admin/users/{user_id}/approve")
def admin_approve_user(user_id: str):
    try:
        return update_user_status(user_id, "active")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Failed to approve user: {exc}")


@router.post("/admin/users/{user_id}/suspend")
def admin_suspend_user(user_id: str):
    try:
        return update_user_status(user_id, "suspended")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Failed to suspend user: {exc}")


@router.post("/admin/users/{user_id}/reject")
def admin_reject_user(user_id: str):
    try:
        return update_user_status(user_id, "rejected")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Failed to reject user: {exc}")


@router.patch("/admin/users/{user_id}/profile")
def admin_update_user_profile(user_id: str, payload: dict):
    payload = payload or {}
    name = payload.get("name")
    email = payload.get("email")

    if name is None and email is None:
        raise HTTPException(400, "At least one of 'name' or 'email' must be provided")

    if name is not None:
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(400, "Name must be a non-empty string")
        if len(name.strip()) > 120:
            raise HTTPException(400, "Name is too long (max 120 characters)")

    if email is not None:
        if not isinstance(email, str) or "@" not in email:
            raise HTTPException(400, "Email must be a valid address")
        if len(email.strip()) > 254:
            raise HTTPException(400, "Email is too long")

    try:
        return update_user_profile(user_id, name, email)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Failed to update profile: {exc}")


@router.post("/admin/users/{user_id}/login-enable")
def admin_enable_user_login(user_id: str):
    try:
        return set_user_login_enabled(user_id, True)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Failed to enable user login: {exc}")


@router.post("/admin/users/{user_id}/login-disable")
def admin_disable_user_login(user_id: str):
    try:
        return set_user_login_enabled(user_id, False)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Failed to disable user login: {exc}")


@router.post("/admin/users/{user_id}/revoke-sessions")
def admin_revoke_sessions(user_id: str):
    try:
        return revoke_user_sessions(user_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Failed to revoke sessions: {exc}")


@router.delete("/admin/users/{user_id}")
def admin_delete_user(user_id: str):
    try:
        return delete_user(user_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Failed to delete user: {exc}")