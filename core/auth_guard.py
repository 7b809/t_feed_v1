# core/auth_guard.py
"""
Global authentication guard middleware.

Public registration and sign-in pages are deliberately accessible without
an existing session. Active members are limited to the read-only market
workspace; administrator credentials retain access to protected routes.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from core.auth import is_authenticated
from core.user_accounts import get_user_session
from core.logger import get_logger

logger = get_logger(__file__)

PUBLIC_EXACT = {
    "/login",
    "/admin/login",
    "/logout",
    "/auth/whoami",
    "/register",
    "/user/login",
    "/user/logout",
    "/favicon.ico",
    "/robots.txt",
}

PUBLIC_PREFIXES = (
    "/static/",
    "/assets/",
    "/_health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/api/isolation",
)


def _is_public(path: str) -> bool:
    return path in PUBLIC_EXACT or any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def _client_wants_html(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept or request.headers.get("x-requested-with", "").lower() == "xmlhttprequest":
        return False
    return "text/html" in accept


def _safe_next(path: str, query: str) -> str:
    target = path + (("?" + query) if query else "")
    return target if target.startswith("/") and not target.startswith("//") else "/"


class AuthGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if _is_public(path):
            return await call_next(request)

        member = get_user_session(request)
        if member:
            allowed_exact = {
                "/", "/charts", "/user/me", "/live/status", "/live/instruments",
                "/ws/live", "/ws/chart",
            }
            allowed_prefixes = ("/chart/", "/live/")
            if path in allowed_exact or any(path.startswith(prefix) for prefix in allowed_prefixes):
                request.state.member = member
                return await call_next(request)
            return JSONResponse(
                status_code=403,
                content={"detail": "This area is available to administrators only."},
                headers={"Cache-Control": "no-store"},
            )

        if is_authenticated(request):
            return await call_next(request)

        client = request.client.host if request.client else "?"
        if _client_wants_html(request):
            destination = _safe_next(path, request.url.query or "")
            logger.info("[auth-guard] redirect unauth HTML request -> /login — path=%s client=%s", path, client)
            return RedirectResponse(
                url=f"/login?next={destination}",
                status_code=303,
                headers={"Cache-Control": "no-store"},
            )

        logger.warning("[auth-guard] 401 unauth API request — path=%s client=%s", path, client)
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required", "login_url": "/login"},
            headers={"Cache-Control": "no-store"},
        )
