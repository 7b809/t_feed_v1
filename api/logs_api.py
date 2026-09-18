from pathlib import Path

import io
import zipfile

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["Logs"])


PROJECT_ROOT = Path(__file__).resolve().parent.parent

LOGS_DIR = PROJECT_ROOT / "logs"
META_DATA_DIR = PROJECT_ROOT / "meta_data"
TEMPLATES_DIR = PROJECT_ROOT / "templates"


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


ALLOWED_LOG_EXTENSIONS = {
    ".log",
    ".txt",
}

ALLOWED_METADATA_EXTENSIONS = {
    ".zip",
    ".log",
    ".txt",
    ".json",
    ".csv",
}


# ============================================================
# LOG FILE FUNCTIONS
# ============================================================


def get_available_log_files() -> list:
    LOGS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    files = []

    for file_path in sorted(
        LOGS_DIR.iterdir(),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ):
        if not file_path.is_file():
            continue

        if file_path.suffix.lower() not in ALLOWED_LOG_EXTENSIONS:
            continue

        stat = file_path.stat()

        files.append(
            {
                "filename": file_path.name,
                "size_bytes": stat.st_size,
                "modified_time": stat.st_mtime,
            }
        )

    return files


def validate_log_file(filename: str) -> Path:
    if not filename or filename in {".", ".."}:
        raise HTTPException(
            status_code=400,
            detail="Invalid log filename",
        )

    requested_path = Path(filename)

    if requested_path.name != filename:
        raise HTTPException(
            status_code=400,
            detail="Invalid log filename",
        )

    if requested_path.suffix.lower() not in ALLOWED_LOG_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported log file type",
        )

    LOGS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    logs_directory = LOGS_DIR.resolve()
    log_file = (LOGS_DIR / filename).resolve()

    try:
        log_file.relative_to(logs_directory)

    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid log file path",
        )

    if not log_file.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Log file not found: {filename}",
        )

    if not log_file.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"Not a log file: {filename}",
        )

    return log_file


def read_log_file(log_file: Path) -> list:
    with log_file.open(
        mode="r",
        encoding="utf-8",
        errors="ignore",
    ) as file:
        return file.readlines()


# ============================================================
# METADATA FILE FUNCTIONS
# ============================================================


def get_available_metadata_files() -> list:
    """
    Return all supported files available inside the meta_data folder.
    """
    META_DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    files = []

    for file_path in sorted(
        META_DATA_DIR.iterdir(),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ):
        if not file_path.is_file():
            continue

        if file_path.suffix.lower() not in ALLOWED_METADATA_EXTENSIONS:
            continue

        stat = file_path.stat()

        files.append(
            {
                "filename": file_path.name,
                "extension": file_path.suffix.lower(),
                "size_bytes": stat.st_size,
                "modified_time": stat.st_mtime,
            }
        )

    return files


def validate_metadata_file(filename: str) -> Path:
    """
    Validate a metadata filename and prevent path traversal.
    """
    if not filename or filename in {".", ".."}:
        raise HTTPException(
            status_code=400,
            detail="Invalid metadata filename",
        )

    requested_path = Path(filename)

    if requested_path.name != filename:
        raise HTTPException(
            status_code=400,
            detail="Invalid metadata filename",
        )

    if requested_path.suffix.lower() not in ALLOWED_METADATA_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported metadata file type",
        )

    META_DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    metadata_directory = META_DATA_DIR.resolve()

    metadata_file = (META_DATA_DIR / filename).resolve()

    try:
        metadata_file.relative_to(metadata_directory)

    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid metadata file path",
        )

    if not metadata_file.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Metadata file not found: {filename}",
        )

    if not metadata_file.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"Not a metadata file: {filename}",
        )

    return metadata_file


# ============================================================
# LOG HTML ROUTE
# ============================================================


@router.get(
    "/logs",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def show_logs(request: Request):
    log_files = get_available_log_files()

    return templates.TemplateResponse(
        request=request,
        name="show_logs.html",
        context={
            "app_name": "T Feed",
            "logs": log_files,
        },
    )


# ============================================================
# LOG CONTENT ROUTE
# ============================================================


@router.get(
    "/ui/logs/{filename}",
    response_class=PlainTextResponse,
    include_in_schema=False,
)
def show_log_file_content(filename: str):
    try:
        log_file = validate_log_file(filename)

        with log_file.open(
            mode="r",
            encoding="utf-8",
            errors="ignore",
        ) as file:
            return PlainTextResponse(
                content=file.read(),
                media_type="text/plain; charset=utf-8",
            )

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read log file: {exc}",
        ) from exc


# ============================================================
# LIST LOG FILES API
# ============================================================


@router.get("/api/logs")
def list_log_files():
    try:
        files = get_available_log_files()

        return {
            "success": True,
            "logs_directory": str(LOGS_DIR),
            "count": len(files),
            "files": files,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list log files: {exc}",
        ) from exc


# ============================================================
# GET LOG FILE API
# ============================================================


@router.get("/api/logs/{filename}")
def get_log_file(
    filename: str,
    lines: int = Query(
        default=200,
        ge=1,
        le=5000,
        description="Number of latest log lines to return",
    ),
):
    try:
        log_file = validate_log_file(filename)

        all_lines = read_log_file(log_file)

        selected_lines = all_lines[-lines:]

        return {
            "success": True,
            "filename": filename,
            "path": str(log_file),
            "total_lines": len(all_lines),
            "returned_lines": len(selected_lines),
            "first_returned_line": (
                len(all_lines) - len(selected_lines) + 1 if selected_lines else 0
            ),
            "logs": selected_lines,
        }

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read log file: {exc}",
        ) from exc


# ============================================================
# DOWNLOAD ALL LOGS AS ZIP
# ============================================================


@router.get(
    "/api/logs/download",
    include_in_schema=False,
)
def download_logs():
    try:
        if not LOGS_DIR.exists() or not LOGS_DIR.is_dir():
            raise HTTPException(
                status_code=404,
                detail="Logs folder not found",
            )

        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(
            zip_buffer,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
        ) as zip_file:

            for file_path in LOGS_DIR.rglob("*"):
                if file_path.is_file():
                    arcname = file_path.relative_to(LOGS_DIR)

                    zip_file.write(
                        file_path,
                        arcname=str(arcname),
                    )

        zip_buffer.seek(0)

        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": ('attachment; filename="logs.zip"')},
        )

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create logs ZIP: {exc}",
        ) from exc


# ============================================================
# METADATA HTML ROUTE
# ============================================================


@router.get(
    "/ui/meta-data",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def show_metadata(request: Request):
    metadata_files = get_available_metadata_files()

    return templates.TemplateResponse(
        request=request,
        name="show_metadata.html",
        context={
            "app_name": "T Feed",
            "metadata_files": metadata_files,
        },
    )


# ============================================================
# LIST ALL METADATA FILES API
# ============================================================


@router.get("/api/meta-data")
def list_metadata_files():
    """
    Return all available files inside the meta_data folder.
    """
    try:
        files = get_available_metadata_files()

        return {
            "success": True,
            "metadata_directory": str(META_DATA_DIR),
            "count": len(files),
            "files": files,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(f"Failed to list metadata files: {exc}"),
        ) from exc


# ============================================================
# GET METADATA FILE INFORMATION
# ============================================================


@router.get("/api/meta-data/{filename}")
def get_metadata_file_info(filename: str):
    """
    Return information about a selected metadata file.
    """
    try:
        metadata_file = validate_metadata_file(filename)

        stat = metadata_file.stat()

        return {
            "success": True,
            "filename": metadata_file.name,
            "path": str(metadata_file),
            "extension": metadata_file.suffix.lower(),
            "size_bytes": stat.st_size,
            "modified_time": stat.st_mtime,
        }

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(f"Failed to get metadata file information: {exc}"),
        ) from exc


# ============================================================
# DOWNLOAD SELECTED METADATA FILE
# ============================================================


@router.get(
    "/api/meta-data/download/{filename}",
)
def download_metadata_file(filename: str):
    """
    Download a selected metadata file.
    """
    try:
        metadata_file = validate_metadata_file(filename)

        return FileResponse(
            path=str(metadata_file),
            filename=metadata_file.name,
            media_type="application/octet-stream",
        )

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(f"Failed to download metadata file: {exc}"),
        ) from exc
