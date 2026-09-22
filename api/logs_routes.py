"""
api/logs_routes.py

Endpoints for browsing, viewing, downloading and managing log files.

Routes
------
GET    /api/logs                         -> list all log files
GET    /api/logs/info                    -> folder info (size, count, latest)
GET    /api/logs/{filename}              -> view a specific log file
GET    /api/logs/download?filename=...   -> download a specific log file
GET    /api/logs/download-all            -> download all logs as ZIP
DELETE /api/logs/{filename}              -> delete a specific log file (guarded)
"""

from __future__ import annotations

import asyncio
import inspect
import os
import zipfile
from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

DEBUG_MODE = False

router = APIRouter(prefix="/api/logs", tags=["logs"])

# ---------------------------------------------------------------------
# Config / paths
# ---------------------------------------------------------------------
LOG_DIR: Path = Path(getattr(config, "LOG_DIR", "logs")).resolve()
ALLOWED_EXTENSIONS = {".log", ".txt", ".jsonl", ".out", ".err"}
MAX_VIEW_BYTES = 5 * 1024 * 1024  # 5 MB max for inline view


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _ensure_log_dir() -> None:
    if not LOG_DIR.exists() or not LOG_DIR.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Log directory not found: {LOG_DIR}",
        )


def _safe_path(filename: str) -> Path:
    """Prevent path traversal and restrict to allowed extensions."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename.",
        )

    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Extension '{ext}' not allowed. Allowed: {sorted(ALLOWED_EXTENSIONS)}",
        )

    full = (LOG_DIR / filename).resolve()
    if LOG_DIR not in full.parents and full != LOG_DIR:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path traversal detected.",
        )
    if not full.exists() or not full.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Log file not found: {filename}",
        )
    return full


def _file_meta(p: Path) -> dict[str, Any]:
    st = p.stat()
    return {
        "filename": p.name,
        "size_bytes": st.st_size,
        "size_kb": round(st.st_size / 1024, 2),
        "modified_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
    }


def _list_log_files() -> list[dict[str, Any]]:
    _ensure_log_dir()
    files: list[dict[str, Any]] = []
    for p in sorted(LOG_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_file() and p.suffix.lower() in ALLOWED_EXTENSIONS:
            files.append(_file_meta(p))
    return files


def _build_zip_bytes(files: list[Path]) -> BytesIO:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, arcname=f.name)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------
class LogFileMeta(BaseModel):
    filename: str
    size_bytes: int
    size_kb: float
    modified_at: str


class LogListResponse(BaseModel):
    count: int
    log_dir: str
    files: list[LogFileMeta]


class LogInfoResponse(BaseModel):
    log_dir: str
    exists: bool
    total_files: int
    total_size_bytes: int
    total_size_mb: float
    latest_file: str | None = None
    oldest_file: str | None = None
    allowed_extensions: list[str]


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@router.get(
    "",
    response_model=LogListResponse,
    summary="List all available log files",
)
async def list_logs() -> LogListResponse:
    """Return metadata for every log file in the logs directory."""
    try:
        files = _list_log_files()
        return LogListResponse(
            count=len(files),
            log_dir=str(LOG_DIR),
            files=[LogFileMeta(**f) for f in files],
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to list log files: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list log files: {exc}",
        )


@router.get(
    "/info",
    response_model=LogInfoResponse,
    summary="Get log folder info",
)
async def log_info() -> LogInfoResponse:
    """Return overall info about the logs folder."""
    exists = LOG_DIR.exists() and LOG_DIR.is_dir()
    if not exists:
        return LogInfoResponse(
            log_dir=str(LOG_DIR),
            exists=False,
            total_files=0,
            total_size_bytes=0,
            total_size_mb=0.0,
            allowed_extensions=sorted(ALLOWED_EXTENSIONS),
        )

    files = _list_log_files()
    total_bytes = sum(f["size_bytes"] for f in files)
    latest = files[0]["filename"] if files else None
    oldest = files[-1]["filename"] if files else None

    return LogInfoResponse(
        log_dir=str(LOG_DIR),
        exists=True,
        total_files=len(files),
        total_size_bytes=total_bytes,
        total_size_mb=round(total_bytes / (1024 * 1024), 3),
        latest_file=latest,
        oldest_file=oldest,
        allowed_extensions=sorted(ALLOWED_EXTENSIONS),
    )


@router.get(
    "/download",
    summary="Download a single log file",
    response_class=FileResponse,
)
async def download_log(
    filename: str = Query(..., description="Log file name (e.g. app.log)"),
):
    """Download the specified log file as an attachment."""
    path = _safe_path(filename)
    return FileResponse(
        path=str(path),
        media_type="application/octet-stream",
        filename=path.name,
    )


@router.get(
    "/download-all",
    summary="Download all log files as a ZIP",
)
async def download_all_logs():
    """Bundle every log file into a single ZIP archive."""
    files = [LOG_DIR / f["filename"] for f in _list_log_files()]
    if not files:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No log files available to download.",
        )

    buf = _build_zip_bytes(files)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    zip_name = f"logs_{ts}.zip"

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_name}"',
        },
    )


@router.delete(
    "/{filename}",
    summary="Delete a specific log file (guarded)",
)
async def delete_log(
    filename: str,
    confirm: bool = Query(
        False,
        description="Set to true to confirm deletion.",
    ),
):
    """Delete a single log file. Requires confirm=true."""
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Deletion requires confirm=true query param.",
        )

    path = _safe_path(filename)
    try:
        path.unlink()
        logger.info("Deleted log file: %s", path.name)
        return {"deleted": True, "filename": path.name}
    except Exception as exc:
        logger.exception("Failed to delete log file %s: %s", path, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete log file: {exc}",
        )


@router.get(
    "/{filename}",
    summary="View a specific log file (last N lines)",
)
async def view_log(
    filename: str,
    lines: int = Query(
        500,
        ge=1,
        le=10000,
        description="Number of trailing lines to return.",
    ),
    raw: bool = Query(
        False,
        description="If true, return the raw file content.",
    ),
):
    """
    View a specific log file.

    - Default: returns the last `lines` lines.
    - `raw=true`: returns full file content (capped at MAX_VIEW_BYTES).
    """
    path = _safe_path(filename)

    try:
        size = path.stat().st_size

        if raw:
            if size > MAX_VIEW_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=(
                        f"File too large to view inline "
                        f"({size} bytes > {MAX_VIEW_BYTES}). "
                        "Use /download instead."
                    ),
                )
            content = path.read_text(encoding="utf-8", errors="replace")
            return {
                "filename": path.name,
                "size_bytes": size,
                "mode": "raw",
                "content": content,
            }

        # tail mode
        tail_lines: list[str] = []
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                tail_lines.append(line)
                if len(tail_lines) > lines:
                    tail_lines.pop(0)

        return {
            "filename": path.name,
            "size_bytes": size,
            "mode": "tail",
            "lines_returned": len(tail_lines),
            "content": "".join(tail_lines),
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to read log file %s: %s", path, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read log file: {exc}",
        )
