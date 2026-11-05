# core/auth_guard.py
"""
Global authentication guard middleware.

Every request (HTML, API, WebSocket upgrade) passes through here before
reaching the router. Behaviour:

  PUBLIC_PATHS / PUBLIC_PREFIXES  → pass through untouched.
  Authenticated request           → pass through untouched.
  Unauthenticated HTML navigation → 303 redirect to /login?next=<path>.
  Unauthenticated API / XHR       → 401 JSON {detail, login_url}.

This complements (and can replace) the older `_redirect_html_to_login`
middleware in main.py — the difference is that this one ALSO covers API
routes, WebSocket handshakes, and any route not explicitly whitelisted.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from core.auth import is_authenticated
from core.logger import get_logger

logger = get_logger(__file__)


# ---------------------------------------------------------------------------
# Whitelist — anything reachable WITHOUT a session.
# ---------------------------------------------------------------------------
PUBLIC_EXACT = {
    "/login",
    "/logout",
    "/auth/whoami",
    "/favicon.ico",
    "/robots.txt",
}

PUBLIC_PREFIXES = (
    "/static/",
    "/assets/",
    "/_health",
    "/docs",           # FastAPI Swagger UI (drop these two if you want docs behind auth)
    "/openapi.json",
    "/redoc",
)


def _is_public(path: str) -> bool:
    if path in PUBLIC_EXACT:
        return True
    for prefix in PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def _client_wants_html(request: Request) -> bool:
    """Best-effort detection: is this a browser navigation (HTML)?"""
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept:
        return False
    if request.headers.get("x-requested-with", "").lower() == "xmlhttprequest":
        return False
    # Browsers send text/html when navigating; APIs rarely do.
    return "text/html" in accept


def _safe_next(path: str, query: str) -> str:
    """Preserve the original destination, sanitised to a same-origin path."""
    target = path + (("?" + query) if query else "")
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


class AuthGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # 1. Public paths always pass through.
        if _is_public(path):
            return await call_next(request)

        # 2. Everything else requires a valid session cookie (or Basic auth).
        if is_authenticated(request):
            return await call_next(request)

        client = request.client.host if request.client else "?"

        # 3. Browser navigation → 303 redirect to login, preserving ?next=
        if _client_wants_html(request):
            nxt = _safe_next(path, request.url.query or "")
            logger.info(
                "[auth-guard] redirect unauth HTML request -> /login — "
                "path=%s client=%s", path, client,
            )
            return RedirectResponse(
                url=f"/login?next={nxt}",
                status_code=303,
                headers={"Cache-Control": "no-store"},
            )

        # 4. API / XHR / WebSocket handshake → 401 JSON
        logger.warning(
            "[auth-guard] 401 unauth API request — path=%s client=%s",
            path, client,
        )
        return JSONResponse(
            status_code=401,
            content={
                "detail": "Authentication required",
                "login_url": "/login",
            },
            headers={"Cache-Control": "no-store"},
        )