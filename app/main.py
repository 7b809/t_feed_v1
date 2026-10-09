from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.candle_routes import router as candle_router
from app.api.routes import router
from app.core.lifespan import lifespan
from app.core.logger import configure_logging

configure_logging()

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="Ordered Instrument Jobs",
    version="1.2.0",
    lifespan=lifespan,
)

app.include_router(router)
app.include_router(candle_router)

app.mount(
    "/static",
    StaticFiles(directory=STATIC_DIR),
    name="static",
)

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")