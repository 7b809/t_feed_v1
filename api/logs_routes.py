from __future__ import annotations

import asyncio
import inspect
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, List, Optional

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

DEBUG_MODE = False


# ------------------------------------------------------------------
# Router
# ------------------------------------------------------------------
router = APIRouter(
    prefix="/api/logs",
    tags=["logs"],
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _resolve_logs_dir() -> Path:
    """
    Resolve the logs directory from config (LOG_DIR) or default to ./logs.
    Always returns an absolute resolved path.
    """
    log_dir = getattr(config, "LOG_DIR", None) or "logs"
    path = Path(str(log_dir)).expanduser()
    if not path.is_absolute():
        # Resolve relative to the project root (parent of this file's package)
        project_root = Path(__file__).resolve().parent.parent
        path = (project_root / path).resolve()
    return path


def _safe_join(base: Path, filename: str) -> Path:
    """
    Safely join base dir + filename, preventing path traversal (../).
    Raises HTTPException(400) if the resolved path escapes base.
    """
    if not filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="filename is required",
        )

    # Reject any path separators or traversal segments
    candidate = (base / filename).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename (path traversal detected)",
        )
    return candidate


def _is_log_file(p: Path) -> bool:
    """Only expose regular files with common log extensions."""
    if not p.is_file():
        return False
    return (
        p.suffix.lower() in {".log", ".txt", ".jsonl", ".out", ".err"} or p.suffix == ""
    )


def _file_meta(p: Path) -> dict:
    st = p.stat()
    return {
        "filename": p.name,
        "size_bytes": st.st_size,
        "size_kb": round(st.st_size / 1024, 2),
        "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(),
    }


# ------------------------------------------------------------------
# Models
# ------------------------------------------------------------------
class LogFileInfo(BaseModel):
    filename: str = Field(..., description="Name of the log file")
    size_bytes: int = Field(..., description="File size in bytes")
    size_kb: float = Field(..., description="File size in KB")
    modified_at: str = Field(..., description="Last modified time (ISO 8601)")


class LogListResponse(BaseModel):
    logs_dir: str = Field(..., description="Absolute path of the logs directory")
    count: int = Field(..., description="Number of log files found")
    files: List[LogFileInfo] = Field(default_factory=list)


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------
@router.get(
    "",
    response_model=LogListResponse,
    summary="List all available log files",
    description="Returns metadata (name, size, modified time) of every log file in the logs/ directory.",
)
async def list_logs() -> LogListResponse:
    base = _resolve_logs_dir()

    if not base.exists() or not base.is_dir():
        logger.warning("Logs directory does not exist: %s", base)
        return LogListResponse(logs_dir=str(base), count=0, files=[])

    files: List[LogFileInfo] = []
    try:
        for entry in sorted(base.iterdir(), key=lambda x: x.name.lower()):
            if _is_log_file(entry):
                files.append(LogFileInfo(**_file_meta(entry)))
    except Exception as exc:
        logger.exception("Failed to enumerate log files: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list log files: {exc}",
        )

    return LogListResponse(
        logs_dir=str(base),
        count=len(files),
        files=files,
    )


@router.get(
    "/{filename}",
    summary="Fetch a specific log file",
    description=(
        "Returns the contents of a log file. "
        "Use `tail` to return only the last N lines. "
        "Use `download=true` to force a file download."
    ),
    responses={
        200: {"content": {"text/plain": {}}},
        404: {"description": "Log file not found"},
        400: {"description": "Invalid filename"},
    },
)
async def get_log_file(
    filename: str,
    tail: Optional[int] = Query(
        None,
        ge=1,
        le=100_000,
        description="Return only the last N lines (optional)",
    ),
    download: bool = Query(
        False,
        description="If true, forces a file download (Content-Disposition: attachment)",
    ),
):
    base = _resolve_logs_dir()

    if not base.exists() or not base.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Logs directory not found",
        )

    target = _safe_join(base, filename)

    if not target.exists() or not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Log file '{filename}' not found",
        )

    if not _is_log_file(target):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File '{filename}' is not a valid log file",
        )

    # Optional tail mode -> return PlainTextResponse
    if tail is not None:
        try:
            content = await asyncio.to_thread(_read_tail, target, tail)
        except Exception as exc:
            logger.exception("Failed to read tail of %s: %s", target, exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to read log file: {exc}",
            )
        return PlainTextResponse(content=content, media_type="text/plain")

    # Download mode -> FileResponse with attachment header
    if download:
        return FileResponse(
            path=str(target),
            media_type="application/octet-stream",
            filename=target.name,
        )

    # Default -> return whole file as text/plain
    try:
        content = await asyncio.to_thread(target.read_text, "utf-8", "replace")
    except Exception as exc:
        logger.exception("Failed to read %s: %s", target, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read log file: {exc}",
        )
    return PlainTextResponse(content=content, media_type="text/plain")


# ------------------------------------------------------------------
# Internal sync helpers (run in threadpool)
# ------------------------------------------------------------------
def _read_tail(path: Path, n: int) -> str:
    """
    Read last N lines from a file efficiently (no full file read for big files).
    """
    if n <= 0:
        return ""
    block_size = 8192
    with path.open("rb") as f:
        f.seek(0, 2)  # end
        file_size = f.tell()
        if file_size == 0:
            return ""

        buffer = b""
        newline_count = 0
        pos = file_size

        while pos > 0 and newline_count <= n:
            read_size = min(block_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size)
            buffer = chunk + buffer
            newline_count = buffer.count(b"\n")

        # Decode and split into lines
        text = buffer.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return "\n".join(lines[-n:]) + ("\n" if lines else "")
