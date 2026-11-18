"""FastAPI entry point for sandbox_v1 — Upstox order execution service."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from api.routes.orders import router as orders_router
from core.config import API_PORT, API_HOST, UPSTOX_SANDBOX, TRADING_ENABLED, PROJECT_ROOT
from core.logger import get_logger
from core.mongo_client import close_mongo_connection, get_mongo_db
from core.telegram_notifier import notify_event

logger = get_logger(__file__)
TEMPLATES = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Starting Upstox Order Service | sandbox=%s | trading_enabled=%s | bind=%s:%s",
        UPSTOX_SANDBOX, TRADING_ENABLED, API_HOST, API_PORT,
    )
    get_mongo_db()
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
        close_mongo_connection()


app = FastAPI(
    title="Upstox Order Service",
    description="Responsive order dashboard and audited Upstox order execution API.",
    version="1.1.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")
app.include_router(orders_router)


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


@app.get("/health", tags=["system"])
def health():
    return {
        "status": "ok", "service": "sandbox_v1", "port": API_PORT,
        "upstox_sandbox": UPSTOX_SANDBOX, "trading_enabled": TRADING_ENABLED,
        "database": "UPSTOX_APP_V2", "orders_collection": "sandbox_orders",
    }


@app.get("/docs-info", tags=["system"])
def root_info():
    return {"service": "Upstox Order Service", "docs": "/docs", "health": "/health"}
