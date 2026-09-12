from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from core.config import settings


# ------------------------------------------------------------------
# Paths
# ------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]

TEMPLATES_DIR = PROJECT_ROOT / "templates"
LOGS_DIR = PROJECT_ROOT / "logs"

TEMPLATES_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)


# ------------------------------------------------------------------
# Router & templates
# ------------------------------------------------------------------

router = APIRouter(
    prefix="/ui",
    tags=["UI"],
)

templates = Jinja2Templates(
    directory=str(TEMPLATES_DIR)
)


# ------------------------------------------------------------------
# UI Home
# ------------------------------------------------------------------

@router.get(
    "/",
    response_class=HTMLResponse,
)
async def index(request: Request):
    """Render the main UI page."""

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
        },
    )


# ------------------------------------------------------------------
# Logs
# ------------------------------------------------------------------

@router.get(
    "/logs",
    response_class=HTMLResponse,
)
async def show_logs(request: Request):
    """Render the page containing the available log files."""

    log_files = []

    if LOGS_DIR.exists():
        log_files = [
            file.name
            for file in LOGS_DIR.iterdir()
            if file.is_file()
        ]

        log_files.sort(
            key=lambda filename: (
                LOGS_DIR / filename
            ).stat().st_mtime,
            reverse=True,
        )

    return templates.TemplateResponse(
        "show_logs.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "logs": log_files,
        },
    )


# ------------------------------------------------------------------
# View single log
# ------------------------------------------------------------------

@router.get(
    "/logs/{filename}",
    response_class=PlainTextResponse,
)
async def get_log_content(filename: str):
    """Return the content of a specific log file."""

    # Prevent path traversal.
    if (
        ".." in filename
        or "/" in filename
        or "\\" in filename
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid filename",
        )

    file_path = LOGS_DIR / filename

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Log file not found",
        )

    try:
        with open(
            file_path,
            "r",
            encoding="utf-8",
        ) as file:
            return file.read()

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Error reading file: {exc}",
        )


# ------------------------------------------------------------------
# Download single log file
# ------------------------------------------------------------------

@router.get(
    "/logs/download/{filename}",
    response_class=FileResponse,
)
async def download_log_file(filename: str):
    """
    Download a single log file.

    Example:
        /ui/logs/download/app.log
    """

    # Prevent path traversal.
    if (
        ".." in filename
        or "/" in filename
        or "\\" in filename
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid filename",
        )

    file_path = LOGS_DIR / filename

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Log file not found",
        )

    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="application/octet-stream",
    )


# ------------------------------------------------------------------
# Download all logs as ZIP
# ------------------------------------------------------------------

@router.get(
    "/logs/download-all",
    response_class=FileResponse,
)
async def download_all_logs():
    """
    Create a ZIP containing all log files and return it
    as a downloadable file.
    """

    if not LOGS_DIR.exists():
        raise HTTPException(
            status_code=404,
            detail="Logs directory not found",
        )

    log_files = [
        file
        for file in LOGS_DIR.rglob("*")
        if file.is_file()
    ]

    if not log_files:
        raise HTTPException(
            status_code=404,
            detail="No log files available",
        )

    # --------------------------------------------------------------
    # Create temporary ZIP.
    # --------------------------------------------------------------

    try:
        temp_file = NamedTemporaryFile(
            prefix="logs_",
            suffix=".zip",
            delete=False,
        )

        zip_path = Path(temp_file.name)

        temp_file.close()

        with ZipFile(
            zip_path,
            mode="w",
            compression=ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:

            for file_path in log_files:
                try:
                    relative_path = file_path.relative_to(
                        LOGS_DIR
                    )
                except ValueError:
                    relative_path = file_path.name

                archive.write(
                    file_path,
                    arcname=str(relative_path),
                )

        return FileResponse(
            path=str(zip_path),
            filename="logs.zip",
            media_type="application/zip",
            background=None,
        )

    except Exception as exc:
        # Clean up partially-created ZIP if something failed.
        try:
            if "zip_path" in locals() and zip_path.exists():
                zip_path.unlink()
        except Exception:
            pass

        raise HTTPException(
            status_code=500,
            detail=f"Error creating logs ZIP: {exc}",
        )
