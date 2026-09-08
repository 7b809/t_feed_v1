import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from core.config import settings  # adjust if your settings location differs

# Determine the base directory (project root)
BASE_DIR = Path(__file__).resolve().parent.parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
LOGS_DIR = BASE_DIR / "logs"  # adjust if logs are stored elsewhere

# Ensure logs directory exists
LOGS_DIR.mkdir(exist_ok=True)

router = APIRouter(prefix="/ui", tags=["UI"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Welcome page."""
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "app_name": settings.app_name}
    )


@router.get("/logs", response_class=HTMLResponse)
async def show_logs(request: Request):
    """Page listing all log files."""
    # Get list of .log files (or all files) from LOGS_DIR
    log_files = []
    if LOGS_DIR.exists():
        log_files = [f.name for f in LOGS_DIR.iterdir() if f.is_file()]
        # optionally sort by modification time (newest first)
        log_files.sort(key=lambda f: (LOGS_DIR / f).stat().st_mtime, reverse=True)

    return templates.TemplateResponse(
        "show_logs.html",
        {"request": request, "app_name": settings.app_name, "logs": log_files}
    )


@router.get("/logs/{filename}", response_class=PlainTextResponse)
async def get_log_content(filename: str):
    """Return the content of a specific log file as plain text."""
    # Security: prevent path traversal
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