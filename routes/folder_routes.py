"""Folder listing + zip-download API.

Exposes a small admin surface for inspecting and downloading the
project's top-level folders (``data``, ``logs``, ``logs_text``,
``meta_data``, ...) as ZIP archives.

    GET /admin/folders                       -> list of exposable folders
    GET /admin/folders/{folder_name}         -> metadata for one folder
    GET /admin/folders/{folder_name}/download -> stream the folder as a .zip

Design notes
------------
* Only folders that are explicitly allowed (either by the built-in
  default list, or by being present in the configured cleanup lists)
  are exposed. Everything else — including ``.git``, ``.venv``,
  ``__pycache__`` — is never returned or zipped.
* Every folder name is resolved strictly inside the project root.
  Path traversal (``..``, absolute paths outside, symlinks that
  escape the tree) is rejected with 404.
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
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from core import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["folders"])

# --------------------------------------------------------------------------- #
# Allow-list / guards                                                          #
# --------------------------------------------------------------------------- #

# Folders this API is allowed to expose. Anything not in this list is
# hidden even if it exists on disk. Configured cleanup folders are
# merged in automatically at request time so custom deployments don't
# have to edit this file.
_EXPOSABLE_DEFAULT = (
    "data",
    "logs",
    "logs_text",
    "meta_data",
    "downloads",
    "output",
    "temp",
)

# Hard cap on the size of a folder that can be zipped in one request.
_MAX_ZIP_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB


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
    """Return the full set of folder names this API may expose."""
    allowed: set[str] = set(_EXPOSABLE_DEFAULT)
    for extra in (
        config.CLEANUP_REMOVE_FOLDERS + config.CLEANUP_RESERVED_FOLDERS
    ):
        cleaned = str(extra or "").strip().rstrip("/\\")
        if cleaned:
            allowed.add(cleaned)
    return allowed


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


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #

@router.get("/folders")
def list_folders() -> dict[str, Any]:
    """List the folders that can be downloaded via this API.

    Response shape::

        {
          "count": 4,
          "folders": [
            {
              "name": "data",
              "path": "D:\\...\\data",
              "size_bytes": 123456,
              "file_count": 42,
              "dir_count": 8,
              "newest_mtime": "2026-10-15T05:37:06.670412+00:00",
              "download_url": "/admin/folders/data/download"
            },
            ...
          ]
        }
    """
    items: list[dict[str, Any]] = []
    for path in _iter_exposable_folders():
        items.append(
            {
                "name": path.name,
                "path": str(path),
                **_folder_stats(path),
                "download_url": f"/admin/folders/{path.name}/download",
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

    return {
        "name": path.name,
        "path": str(path),
        **_folder_stats(path),
        "download_url": f"/admin/folders/{path.name}/download",
    }


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