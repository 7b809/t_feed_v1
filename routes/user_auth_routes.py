"""Member registration, sign-in and administrator account management."""
from __future__ import annotations
import os
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from core.user_accounts import (authenticate_user, create_user_session, get_user_session,
    list_users, register_user, revoke_user_sessions, update_user_status)

router = APIRouter(tags=["platform-accounts"])

def _set_cookie(response, token):
    from core import config
    response.set_cookie(key=os.getenv("USER_AUTH_COOKIE_NAME", "market_member"),
        value=token, max_age=max(900, int(os.getenv("USER_AUTH_SESSION_TTL_SECONDS", "28800"))),
        httponly=True, secure=bool(config.ADMIN_COOKIE_SECURE), samesite="lax", path="/")
    response.headers["Cache-Control"] = "no-store"
    return response

def _form(title, action, fields, footer):
    controls = "".join("<input " + attrs + ">" for attrs in fields)
    return HTMLResponse("<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'><title>" + title + "</title><style>body{margin:0;background:#0f172a;color:#f8fafc;font:16px system-ui;display:grid;min-height:100vh;place-items:center}.card{width:min(420px,calc(100% - 40px));background:#1e293b;border:1px solid #334155;border-radius:18px;padding:28px;box-sizing:border-box}input,button{width:100%;box-sizing:border-box;padding:13px;margin:8px 0;border-radius:9px;border:1px solid #475569;background:#0f172a;color:white}button{background:#2563eb;border:0;font-weight:700;cursor:pointer}.muted{color:#94a3b8;font-size:13px}a{color:#93c5fd}</style></head><body><form class='card' method='post' action='" + action + "'><h1>" + title + "</h1><p class='muted'>Market Engine member access</p>" + controls + "<button>Continue</button><p class='muted'>" + footer + "</p></form></body></html>")

@router.get("/register", response_class=HTMLResponse)
def register_page():
    return _form("Create account", "/register", [
        "name='name' placeholder='Full name' maxlength='100' required",
        "name='email' type='email' placeholder='Email address' required",
        "name='password' type='password' minlength='12' placeholder='Password (12+ characters)' required"
    ], "New accounts require administrator approval. <a href='/user/login'>Sign in</a>")

@router.post("/register")
def register_submit(name: str = Form(...), email: str = Form(...), password: str = Form(...)):
    register_user(name, email, password)
    return HTMLResponse("<h2>Registration received</h2><p>Your account is pending administrator approval.</p><a href='/user/login'>Sign in</a>", status_code=201)

@router.get("/user/login", response_class=HTMLResponse)
def member_login_page():
    return _form("Member sign in", "/user/login", [
        "name='email' type='email' placeholder='Email address' required",
        "name='password' type='password' placeholder='Password' required"
    ], "New here? <a href='/register'>Create account</a> · <a href='/login'>Administrator login</a>")

@router.post("/user/login")
def member_login(email: str = Form(...), password: str = Form(...)):
    user = authenticate_user(email, password)
    try:
        token = create_user_session(user)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    return _set_cookie(RedirectResponse("/", status_code=303), token)

@router.post("/user/logout")
def member_logout():
    response = RedirectResponse("/user/login", status_code=303)
    response.delete_cookie(os.getenv("USER_AUTH_COOKIE_NAME", "market_member"), path="/", httponly=True, samesite="lax")
    return response

@router.get("/user/me")
def member_me(request: Request):
    user = get_user_session(request)
    if not user:
        raise HTTPException(401, "Member session required")
    return {"authenticated": True, "user": user}

@router.get("/admin/users")
def admin_list_users(status: str | None = None, limit: int = 100):
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

@router.post("/admin/users/{user_id}/revoke-sessions")
def admin_revoke_sessions(user_id: str):
    return revoke_user_sessions(user_id)
