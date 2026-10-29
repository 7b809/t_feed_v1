# routes/folder_routes.py
"""Folder listing + zip-download API.

Exposes a small admin surface for inspecting and downloading the
project's top-level folders as ZIP archives, plus a read-only text
browser for the files inside each folder.

    GET /admin/folders                              -> list of exposable folders
    GET /admin/folders/{folder_name}                -> metadata for one folder
    GET /admin/folders/{folder_name}/download       -> stream the folder as a .zip
    GET /admin/folders/{folder_name}/list           -> recursive file list (JSON)
    GET /admin/folders/{folder_name}/file?path=...  -> single file as text/plain

Allow-list
----------
The set of folders exposed here is controlled by ``_EXPOSABLE_DEFAULT``
at the bottom of this module. Only those folder names are ever listed,
browsed, or downloaded -- everything else (``.git``, ``.venv``, the
code tree, virtualenv, ``__pycache__``, ...) is invisible even if it
exists on disk.

The default list is deliberately narrow (``data``, ``refs``, ``logs``,
``logs_text``). To extend it without a code change, set the
``FOLDER_BROWSER_ALLOWLIST`` environment variable to a comma-separated
list of folder names::

    FOLDER_BROWSER_ALLOWLIST=data,refs,logs,logs_text,meta_data,temp

An explicit non-empty value REPLACES the built-in default. An empty or
unset value means "use the built-in default".

Design notes
------------
* Every folder name is resolved strictly inside the project root.
  Path traversal (``..``, absolute paths outside, symlinks that
  escape the tree) is rejected with 404 / 403.
* The list endpoint walks the folder recursively but skips forbidden
  subdirectories (``.git``, ``__pycache__``, virtualenv trees,
  node_modules) so a browse click cannot accidentally try to serve
  tens of thousands of unrelated files. The response is capped at
  ``_MAX_LIST_FILES`` entries.
* The file endpoint refuses files above ``_MAX_FILE_BYTES`` (default
  5 MB) and refuses anything with a null byte in its first 8 KB.
  Files that fail either check return HTTP 400 with a structured
  ``detail`` so the caller can display a hint instead of a raw 500.
  ``.env`` and its variants are always refused.
* The ZIP archive is written to a temp file on disk, streamed to the
  client via ``FileResponse``, and deleted afterwards by a
  ``BackgroundTask``. Nothing is buffered in memory regardless of the
  folder size.
* A size guard rejects folders larger than ``_MAX_ZIP_BYTES`` (2 GB)
  with HTTP 413 so a stray caller cannot exhaust the disk.
"""

from __future__ import annotations

import logging
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from starlette.background import BackgroundTask

from core import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["folders"])

# --------------------------------------------------------------------------- #
# Allow-list / guards                                                          #
# --------------------------------------------------------------------------- #

# Folders this API is allowed to expose BY DEFAULT. Anything not in
# this list (or the env-var override -- see ``_allowed_names``) is
# invisible even if it exists on disk.
#
# Edit this tuple to add or remove a folder permanently. To extend
# the list at runtime without a code change, set the
# ``FOLDER_BROWSER_ALLOWLIST`` environment variable to a
# comma-separated list; an explicit non-empty value replaces this
# tuple entirely.
_EXPOSABLE_DEFAULT = (
    "data",
    "refs",
    "logs",
    "logs_text",
)

# Hard cap on the size of a folder that can be zipped in one request.
_MAX_ZIP_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

# Hard cap on the size of a single file that can be read as text.
_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB

# Hard cap on the number of files returned by the list endpoint.
_MAX_LIST_FILES = 5000

# Directories skipped by the recursive walk of the list endpoint.
# These are the trees that would either leak secrets (.git) or blow up
# the response with tens of thousands of unrelated files (virtualenv,
# node_modules, __pycache__).
_FORBIDDEN_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "myenv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".idea",
    ".vscode",
    "htmlcov",
}

# Filenames skipped by the recursive walk of the list endpoint and
# refused by the file endpoint. These carry credentials or platform
# cruft that must never appear in a browse surface.
_FORBIDDEN_FILES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    ".env.test",
    ".DS_Store",
    "Thumbs.db",
}

# Kept as a compatibility alias for any external import that used the
# older list-only constant name.
_EXPOSABLE_FOLDERS = _EXPOSABLE_DEFAULT


# --------------------------------------------------------------------------- #
# Path resolution                                                              #
# --------------------------------------------------------------------------- #

def _project_root() -> Path:
    return Path(config.PROJECT_ROOT).resolve()


def _safe_resolve(name: str) -> Path | None:
    """Resolve ``name`` to a path strictly inside the project root.

    Returns None when the input is empty, contains a separator, starts
    with a dot, or resolves outside the project root.
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    root = _project_root()
    try:
        candidate = (root / name).resolve()
    except Exception:
        return None
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _allowed_names() -> set[str]:
    """Return the full set of folder names this API may expose.

    Resolution order:

      1. If ``FOLDER_BROWSER_ALLOWLIST`` is set to a non-empty
         comma-separated string, that list REPLACES the built-in
         default entirely. This is the runtime escape hatch for a
         deployment that needs a different set of folders without
         editing this file.
      2. Otherwise, the built-in ``_EXPOSABLE_DEFAULT`` tuple is used.

    The older behaviour of merging ``CLEANUP_REMOVE_FOLDERS`` and
    ``CLEANUP_RESERVED_FOLDERS`` was removed on purpose: those lists
    contain code-tree folders (``routes``, ``services``, ``core``,
    ``token_tasks``, ...) that should never be browserable over HTTP,
    and pulling them in made the folder browser show far more than
    the operator intended. Deployments that need ``temp`` /
    ``downloads`` / ``output`` / ``meta_data`` in the browser can set
    ``FOLDER_BROWSER_ALLOWLIST`` explicitly.
    """
    env_raw = os.getenv("FOLDER_BROWSER_ALLOWLIST", "").strip()
    if env_raw:
        names = {
            part.strip().rstrip("/\\")
            for part in env_raw.split(",")
            if part.strip()
        }
        if names:
            return names
    return set(_EXPOSABLE_DEFAULT)


def _iter_exposable_folders() -> list[Path]:
    """Return the existing folders that can be listed / downloaded."""
    out: list[Path] = []
    for name in sorted(_allowed_names()):
        path = _safe_resolve(name)
        if path is None:
            continue
        if path.is_dir():
            out.append(path)
    return out


# --------------------------------------------------------------------------- #
# Metadata helpers                                                             #
# --------------------------------------------------------------------------- #

def _human_size(size_bytes: int) -> str:
    """Return a compact human-readable size string."""
    n = float(size_bytes)
    if not (n >= 0):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{size_bytes} B"


def _folder_stats(path: Path) -> dict[str, Any]:
    """Return size + file/dir counts for a folder (recursive)."""
    total_bytes = 0
    file_count = 0
    dir_count = 0
    newest_mtime = 0.0

    for current_root, dirs, files in os.walk(path, followlinks=False):
        dir_count += len(dirs)
        for f in files:
            fp = Path(current_root) / f
            try:
                if fp.is_symlink():
                    continue
                st = fp.stat()
            except OSError:
                continue
            total_bytes += st.st_size
            file_count += 1
            if st.st_mtime > newest_mtime:
                newest_mtime = st.st_mtime

    return {
        "size_bytes": total_bytes,
        "size_human": _human_size(total_bytes),
        "file_count": file_count,
        "dir_count": dir_count,
        "newest_mtime": (
            datetime.fromtimestamp(newest_mtime, tz=timezone.utc).isoformat()
            if newest_mtime
            else None
        ),
    }


def _build_zip(folder: Path, dest: Path, include_empty: bool) -> int:
    """Write ``folder`` into ``dest`` as a zip. Returns bytes written."""
    folder_name = folder.name
    with zipfile.ZipFile(
        dest, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
    ) as zf:
        for current_root, dirs, files in os.walk(folder, followlinks=False):
            cur = Path(current_root)
            # Skip symlinked subdirectories so we never step outside
            # the folder we were asked to archive.
            dirs[:] = [d for d in dirs if not (cur / d).is_symlink()]

            if include_empty and not files and not dirs:
                try:
                    rel = cur.relative_to(folder)
                except ValueError:
                    continue
                if str(rel) != ".":
                    arcname = str(Path(folder_name) / rel) + "/"
                    zf.writestr(arcname, b"")

            for f in files:
                fp = cur / f
                try:
                    if fp.is_symlink():
                        continue
                    rel = fp.relative_to(folder)
                except (OSError, ValueError):
                    continue
                arcname = str(Path(folder_name) / rel)
                try:
                    zf.write(fp, arcname=arcname)
                except OSError:
                    logger.warning("Skipping unreadable file %s", fp)
    return dest.stat().st_size


def _walk_files(
    folder: Path,
    max_files: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(files, truncated)`` for one folder tree."""
    files: list[dict[str, Any]] = []
    truncated = False
    folder_root = folder.resolve()

    for current_root, dirs, filenames in os.walk(folder, followlinks=False):
        cur = Path(current_root)
        dirs[:] = [
            d
            for d in dirs
            if d not in _FORBIDDEN_DIRS and not (cur / d).is_symlink()
        ]

        for f in filenames:
            if f in _FORBIDDEN_FILES:
                continue
            fp = cur / f
            try:
                if fp.is_symlink():
                    continue
                st = fp.stat()
                rel = fp.resolve().relative_to(folder_root).as_posix()
            except (OSError, ValueError):
                continue
            files.append(
                {
                    "path": rel,
                    "size_bytes": st.st_size,
                    "size_human": _human_size(st.st_size),
                    "ext": fp.suffix.lower().lstrip("."),
                }
            )
            if len(files) >= max_files:
                truncated = True
                break
        if truncated:
            break

    files.sort(key=lambda x: x["path"])
    return files, truncated


def _read_text_file(
    folder: Path,
    rel_path: str,
    max_bytes: int = _MAX_FILE_BYTES,
) -> tuple[str | None, str | None]:
    """Read a text file inside ``folder``.

    Returns ``(content, None)`` on success or ``(None, error_code)`` on
    failure. Never raises for expected failure modes.
    """
    rel = str(rel_path or "").strip().replace("\\", "/")
    if not rel:
        return None, "empty_path"
    if (
        rel.startswith("/")
        or rel.startswith("../")
        or "/../" in rel
        or rel.endswith("/..")
        or rel == ".."
    ):
        return None, "path_traversal"

    if any(part in _FORBIDDEN_DIRS for part in rel.split("/")):
        return None, "forbidden_dir"

    folder_root = folder.resolve()
    try:
        target = (folder / rel).resolve()
    except Exception:
        return None, "invalid_path"

    try:
        target.relative_to(folder_root)
    except ValueError:
        return None, "path_traversal"

    if not target.is_file():
        return None, "not_found"

    if target.name in _FORBIDDEN_FILES:
        return None, "forbidden_file"

    try:
        size = target.stat().st_size
    except OSError:
        return None, "stat_failed"

    if size > max_bytes:
        return None, f"too_large ({size} > {max_bytes} bytes)"

    try:
        raw = target.read_bytes()
    except OSError as exc:
        return None, f"read_failed: {type(exc).__name__}"

    # Binary detection: if the first 8 KB contains a null byte, treat
    # the file as binary and refuse to serve it as text.
    if b"\x00" in raw[:8192]:
        return None, "binary_file"

    return raw.decode("utf-8", errors="replace"), None


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #

@router.get("/folders")
def list_folders() -> dict[str, Any]:
    """List the folders that can be browsed / downloaded via this API.

    Response shape::

        {
          "count": 4,
          "folders": [
            {
              "name": "data",
              "path": "/home/.../data",
              "size_bytes": 8806042,
              "size_human": "8.4 MB",
              "file_count": 3412,
              "dir_count": 178,
              "newest_mtime": "2026-10-29T05:37:06.670412+00:00",
              "download_url": "/admin/folders/data/download",
              "list_url": "/admin/folders/data/list"
            },
            ...
          ]
        }
    """
    items: list[dict[str, Any]] = []
    for path in _iter_exposable_folders():
        stats = _folder_stats(path)
        items.append(
            {
                "name": path.name,
                "path": str(path),
                "size_bytes": stats["size_bytes"],
                "size_human": stats["size_human"],
                "file_count": stats["file_count"],
                "dir_count": stats["dir_count"],
                "newest_mtime": stats["newest_mtime"],
                "download_url": f"/admin/folders/{path.name}/download",
                "list_url": f"/admin/folders/{path.name}/list",
            }
        )
    return {"count": len(items), "folders": items}


@router.get("/folders/{folder_name}")
def folder_detail(folder_name: str) -> dict[str, Any]:
    """Return metadata for one exposable folder."""
    path = _safe_resolve(folder_name)
    if path is None or not path.is_dir():
        raise HTTPException(status_code=404, detail="Folder not found")

    if path.name not in _allowed_names():
        raise HTTPException(status_code=403, detail="Folder is not exposable")

    stats = _folder_stats(path)
    return {
        "name": path.name,
        "path": str(path),
        "size_bytes": stats["size_bytes"],
        "size_human": stats["size_human"],
        "file_count": stats["file_count"],
        "dir_count": stats["dir_count"],
        "newest_mtime": stats["newest_mtime"],
        "download_url": f"/admin/folders/{path.name}/download",
        "list_url": f"/admin/folders/{path.name}/list",
    }


@router.get("/folders/{folder_name}/list")
def list_folder_contents(
    folder_name: str,
    max_files: int = Query(default=_MAX_LIST_FILES, ge=1, le=20000),
) -> dict[str, Any]:
    """Return every file inside ``folder_name`` as a flat list.

    Skips forbidden directories (``.git``, ``__pycache__``, virtualenv
    trees, node_modules) and forbidden filenames (``.env`` and friends).
    Paths are returned relative to the folder root and use forward
    slashes on every platform. The response is capped at ``max_files``
    entries; ``truncated`` is True when the cap was reached.
    """
    path = _safe_resolve(folder_name)
    if path is None or not path.is_dir():
        raise HTTPException(status_code=404, detail="Folder not found")

    if path.name not in _allowed_names():
        raise HTTPException(status_code=403, detail="Folder is not exposable")

    files, truncated = _walk_files(path, max_files)
    return {
        "folder": path.name,
        "path": str(path),
        "count": len(files),
        "truncated": truncated,
        "max_files": max_files,
        "files": files,
    }


@router.get(
    "/folders/{folder_name}/file",
    response_class=PlainTextResponse,
)
def read_folder_file(
    folder_name: str,
    path: str = Query(
        ...,
        min_length=1,
        description="Relative path inside the folder",
    ),
) -> PlainTextResponse:
    """Return the content of one text file inside ``folder_name``.

    Binary files, files above ``_MAX_FILE_BYTES`` (5 MB), paths that
    escape the folder root, and forbidden filenames (``.env`` and
    friends) are rejected with HTTP 400 / 403 / 404 and a structured
    ``detail`` body.
    """
    folder = _safe_resolve(folder_name)
    if folder is None or not folder.is_dir():
        raise HTTPException(status_code=404, detail="Folder not found")

    if folder.name not in _allowed_names():
        raise HTTPException(status_code=403, detail="Folder is not exposable")

    content, error = _read_text_file(folder, path)
    if error is not None:
        status = 404
        if error in {"path_traversal", "forbidden_file", "forbidden_dir"}:
            status = 403
        elif error.startswith("too_large") or error == "binary_file":
            status = 400
        raise HTTPException(
            status_code=status,
            detail={
                "error": error,
                "folder": folder.name,
                "path": path,
            },
        )

    return PlainTextResponse(
        content=content or "",
        media_type="text/plain; charset=utf-8",
    )


@router.get("/folders/{folder_name}/download")
def download_folder(
    folder_name: str,
    include_empty: bool = Query(
        default=True,
        description="Include empty directories as entries in the archive.",
    ),
):
    """Stream ``folder_name`` as a ``.zip`` archive.

    The archive is built into a temporary file on disk, streamed to the
    client with ``FileResponse``, and deleted by a background task once
    the response has been sent. Nothing is buffered in memory.
    """
    path = _safe_resolve(folder_name)
    if path is None or not path.is_dir():
        raise HTTPException(status_code=404, detail="Folder not found")

    if path.name not in _allowed_names():
        raise HTTPException(status_code=403, detail="Folder is not exposable")

    stats = _folder_stats(path)
    if stats["size_bytes"] > _MAX_ZIP_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Folder is too large to zip "
                f"({stats['size_bytes']} bytes > {_MAX_ZIP_BYTES} byte limit)"
            ),
        )

    # Create the temp zip destination.
    tmp_fd, tmp_path_str = tempfile.mkstemp(
        prefix=f"{folder_name}_", suffix=".zip"
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_path_str)

    try:
        _build_zip(path, tmp_path, include_empty=include_empty)
    except Exception:
        logger.exception("Folder zip failed for %s", path)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail="Failed to build zip")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{folder_name}_{timestamp}.zip"

    def _cleanup() -> None:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed cleaning temp zip %s", tmp_path)

    return FileResponse(
        path=tmp_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(_cleanup),
    )