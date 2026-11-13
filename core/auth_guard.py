# core/auth_guard.py
"""
Global authentication guard middleware.

Public registration and sign-in pages are deliberately accessible without
an existing session. Active members are limited to the read-only market
workspace; administrator credentials retain access to protected routes.
"""
from __future__ import annotations

import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from core import config
from core.auth import is_authenticated
from core.user_accounts import (
    flag_user_security_incident,
    get_security_revocation_notice,
    get_user_session,
)
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

        # Chrome DevTools requests this well-known resource when inspection
        # tooling is opened. Only this exact signal is treated as suspicious.
        devtools_path = "/.well-known/appspecific/com.chrome.devtools.json"
        devtools_logout_enabled = os.getenv(
            "USER_AUTH_DEVTOOLS_LOGOUT_ENABLED", "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        if (
            member
            and devtools_logout_enabled
            and path == devtools_path
            and request.method in {"GET", "HEAD"}
        ):
            client_ip = request.client.host if request.client else "unknown"
            flagged = flag_user_security_incident(
                member.get("user_id"),
                {
                    "reason": "Chrome DevTools inspection endpoint accessed",
                    "path": path,
                    "client_ip": client_ip,
                    "user_agent": request.headers.get("user-agent", ""),
                },
            )
            logger.warning(
                "[security] member session revoked after DevTools inspection signal user_id=%s client=%s recorded=%s",
                member.get("user_id"), client_ip, flagged,
            )
            response = Response(status_code=204)
            response.delete_cookie(
                os.getenv("USER_AUTH_COOKIE_NAME", "market_member"),
                path="/",
                httponly=True,
                secure=bool(config.ADMIN_COOKIE_SECURE),
                samesite="lax",
            )
            return response

        if member:
            read_only_methods = {"GET", "HEAD"}

            # Shared member workspace APIs. These are strictly read-only;
            # subscription changes, order actions and all other writes remain
            # administrator-only.
            member_read_only_upstox = (
                path in {
                    "/api/upstox/search",
                    "/api/upstox/instrument",
                    "/api/upstox/intraday",
                    "/api/upstox/historical",
                    "/api/upstox/subscription-status",
                }
                and request.method in read_only_methods
            )
            member_read_only_isolation = (
                path.startswith("/admin/level-isolation/")
                and request.method in read_only_methods
            )
            member_read_only_sr = (
                path.startswith("/live-sr/")
                and request.method in read_only_methods
            )

            member_page = (
                path in {"/", "/charts", "/isolated", "/positions", "/user/me"}
                and request.method in read_only_methods
            )
            member_live_api = (
                path in {"/live/status", "/live/instruments"}
                and request.method in read_only_methods
            )
            member_chart_page = (
                path.startswith("/chart/")
                and request.method in read_only_methods
            )
            member_live_read = (
                path.startswith("/live/")
                and request.method in read_only_methods
            )
            member_websocket = path in {"/ws/live", "/ws/chart"}

            if (
                member_page
                or member_live_api
                or member_chart_page
                or member_live_read
                or member_websocket
                or member_read_only_upstox
                or member_read_only_isolation
                or member_read_only_sr
            ):
                request.state.member = member
                request.state.is_admin = False
                return await call_next(request)

            request.state.member = member
            request.state.is_admin = False
            if _client_wants_html(request):
                from routes.page_routes import templates
                return templates.TemplateResponse(
                    request=request,
                    name="restricted.html",
                    context={
                        "app_name": "NG7",
                        "requested_path": path,
                    },
                    status_code=403,
                    headers={"Cache-Control": "no-store"},
                )
            return JSONResponse(
                status_code=403,
                content={"detail": "This area is available to administrators only."},
                headers={"Cache-Control": "no-store"},
            )

        if is_authenticated(request):
            request.state.is_admin = True
            request.state.member = None
            return await call_next(request)

        if get_security_revocation_notice(request):
            response = RedirectResponse("/user/login?security_logout=1", status_code=303)
            response.delete_cookie(
                os.getenv("USER_AUTH_COOKIE_NAME", "market_member"),
                path="/",
                httponly=True,
                secure=bool(config.ADMIN_COOKIE_SECURE),
                samesite="lax",
            )
            return response

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
