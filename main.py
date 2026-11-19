"""FastAPI entry point for sandbox_v1 — Upstox order execution service."""
from __future__ import annotations

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from api.routes.backups import router as backups_router
from api.routes.gtt import router as gtt_router
from api.routes.orders import router as orders_router
from core.config import (
    API_HOST, API_PORT, MONGO_DB_NAME, ORDERS_COLLECTION, PROJECT_ROOT,
    TRADING_ENABLED, UPSTOX_SANDBOX,
)
from core.logger import get_logger
from core.mongo_client import close_mongo_connection, get_mongo_db
from core.telegram_notifier import notify_event
from core.token_service import get_token_status
from services.mega_backup_scheduler import start_mega_backup_scheduler, stop_mega_backup_scheduler
from services.token_check_scheduler import start_token_check_scheduler, stop_token_check_scheduler

logger = get_logger(__file__)
TEMPLATES = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Starting Upstox Order Service | sandbox=%s | trading_enabled=%s | bind=%s:%s",
        UPSTOX_SANDBOX, TRADING_ENABLED, API_HOST, API_PORT,
    )
    get_mongo_db()
    start_mega_backup_scheduler()
    start_token_check_scheduler()
    notify_event("Sandbox service started", [
        f"Port: {API_PORT}",
        "Upstox mode: " + ("Sandbox" if UPSTOX_SANDBOX else "Live"),
        f"Trading enabled: {TRADING_ENABLED}",
    ], "STARTUP")
    try:
        yield
    finally:
        logger.info("Stopping Upstox Order Service")
        notify_event("Sandbox service stopped", [f"Port: {API_PORT}"], "WARNING")
        stop_token_check_scheduler()
        stop_mega_backup_scheduler()
        close_mongo_connection()


app = FastAPI(
    title="Upstox Order Service",
    description="Responsive order dashboard, BUY order and GTT execution API.",
    version="1.2.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")
app.include_router(orders_router)
app.include_router(backups_router)
app.include_router(gtt_router)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request):
    return TEMPLATES.TemplateResponse(
        request=request, name="dashboard.html",
        context={"request": request, "active_page": "dashboard"},
    )


@app.get("/buy", response_class=HTMLResponse, include_in_schema=False)
def buy_page(request: Request):
    return TEMPLATES.TemplateResponse(
        request=request, name="buy.html",
        context={"request": request, "active_page": "buy"},
    )


@app.get("/gtt", response_class=HTMLResponse, include_in_schema=False)
def gtt_page(request: Request):
    return TEMPLATES.TemplateResponse(
        request=request, name="gtt.html",
        context={"request": request, "active_page": "gtt"},
    )


@app.get("/health", tags=["system"])
def health():
    db_ok = True
    try:
        get_mongo_db().command("ping")
    except Exception:
        logger.exception("Health check: MongoDB ping failed")
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "service": "sandbox_v1",
        "port": API_PORT,
        "upstox_sandbox": UPSTOX_SANDBOX,
        "trading_enabled": TRADING_ENABLED,
        "database": MONGO_DB_NAME,
        "database_ok": db_ok,
        "orders_collection": ORDERS_COLLECTION,
    }


@app.get("/health/token", tags=["system"])
def health_token():
    status = get_token_status()
    return JSONResponse(status_code=200 if status.get("valid") else 503, content=status)


@app.get("/docs-info", tags=["system"])
def root_info():
    return {"service": "Upstox Order Service", "docs": "/docs", "health": "/health"}
