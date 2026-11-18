"""FastAPI entry point for sandbox_v1 — Upstox order execution service."""
from __future__ import annotations

from contextlib import asynccontextmanager
from fastapi import FastAPI

from api.routes.orders import router as orders_router
from core.config import API_PORT, API_HOST, UPSTOX_SANDBOX, TRADING_ENABLED
from core.logger import get_logger
from core.mongo_client import close_mongo_connection, get_mongo_db

logger = get_logger(__file__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Starting Upstox Order Service | sandbox=%s | trading_enabled=%s | bind=%s:%s",
        UPSTOX_SANDBOX, TRADING_ENABLED, API_HOST, API_PORT,
    )
    get_mongo_db()
    yield
    logger.info("Stopping Upstox Order Service")
    close_mongo_connection()


app = FastAPI(
    title="Upstox Order Service",
    description=(
        "Dedicated Upstox order placement, cancellation and square-off API. "
        "Order actions are audited in local daily JSON files and MongoDB."
    ),
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(orders_router)


@app.get("/health", tags=["system"])
def health():
    return {
        "status": "ok",
        "service": "sandbox_v1",
        "port": API_PORT,
        "upstox_sandbox": UPSTOX_SANDBOX,
        "trading_enabled": TRADING_ENABLED,
        "database": "UPSTOX_APP_V2",
        "orders_collection": "sandbox_orders",
    }


@app.get("/", tags=["system"])
def root():
    return {
        "service": "Upstox Order Service",
        "docs": "/docs",
        "health": "/health",
        "orders": "/api/v1/orders",
    }
