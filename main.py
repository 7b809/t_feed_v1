from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from api.routes.health import router as health_router
from api.routes.order_requests import router as order_requests_router

from core.config import settings
from core.database import (
    close_mongo_connection,
    connect_to_mongo,
)
from core.logger import get_logger

from services.telegram_bot_service import (
    telegram_bot_service,
)
from services.token_service import (
    token_service,
)

logger = get_logger("app")

# ------------------------------------------------------------------
# Templates & logs directory setup
# ------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
LOGS_DIR = BASE_DIR / "logs"

TEMPLATES_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ------------------------------------------------------------------
# Lifespan (unchanged)
# ------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_: FastAPI):
    await connect_to_mongo()
    token_loaded = await token_service.start()
    if settings.upstox_token_required_on_startup and not token_loaded:
        logger.error("Application startup aborted. Upstox access token is unavailable.")
        await token_service.stop()
        await close_mongo_connection()
        raise RuntimeError("Upstox access token is unavailable.")
    logger.info(
        "%s started token_available=%s",
        settings.app_name,
        token_service.has_access_token(),
    )
    telegram_bot_service.start()
    telegram_bot_service.send_startup_message()
    try:
        yield
    finally:
        telegram_bot_service.stop()
        await token_service.stop()
        await close_mongo_connection()
        logger.info("%s stopped", settings.app_name)


# ------------------------------------------------------------------
# FastAPI app instance
# ------------------------------------------------------------------
app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=(
        "Receives isolated EMA alert payloads, "
        "selects instruments, executes orders, "
        "and stores request/execution data."
    ),
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(order_requests_router, prefix="/api/v1")


# ------------------------------------------------------------------
# UI routes – using manual rendering to avoid cache key issues
# ------------------------------------------------------------------
@app.get("/ui", response_class=HTMLResponse, tags=["UI"])
async def ui_index(request: Request):
    template = templates.get_template("index.html")
    content = template.render({"request": request, "app_name": settings.app_name})
    return HTMLResponse(content=content)


@app.get("/ui/logs", response_class=HTMLResponse, tags=["UI"])
async def show_logs(request: Request):
    log_files = []
    if LOGS_DIR.exists():
        log_files = [f.name for f in LOGS_DIR.iterdir() if f.is_file()]
        log_files.sort(key=lambda f: (LOGS_DIR / f).stat().st_mtime, reverse=True)
    template = templates.get_template("show_logs.html")
    content = template.render(
        {
            "request": request,
            "app_name": settings.app_name,
            "logs": log_files,
        }
    )
    return HTMLResponse(content=content)


@app.get("/ui/logs/{filename}", response_class=PlainTextResponse, tags=["UI"])
async def get_log_content(filename: str):
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    file_path = LOGS_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Log file not found")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        return content
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading file: {str(e)}")


# ------------------------------------------------------------------
# Root – manual rendering
# ------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, tags=["Root"])
async def root(request: Request):
    template = templates.get_template("index.html")
    content = template.render({"request": request, "app_name": settings.app_name})
    return HTMLResponse(content=content)
