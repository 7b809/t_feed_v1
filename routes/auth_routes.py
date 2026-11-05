# routes/auth_routes.py
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from core import config
from core.auth import (
    create_session_cookie_value, verify_login_credentials, require_any_auth,
)
from core.logger import get_logger
from fastapi import Depends

logger = get_logger(__file__)
router = APIRouter(tags=["auth"])

def _safe_next(n: str | None) -> str:
    if not n or not n.startswith("/") or n.startswith("//"):
        return "/"
    return n

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    from routes.page_routes import templates  # reuse existing Jinja env
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": _safe_next(next), "error": None},
    )

@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    from routes.page_routes import templates
    if not verify_login_credentials(username, password):
        logger.warning("[auth] login failed user=%s ip=%s",
                       username, request.client.host if request.client else "?")
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": _safe_next(next),
             "error": "Invalid username or password"},
            status_code=401,
        )
    resp = RedirectResponse(_safe_next(next), status_code=303)
    resp.set_cookie(
        key=config.ADMIN_COOKIE_NAME,
        value=create_session_cookie_value(),
        max_age=int(config.ADMIN_SESSION_TTL_SECONDS),
        httponly=True, secure=bool(config.ADMIN_COOKIE_SECURE),
        samesite="lax", path="/",
    )
    logger.info("[auth] login ok ip=%s",
                request.client.host if request.client else "?")
    return resp

# routes/auth_routes.py — replace the existing logout() handler with this

@router.get("/logout")
def logout(request: Request):
    """
    Clear the admin session cookie.

    * Browser navigation (Accept: text/html)  → 303 redirect to /login
    * API / fetch / curl  (Accept: JSON / */*)→ 200 JSON {"ok": true, "logged_out": true}

    This dual behaviour means the same URL works from the nav-bar
    "Logout" button AND from a programmatic `fetch('/logout')` call.
    """
    # Decide response type based on Accept header.
    accept = (request.headers.get("accept") or "").lower()
    wants_html = "text/html" in accept and "application/json" not in accept

    if wants_html:
        resp = RedirectResponse("/login", status_code=303)
    else:
        resp = JSONResponse({
            "ok": True,
            "logged_out": True,
            "login_url": "/login",
        })

    # Clear the cookie on BOTH branches — this is what actually logs the user out.
    resp.delete_cookie(
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
    return resp


@router.get("/auth/whoami")
def whoami(_=Depends(require_any_auth)):
    return JSONResponse({"authenticated": True})