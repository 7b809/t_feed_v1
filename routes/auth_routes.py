# routes/auth_routes.py
"""Administrator browser login and logout routes."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core import config
from core.auth import create_session_cookie_value, verify_login_credentials, require_any_auth, is_authenticated
from core.user_accounts import get_user_session
from core.logger import get_logger

logger = get_logger(__file__)
router = APIRouter(tags=["auth"])


def _safe_next(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def _render_admin_login(request: Request, next_path: str, error: str | None = None,
                        form_action: str = "/admin/login", status_code: int = 200):
    from routes.page_routes import templates
    return templates.TemplateResponse(
        request=request,
        name="admin_login.html",
        context={
            "next": _safe_next(next_path),
            "error": error,
            "form_action": form_action,
        },
        status_code=status_code,
    )


@router.get("/login")
def default_login():
    """Default browser login is for members; admin login has its own route."""
    return RedirectResponse("/user/login", status_code=303)


@router.get("/admin/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/admin/users/panel"):
    action = "/admin/login"
    return _render_admin_login(request, next, form_action=action)


@router.post("/admin/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    if not verify_login_credentials(username, password):
        logger.warning(
            "[auth] login failed user=%s ip=%s",
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
        logger.error("[auth] admin login blocked: ADMIN_SESSION_SECRET is not configured")
        return _render_admin_login(
            request,
            next,
            error="Administrator session is not configured. Set ADMIN_SESSION_SECRET and restart the application.",
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
    logger.info("[auth] login ok ip=%s", request.client.host if request.client else "?")
    return response


@router.get("/logout")
def logout(request: Request):
    """Clear the administrator session cookie."""
    accept = (request.headers.get("accept") or "").lower()
    wants_html = "text/html" in accept and "application/json" not in accept

    if wants_html:
        response = RedirectResponse("/admin/login", status_code=303)
    else:
        response = JSONResponse({
            "ok": True,
            "logged_out": True,
            "login_url": "/login",
        })

    response.delete_cookie(
        key=config.ADMIN_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=bool(config.ADMIN_COOKIE_SECURE),
        samesite="lax",
    )
    logger.info(
        "[auth] logout ip=%s mode=%s",
        request.client.host if request.client else "?",
        "html" if wants_html else "json",
    )
    return response


@router.get("/auth/whoami")
def whoami(request: Request):
    """Return the active browser identity without forcing members through admin auth."""
    member = get_user_session(request)
    if member:
        return JSONResponse({"authenticated": True, "role": "member", "user": {
            "name": member.get("name"), "email": member.get("email")
        }})
    if is_authenticated(request):
        return JSONResponse({"authenticated": True, "role": "admin"})
    return JSONResponse(
        {"authenticated": False, "role": None, "login_url": "/user/login"},
        status_code=401,
        headers={"Cache-Control": "no-store"},
    )
