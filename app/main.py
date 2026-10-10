from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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