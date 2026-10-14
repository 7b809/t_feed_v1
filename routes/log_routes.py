"""Log viewer routes.

Serves the HTML logs page and the raw log-content endpoint used by
``templates/show_logs.html``.

    GET /logs                        -> HTML log viewer page
    GET /ui/logs                     -> JSON list of available log files
    GET /ui/logs/{filename}          -> raw text content of one log file

The viewer reads files from two directories created by ``core.logger``:

    <project_root>/logs/          rotating *.log files
    <project_root>/logs_text/     append-only *.txt mirror

Both are surfaced in one list; filenames are the identifiers and are
uniquely resolvable because the two directories never contain a file
with the same name.

Security
--------
Filenames are resolved strictly inside the two allowed roots. Path
traversal (``..``, absolute paths, symlinks that escape) is rejected.
Only files with ``.log``, ``.txt``, ``.log.N`` suffixes are served.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["logs"])

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_LOG_DIRS: tuple[Path, ...] = (
    _PROJECT_ROOT / "logs",
    _PROJECT_ROOT / "logs_text",
)

# Filenames are always safe ASCII plus ".", "-", "_" and digits, and must
# end with one of the accepted suffixes. This blocks traversal even before
# the resolved-path check below.
_ALLOWED_SUFFIXES = (".log", ".txt")


def _ensure_dirs() -> None:
    for d in _LOG_DIRS:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.exception("Unable to create log dir %s", d)


def _is_safe_filename(name: str) -> bool:
    """Return True when ``name`` is a plain filename with a safe suffix."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return False
    if name != Path(name).name:
        return False
    lower = name.lower()
    # allow ".log", ".txt", and rotated variants like ".log.1"
    if lower.endswith(_ALLOWED_SUFFIXES):
        return True
    # rotated: name.log.1 -> endswith ".log.1" etc.
    for suffix in _ALLOWED_SUFFIXES:
        if suffix in lower and lower.split(suffix, 1)[1].lstrip(".").isdigit():
            return True
    return False


def _resolve_log_path(filename: str) -> Path:
    """Return the on-disk path for a filename or raise 404.

    Searches each allowed root in order. Any candidate that resolves
    outside its root is discarded.
    """
    if not _is_safe_filename(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    for root in _LOG_DIRS:
        candidate = (root / filename).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            # Escaped the root (e.g. via symlink); skip.
            continue
        if candidate.is_file():
            return candidate

    raise HTTPException(status_code=404, detail="Log file not found")


def _list_log_files() -> list[dict[str, Any]]:
    """Return metadata for every file in the two log dirs, newest first."""
    _ensure_dirs()

    entries: list[dict[str, Any]] = []
    for root in _LOG_DIRS:
        if not root.exists():
            continue
        for path in root.iterdir():
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append(
                {
                    "filename": path.name,
                    "size_bytes": stat.st_size,
                    "mtime": stat.st_mtime,
                    "dir": root.name,
                }
            )

    entries.sort(key=lambda e: e["mtime"], reverse=True)
    for e in entries:
        # mtime is only used for sorting; drop it before returning.
        e.pop("mtime", None)
    return entries


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

# Jinja2 environment points at the same templates folder used by the rest
# of the app. If you already have a central templates directory elsewhere,
# update this path once and it applies to every route.
templates = Jinja2Templates(directory=str(_PROJECT_ROOT / "templates"))

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request) -> HTMLResponse:
    """Render the log viewer page."""
    logs = _list_log_files()
    return templates.TemplateResponse(
        request=request,
        name="show_logs.html",
        context={
            "app_name": "Market Data Foundation",
            "logs": logs,
        },
    )


@router.get("/ui/logs")
def list_logs() -> dict[str, Any]:
    """JSON list of log files (used by callers that poll for new logs)."""
    logs = _list_log_files()
    return {"count": len(logs), "logs": logs}


@router.get("/ui/logs/{filename}", response_class=PlainTextResponse)
def read_log(filename: str) -> PlainTextResponse:
    """Return the raw text content of a single log file."""
    path = _resolve_log_path(filename)

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.exception("Unable to read log file %s", path)
        raise HTTPException(status_code=500, detail="Unable to read log file")

    return PlainTextResponse(content)