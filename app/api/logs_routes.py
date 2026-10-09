"""Read-only API for the application's log directory.

Endpoints
---------
GET /api/logs
    List every file in the configured log directory with basic metadata.

GET /api/logs/{filename}
    Return the contents of a single log file. Supports:

    * ``tail`` — return only the last N lines (default: all lines).
    * ``lines`` — maximum number of lines to return from the tail.
    * ``grep`` — case-insensitive substring filter applied after tail.

The filename is validated against the actual directory listing so path
traversal (``..``, absolute paths, symlinks pointing outside the log
folder) is rejected before any read is attempted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/logs", tags=["logs"])

LOG_DIR: Path = settings.log_file.parent

# Cap on how much we will ever read from a single log file. 8 MB is
# generous for text logs and protects the server from a runaway file.
MAX_READ_BYTES = 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ensure_log_dir() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR


def _safe_log_path(filename: str) -> Path:
    """Resolve ``filename`` inside LOG_DIR, rejecting any escape attempts."""
    if not filename or filename.strip() != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    candidate = (LOG_DIR / filename).resolve()
    log_dir_resolved = LOG_DIR.resolve()

    try:
        candidate.relative_to(log_dir_resolved)
    except ValueError:
        # Attempted traversal outside the log directory.
        raise HTTPException(status_code=400, detail="Invalid filename")

    if not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"Log file not found: {filename}")

    return candidate


def _file_info(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.name,
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat(),
        "created_at": datetime.fromtimestamp(
            stat.st_ctime, tz=timezone.utc
        ).isoformat(),
    }


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
@router.get("")
async def list_logs() -> dict[str, Any]:
    """List every file in the log directory, newest first."""
    log_dir = _ensure_log_dir()
    files: list[dict[str, Any]] = []

    for entry in log_dir.iterdir():
        try:
            if not entry.is_file():
                continue
            files.append(_file_info(entry))
        except Exception:
            logger.exception("Unable to stat log file: %s", entry)

    files.sort(key=lambda f: f["modified_at"], reverse=True)

    return {
        "directory": str(log_dir),
        "count": len(files),
        "files": files,
    }


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
@router.get("/{filename}")
async def read_log(
    filename: str,
    tail: bool = Query(
        default=False,
        description="If true, only the last N lines are returned.",
    ),
    lines: int = Query(
        default=200,
        ge=1,
        le=100_000,
        description="Maximum number of trailing lines when tail=true.",
    ),
    grep: str | None = Query(
        default=None,
        description="Case-insensitive substring filter applied after tail.",
    ),
) -> dict[str, Any]:
    """Return the contents of a log file.

    By default the whole file is returned. With ``tail=true`` only the last
    ``lines`` lines are returned. ``grep`` further filters those lines by a
    case-insensitive substring match.
    """
    path = _safe_log_path(filename)
    stat = path.stat()

    if stat.st_size > MAX_READ_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Log file is too large to return in one response "
                f"({stat.st_size} bytes > {MAX_READ_BYTES} bytes). "
                "Use tail=true and/or grep to narrow the result."
            ),
        )

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.exception("Unable to read log file: %s", path)
        raise HTTPException(
            status_code=500,
            detail=f"Unable to read log file: {exc}",
        ) from exc

    all_lines = text.splitlines()

    if tail:
        all_lines = all_lines[-lines:]

    if grep:
        needle = grep.lower()
        all_lines = [ln for ln in all_lines if needle in ln.lower()]

    return {
        "name": path.name,
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat(),
        "total_lines": len(text.splitlines()),
        "returned_lines": len(all_lines),
        "tail": tail,
        "grep": grep,
        "content": "\n".join(all_lines),
    }