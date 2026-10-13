from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.candle_routes import router as candle_router
from app.api.live_ema_ws_routes import router as live_ema_ws_router
from app.api.logs_routes import router as logs_router
from app.api.routes import router
from app.core.config import settings
from app.core.lifespan import lifespan
from app.core.logger import configure_logging

configure_logging()

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="Ordered Instrument Jobs",
    version="1.5.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Startup gate
# ---------------------------------------------------------------------------
# Until ``lifespan`` finishes jobs 1–5 and sets ``app.state.startup_complete``
# to True, every incoming HTTP request is answered with a 503 "still loading"
# payload. This is a safety net: with a plain uvicorn server the socket is
# not bound until the lifespan startup completes, so under normal operation
# clients never see this. It *does* matter when:
#
#   * the app is fronted by a reverse proxy (nginx, Caddy, Cloudflare) that
#     accepts connections before the backend is ready, or
#   * an operator is re-running startup on a hot-reload worker, or
#   * a load balancer runs health checks while the process is booting.
#
# Endpoints that must always answer — health checks, docs, the status probe,
# and static assets — are whitelisted so monitoring and dashboards work
# during the boot window.
# ---------------------------------------------------------------------------
STARTUP_WHITELIST: set[str] = {
    "/api/health",
    "/api/startup-status",
    "/docs",
    "/docs/oauth2-redirect",
    "/openapi.json",
    "/redoc",
}

# Prefixes that bypass the gate (static assets, favicon, etc.).
STARTUP_WHITELIST_PREFIXES: tuple[str, ...] = (
    "/static",
    "/favicon",
)

# Message returned while the app is still booting.
STARTUP_LOADING_MESSAGE = (
    "Project is still loading. Jobs are being initialised "
    "(subscriptions, historical candles, intraday candles, EMA crosses, "
    "live EMA). Please retry in a moment."
)


class StartupGateMiddleware(BaseHTTPMiddleware):
    """Return HTTP 503 until ``app.state.startup_complete`` is True."""

    async def dispatch(self, request: Request, call_next):
        # Fast path: startup finished — serve normally.
        if getattr(request.app.state, "startup_complete", False):
            return await call_next(request)

        path = request.url.path

        # Always allow the whitelist through (health, docs, static, probe).
        if path in STARTUP_WHITELIST or path.startswith(
            STARTUP_WHITELIST_PREFIXES
        ):
            return await call_next(request)

        phase = getattr(request.app.state, "startup_phase", "initializing")
        started_at = getattr(request.app.state, "startup_started_at", None)

        return JSONResponse(
            status_code=503,
            content={
                "status": "loading",
                "message": STARTUP_LOADING_MESSAGE,
                "phase": phase,
                "started_at": started_at,
                "retry_after_seconds": 5,
            },
            headers={"Retry-After": "5"},
        )


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
# Middleware added later wraps middleware added earlier (Starlette inserts
# new entries at position 0). We therefore register the startup gate FIRST
# so that CORS — added SECOND — wraps it. That guarantees 503 responses
# still carry CORS headers and are readable from the browser.
# ---------------------------------------------------------------------------
app.add_middleware(StartupGateMiddleware)


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
# Allowed origins are read from the CORS_ALLOWED_ORIGINS environment variable
# (comma-separated). Defaults to "*" so local development and the bundled
# dashboard keep working out of the box.
#
# When allow_credentials=True, "*" is invalid per the CORS spec — Starlette
# silently falls back to echoing the request Origin. We therefore only enable
# credentials when explicit origins are configured.
# ---------------------------------------------------------------------------
_cors_origins_raw = settings.cors_allowed_origins.strip()

if _cors_origins_raw in ("", "*"):
    _cors_origins: list[str] = ["*"]
    _cors_allow_credentials = False
else:
    _cors_origins = [
        origin.strip()
        for origin in _cors_origins_raw.split(",")
        if origin.strip()
    ]
    _cors_allow_credentials = settings.cors_allow_credentials

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=600,
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(router)
app.include_router(candle_router)
app.include_router(logs_router)
app.include_router(live_ema_ws_router)

app.mount(
    "/static",
    StaticFiles(directory=STATIC_DIR),
    name="static",
)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/logs", include_in_schema=False)
async def logs_page():
    """Serve the logs dashboard page."""
    return FileResponse(STATIC_DIR / "logs.html")


# ---------------------------------------------------------------------------
# Startup status probe (always reachable, bypasses the gate)
# ---------------------------------------------------------------------------
@app.get("/api/startup-status", tags=["health"])
async def startup_status():
    """Report whether the app has finished booting.

    Always reachable — even while the StartupGateMiddleware is answering
    every other route with HTTP 503. Useful for probes, dashboards, and
    for the bundled web UI to show a "still loading" overlay.
    """
    return {
        "startup_complete": bool(
            getattr(app.state, "startup_complete", False)
        ),
        "phase": getattr(app.state, "startup_phase", "initializing"),
        "started_at": getattr(app.state, "startup_started_at", None),
        "completed_at": getattr(app.state, "startup_completed_at", None),
        "message": (
            "Ready"
            if getattr(app.state, "startup_complete", False)
            else STARTUP_LOADING_MESSAGE
        ),
    }