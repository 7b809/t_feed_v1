"""Read-only API for the application's log directory.

Two log layers are exposed:

* **Combined** — the "project" log (``settings.log_file``, e.g.
  ``logs/app.log``) that receives every record from every module.
* **Per-module** — one file per Python module that calls
  ``get_logger(__name__)``, living inside ``settings.module_log_dir``
  (default ``logs/modules/``).

Endpoints
---------
GET /api/logs
    List files, newest first. By default includes both combined and
    per-module logs; each entry carries a ``source`` field
    (``"combined"`` or ``"module"``). Pass ``include_modules=false`` to
    list only the combined directory.

GET /api/logs/modules
    List only the per-module log files.

GET /api/logs/modules/{filename}
    Read a single per-module log file. Supports ``tail``, ``lines``, and
    ``grep`` (see below).

GET /api/logs/{filename}
    Read a single combined log file. Same query options.

For both read endpoints:
    * ``tail``  — return only the last N lines (default: all lines).
    * ``lines`` — maximum number of trailing lines when ``tail=true``.
    * ``grep``  — case-insensitive substring filter applied after tail.

The filenames are validated against the actual directory listing so path
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

# Combined ("project") log directory — where settings.log_file lives.
LOG_DIR: Path = settings.log_file.parent

# Per-module log directory.
MODULE_LOG_DIR: Path = settings.module_log_dir

# Cap on how much we will ever read from a single log file. 8 MB is
# generous for text logs and protects the server from a runaway file.
MAX_READ_BYTES = 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_path(directory: Path, filename: str) -> Path:
    """Resolve ``filename`` inside ``directory``, rejecting escape attempts."""
    if not filename or filename.strip() != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Log files never live in subdirectories of these folders, so reject
    # any separator early as defense-in-depth.
    if "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    candidate = (directory / filename).resolve()
    dir_resolved = directory.resolve()

    try:
        candidate.relative_to(dir_resolved)
    except ValueError:
        # Attempted traversal outside the log directory.
        raise HTTPException(status_code=400, detail="Invalid filename")

    if not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"Log file not found: {filename}")

    return candidate


def _file_info(path: Path, source: str) -> dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.name,
        "source": source,  # "combined" or "module"
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat(),
        "created_at": datetime.fromtimestamp(
            stat.st_ctime, tz=timezone.utc
        ).isoformat(),
    }


def _list_files(directory: Path, source: str) -> list[dict[str, Any]]:
    if not directory.exists():
        return []

    files: list[dict[str, Any]] = []
    for entry in directory.iterdir():
        try:
            if not entry.is_file():
                continue
            files.append(_file_info(entry, source))
        except Exception:
            logger.exception("Unable to stat log file: %s", entry)
    return files


def _read_log_file(
    directory: Path,
    filename: str,
    source: str,
    tail: bool,
    lines: int,
    grep: str | None,
) -> dict[str, Any]:
    path = _safe_path(directory, filename)
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
    total_lines = len(all_lines)

    if tail:
        all_lines = all_lines[-lines:]

    if grep:
        needle = grep.lower()
        all_lines = [ln for ln in all_lines if needle in ln.lower()]

    return {
        "name": path.name,
        "source": source,
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat(),
        "total_lines": total_lines,
        "returned_lines": len(all_lines),
        "tail": tail,
        "grep": grep,
        "content": "\n".join(all_lines),
    }


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
@router.get("")
async def list_logs(
    include_modules: bool = Query(
        default=True,
        description=(
            "When true (default), per-module log files from MODULE_LOG_DIR "
            "are included alongside the combined logs. Each entry's "
            "'source' field distinguishes the two."
        ),
    ),
) -> dict[str, Any]:
    """List log files, newest first.

    Includes the combined ("project") log directory and, when
    ``include_modules=true``, the per-module log directory. Each entry
    carries a ``source`` field: ``"combined"`` or ``"module"``.
    """
    combined_dir = _ensure_dir(LOG_DIR)
    files = _list_files(combined_dir, "combined")

    modules_dir = MODULE_LOG_DIR
    if include_modules and settings.module_logs_enabled and modules_dir.exists():
        files.extend(_list_files(modules_dir, "module"))

    files.sort(key=lambda f: f["modified_at"], reverse=True)

    return {
        "directory": str(combined_dir),
        "module_directory": str(modules_dir),
        "module_logs_enabled": settings.module_logs_enabled,
        "count": len(files),
        "files": files,
    }


@router.get("/modules")
async def list_module_logs() -> dict[str, Any]:
    """List every per-module log file, newest first."""
    if not settings.module_logs_enabled:
        raise HTTPException(
            status_code=404,
            detail="Per-module logging is disabled (MODULE_LOGS_ENABLED=false)",
        )

    module_dir = _ensure_dir(MODULE_LOG_DIR)
    files = _list_files(module_dir, "module")
    files.sort(key=lambda f: f["modified_at"], reverse=True)

    return {
        "directory": str(module_dir),
        "count": len(files),
        "files": files,
    }


# ---------------------------------------------------------------------------
# Read — per-module logs (must be declared before /{filename})
# ---------------------------------------------------------------------------
@router.get("/modules/{filename}")
async def read_module_log(
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
    """Return the contents of a per-module log file."""
    if not settings.module_logs_enabled:
        raise HTTPException(
            status_code=404,
            detail="Per-module logging is disabled (MODULE_LOGS_ENABLED=false)",
        )

    _ensure_dir(MODULE_LOG_DIR)
    return _read_log_file(
        MODULE_LOG_DIR, filename, "module", tail, lines, grep
    )


# ---------------------------------------------------------------------------
# Read — combined logs
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
    """Return the contents of a combined ("project") log file."""
    return _read_log_file(LOG_DIR, filename, "combined", tail, lines, grep)