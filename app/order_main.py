"""Standalone entry point for the order-management API."""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.core.logger import configure_logging
from app.db.mongo import connect_mongo, close_mongo
from app.api.order_routes import router as orders_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    connect_mongo()
    yield
    close_mongo()

app = FastAPI(title="Upstox Order Management API", version="1.0.0", lifespan=lifespan)
app.include_router(orders_router)

@app.get("/health")
def health():
    return {"status": "ok", "service": "upstox-order-management"}

if __name__ == "__main__":
    import uvicorn
    from app.core.config import settings
    uvicorn.run("app.order_main:app", host=settings.APP_HOST, port=settings.APP_PORT, reload=False)
