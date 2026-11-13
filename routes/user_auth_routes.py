"""Member registration, sign-in and administrator account management."""
from __future__ import annotations

import os

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core.user_accounts import (
    authenticate_user, create_user_session, delete_user, get_user_session,
    list_users, register_user, revoke_user_sessions, set_user_login_enabled,
    update_user_profile, update_user_status,
)

router = APIRouter(tags=["platform-accounts"])

def _templates():
    from routes.page_routes import templates
    return templates

def _set_cookie(response, token):
    from core import config
    response.set_cookie(
        key=os.getenv("USER_AUTH_COOKIE_NAME", "market_member"),
        value=token,
        max_age=max(900, int(os.getenv("USER_AUTH_SESSION_TTL_SECONDS", "28800"))),
        httponly=True, secure=bool(config.ADMIN_COOKIE_SECURE),
        samesite="lax", path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response

@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return _templates().TemplateResponse(
        request=request, name="register.html",
        context={"error": None, "success": None, "name": "", "email": ""},
    )

@router.post("/register", response_class=HTMLResponse)
def register_submit(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...)):
    try:
        result = register_user(name, email, password)
    except HTTPException as exc:
        return _templates().TemplateResponse(
            request=request, name="register.html",
            context={"error": exc.detail, "success": None, "name": name, "email": email},
            status_code=exc.status_code,
        )
    return _templates().TemplateResponse(
        request=request, name="register.html",
        context={
            "error": None,
            "success": "Registration received. Your account is pending administrator approval. You can sign in after approval.",
            "name": result.get("name", name), "email": result.get("email", email),
        }, status_code=201,
    )

@router.get("/user/login", response_class=HTMLResponse)
def member_login_page(request: Request):
    return _templates().TemplateResponse(
        request=request, name="user_login.html",
        context={"error": None, "message": None},
    )

@router.post("/user/login", response_class=HTMLResponse)
def member_login(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        user = authenticate_user(email, password)
        token = create_user_session(user)
    except HTTPException as exc:
        return _templates().TemplateResponse(
            request=request, name="user_login.html",
            context={"error": exc.detail, "message": None}, status_code=exc.status_code,
        )
    except RuntimeError as exc:
        return _templates().TemplateResponse(
            request=request, name="user_login.html",
            context={"error": str(exc), "message": None}, status_code=503,
        )
    return _set_cookie(RedirectResponse("/", status_code=303), token)

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
        os.getenv("USER_AUTH_COOKIE_NAME", "market_member"), path="/",
        httponly=True, secure=bool(config.ADMIN_COOKIE_SECURE), samesite="lax",
    )
    return response

@router.get("/user/me")
def member_me(request: Request):
    user = get_user_session(request)
    if not user:
        raise HTTPException(401, "Member session required")
    return {"authenticated": True, "user": user}

# This HTML route is protected by AuthGuardMiddleware; member sessions are
# explicitly denied access to /admin/* by that middleware.
@router.get("/admin/users/panel", response_class=HTMLResponse)
def admin_users_panel(request: Request):
    return _templates().TemplateResponse(
        request=request, name="admin_users.html",
        context={"app_name": "NG7"},
    )

@router.get("/admin/users")
def admin_list_users(status: str | None = None, limit: int = 300):
    return {"users": list_users(status=status, limit=limit)}

@router.post("/admin/users/{user_id}/approve")
def admin_approve_user(user_id: str):
    return update_user_status(user_id, "active")

@router.post("/admin/users/{user_id}/suspend")
def admin_suspend_user(user_id: str):
    return update_user_status(user_id, "suspended")

@router.post("/admin/users/{user_id}/reject")
def admin_reject_user(user_id: str):
    return update_user_status(user_id, "rejected")

@router.patch("/admin/users/{user_id}/profile")
def admin_update_user_profile(user_id: str, payload: dict):
    return update_user_profile(user_id, payload.get("name"), payload.get("email"))

@router.post("/admin/users/{user_id}/login-enable")
def admin_enable_user_login(user_id: str):
    return set_user_login_enabled(user_id, True)

@router.post("/admin/users/{user_id}/login-disable")
def admin_disable_user_login(user_id: str):
    return set_user_login_enabled(user_id, False)

@router.post("/admin/users/{user_id}/revoke-sessions")
def admin_revoke_sessions(user_id: str):
    return revoke_user_sessions(user_id)

@router.delete("/admin/users/{user_id}")
def admin_delete_user(user_id: str):
    return delete_user(user_id)
