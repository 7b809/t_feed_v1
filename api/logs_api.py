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
TEXT_LOGS_DIR = PROJECT_ROOT / "logs_text"
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
# LOG SOURCE HELPERS
# ============================================================


def resolve_log_source(source: str) -> str:
    """
    Normalizes a user-supplied source into one of:

        "logs"       -> rotating .log files
        "logs_text"  -> plain-text .txt files
        "all"        -> both directories merged
    """
    normalized = str(source or "all").strip().lower()

    if normalized in {"logs_text", "text", "txt"}:
        return "logs_text"

    if normalized in {"logs", "log"}:
        return "logs"

    return "all"


def get_source_directories(source: str) -> list[tuple[str, Path]]:
    """
    Returns a list of (source_name, directory) tuples for the given
    resolved source value.
    """
    resolved_source = resolve_log_source(source)

    if resolved_source == "logs":
        return [("logs", LOGS_DIR)]

    if resolved_source == "logs_text":
        return [("logs_text", TEXT_LOGS_DIR)]

    return [
        ("logs", LOGS_DIR),
        ("logs_text", TEXT_LOGS_DIR),
    ]


def get_directory_for_source(source: str) -> tuple[str, Path]:
    """
    Returns a single (source_name, directory) tuple for the given
    source. Falls back to "logs" when the source is unknown or "all".
    """
    resolved_source = resolve_log_source(source)

    if resolved_source == "logs_text":
        return ("logs_text", TEXT_LOGS_DIR)

    return ("logs", LOGS_DIR)


# ============================================================
# LOG FILE FUNCTIONS
# ============================================================


def list_files_in_directory(
    directory: Path,
    source_name: str,
) -> list:
    """
    Lists all supported log files inside a single directory.
    """
    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    files = []

    for file_path in sorted(
        directory.iterdir(),
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
                "source": source_name,
                "extension": file_path.suffix.lower(),
                "size_bytes": stat.st_size,
                "modified_time": stat.st_mtime,
            }
        )

    return files


def get_available_log_files(source: str = "all") -> list:
    """
    Lists all supported log files for the given source:

        "logs"       -> only logs/
        "logs_text"  -> only logs_text/
        "all"        -> both, merged and sorted by modified_time desc
    """
    resolved_source = resolve_log_source(source)

    if resolved_source in {"logs", "logs_text"}:
        source_name, directory = get_directory_for_source(resolved_source)

        return list_files_in_directory(directory, source_name)

    merged: list = []

    for source_name, directory in get_source_directories("all"):
        merged.extend(list_files_in_directory(directory, source_name))

    merged.sort(
        key=lambda entry: entry.get("modified_time", 0),
        reverse=True,
    )

    return merged


def validate_log_file(
    filename: str,
    source: str = "logs",
) -> Path:
    """
    Validates a log filename for the given source and returns a
    resolved Path. Prevents path traversal.
    """
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

    _, logs_dir = get_directory_for_source(source)

    logs_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    logs_directory = logs_dir.resolve()
    log_file = (logs_dir / filename).resolve()

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
async def show_logs(
    request: Request,
    source: str = Query(
        default="all",
        description="Which log source to list: 'logs', 'logs_text' or 'all'",
    ),
):
    resolved_source = resolve_log_source(source)

    log_files = get_available_log_files(source=resolved_source)

    return templates.TemplateResponse(
        request=request,
        name="show_logs.html",
        context={
            "app_name": "T Feed",
            "logs": log_files,
            "log_source": resolved_source,
            "logs_directory": str(LOGS_DIR),
            "text_logs_directory": str(TEXT_LOGS_DIR),
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
def show_log_file_content(
    filename: str,
    source: str = Query(
        default="logs",
        description="Which log source to read: 'logs' or 'logs_text'",
    ),
):
    try:
        log_file = validate_log_file(filename, source=source)

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
def list_log_files(
    source: str = Query(
        default="all",
        description="Which log source to list: 'logs', 'logs_text' or 'all'",
    ),
):
    try:
        resolved_source = resolve_log_source(source)

        files = get_available_log_files(source=resolved_source)

        return {
            "success": True,
            "source": resolved_source,
            "logs_directory": str(LOGS_DIR),
            "text_logs_directory": str(TEXT_LOGS_DIR),
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
    source: str = Query(
        default="logs",
        description="Which log source to read: 'logs' or 'logs_text'",
    ),
):
    try:
        resolved_source = resolve_log_source(source)

        if resolved_source == "all":
            resolved_source = "logs"

        log_file = validate_log_file(filename, source=resolved_source)

        all_lines = read_log_file(log_file)

        selected_lines = all_lines[-lines:]

        return {
            "success": True,
            "filename": filename,
            "source": resolved_source,
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
def download_logs(
    source: str = Query(
        default="all",
        description="Which log source to zip: 'logs', 'logs_text' or 'all'",
    ),
):
    try:
        resolved_source = resolve_log_source(source)

        directories = get_source_directories(resolved_source)

        any_content = False

        for _, directory in directories:
            if not directory.exists() or not directory.is_dir():
                continue

            if any(directory.rglob("*")):
                any_content = True
                break

        if not any_content:
            raise HTTPException(
                status_code=404,
                detail="No log files found to download",
            )

        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(
            zip_buffer,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
        ) as zip_file:

            for source_name, directory in directories:
                if not directory.exists() or not directory.is_dir():
                    continue

                for file_path in directory.rglob("*"):
                    if not file_path.is_file():
                        continue

                    relative = file_path.relative_to(directory)

                    arcname = Path(source_name) / relative

                    zip_file.write(
                        file_path,
                        arcname=str(arcname),
                    )

        zip_buffer.seek(0)

        if resolved_source == "all":
            archive_name = "logs_all.zip"
        else:
            archive_name = f"{resolved_source}.zip"

        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{archive_name}"'
            },
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