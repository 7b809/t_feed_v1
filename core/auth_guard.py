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

Incoming isolation notifications
--------------------------------
`/api/isolation` (and everything under it) is whitelisted so the
isolation service can POST its document without a browser session.
Auth on that endpoint is enforced *inside the route handler* by
`routes/isolation_receiver_routes.py`, which validates the shared
secret header (`X-Isolation-Key` by default) against
`ISOLATION_RECEIVER_KEY`. Whitelisting here does NOT make the endpoint
public — it only lets the request reach the handler that will perform
the actual auth check.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from core.auth import is_authenticated
from core.user_accounts import get_user_session
from core.logger import get_logger

logger = get_logger(__file__)


# ---------------------------------------------------------------------------
# Whitelist — anything reachable WITHOUT a session.
# ---------------------------------------------------------------------------
PUBLIC_EXACT = {
    "/login",
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
    "/docs",           # FastAPI Swagger UI (drop these two if you want docs behind auth)
    "/openapi.json",
    "/redoc",
    # Incoming isolation notifications from the isolation service.
    # Covers POST /api/isolation, GET /api/isolation/received,
    # GET /api/isolation/received/{date}/{filename}.
    # Route-level auth is enforced by the shared-secret header check in
    # routes/isolation_receiver_routes.py — do NOT rely on this entry
    # alone as the endpoint's only protection.
    "/api/isolation",
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

        # Member sessions receive read-only market access only.
        member = get_user_session(request)
        if member:
            allowed_exact = {"/", "/charts", "/user/me", "/live/status",
                             "/live/instruments", "/ws/live", "/ws/chart"}
            # The V4 live router is mounted without an /api prefix.
            # Restrict members to read-only /live endpoints and chart pages.
            allowed_prefixes = ("/chart/", "/live/")
            if path in allowed_exact or any(path.startswith(p) for p in allowed_prefixes):
                request.state.member = member
                return await call_next(request)
            return JSONResponse(status_code=403,
                content={"detail": "This area is available to administrators only."},
                headers={"Cache-Control": "no-store"})

        # Existing administrator/API-key/Basic authentication remains unchanged.
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