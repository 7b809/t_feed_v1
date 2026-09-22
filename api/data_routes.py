"""
api/data_routes.py

Endpoints for browsing and reading files inside the `data/` folder.

Routes
------
GET    /api/data/tree                     -> full recursive tree of data/
GET    /api/data/list                     -> list items in a folder (default: root)
GET    /api/data/file?path=...            -> read content of a specific file
GET    /api/data/download?path=...        -> download a specific file
GET    /api/data/download-folder?path=... -> download a folder as ZIP
GET    /api/data/info?path=...            -> metadata for a file or folder
DELETE /api/data/file?path=...&confirm=.. -> delete a file (guarded)
"""

from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    status,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

DEBUG_MODE = False

router = APIRouter(prefix="/api/data", tags=["data"])

# ---------------------------------------------------------------------
# Config / paths
# ---------------------------------------------------------------------
DATA_DIR: Path = Path(getattr(config, "DATA_DIR", "data")).resolve()

ALLOWED_EXTENSIONS = {
    ".json",
    ".jsonl",
    ".txt",
    ".log",
    ".csv",
    ".yaml",
    ".yml",
}
MAX_VIEW_BYTES = 10 * 1024 * 1024  # 10 MB inline view cap
MAX_TREE_DEPTH = 10


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _ensure_data_dir() -> None:
    if not DATA_DIR.exists() or not DATA_DIR.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Data directory not found: {DATA_DIR}",
        )


def _resolve_rel_path(rel_path: str | None, *, must_exist: bool = True) -> Path:
    """
    Safely resolve a relative path inside DATA_DIR.
    Blocks traversal, absolute paths, and symlink escapes.
    """
    if rel_path is None or rel_path.strip() in ("", ".", "/"):
        return DATA_DIR

    # Normalize slashes
    rel_path = rel_path.replace("\\", "/").strip("/")

    if ".." in rel_path.split("/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path traversal detected.",
        )

    full = (DATA_DIR / rel_path).resolve()

    # Must remain inside DATA_DIR
    if full != DATA_DIR and DATA_DIR not in full.parents:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path escapes data directory.",
        )

    if must_exist and not full.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Path not found: {rel_path}",
        )

    return full


def _rel(p: Path) -> str:
    """Relative path from DATA_DIR (posix-style)."""
    try:
        return p.relative_to(DATA_DIR).as_posix()
    except ValueError:
        return p.name


def _file_meta(p: Path) -> dict[str, Any]:
    st = p.stat()
    return {
        "name": p.name,
        "path": _rel(p),
        "type": "file",
        "extension": p.suffix.lower(),
        "size_bytes": st.st_size,
        "size_kb": round(st.st_size / 1024, 2),
        "modified_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
    }


def _dir_meta(p: Path) -> dict[str, Any]:
    st = p.stat()
    return {
        "name": p.name or "data",
        "path": _rel(p) if p != DATA_DIR else "",
        "type": "dir",
        "modified_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
    }


def _list_dir(p: Path) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for child in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if child.is_dir():
            items.append(_dir_meta(child))
        elif child.is_file() and child.suffix.lower() in ALLOWED_EXTENSIONS:
            items.append(_file_meta(child))

    return {
        "path": _rel(p) if p != DATA_DIR else "",
        "abs_path": str(p),
        "count": len(items),
        "items": items,
    }


def _build_tree(p: Path, depth: int = 0) -> dict[str, Any]:
    if depth > MAX_TREE_DEPTH:
        return {"name": p.name, "type": "dir", "truncated": True, "children": []}

    node: dict[str, Any] = {
        "name": p.name or "data",
        "path": _rel(p) if p != DATA_DIR else "",
        "type": "dir" if p.is_dir() else "file",
    }

    if p.is_file():
        node["extension"] = p.suffix.lower()
        node["size_bytes"] = p.stat().st_size
        return node

    children: list[dict[str, Any]] = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if child.is_dir():
                children.append(_build_tree(child, depth + 1))
            elif child.is_file() and child.suffix.lower() in ALLOWED_EXTENSIONS:
                children.append(_build_tree(child, depth + 1))
    except PermissionError as exc:
        node["error"] = f"Permission denied: {exc}"

    node["children"] = children
    node["child_count"] = len(children)
    return node


def _read_file_content(p: Path) -> dict[str, Any]:
    ext = p.suffix.lower()
    size = p.stat().st_size

    if size > MAX_VIEW_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"File too large to view inline ({size} bytes > {MAX_VIEW_BYTES}). "
                "Use /download instead."
            ),
        )

    raw = p.read_text(encoding="utf-8", errors="replace")

    if ext == ".json":
        try:
            parsed = json.loads(raw)
            return {
                "format": "json",
                "content": parsed,
                "raw": None,
            }
        except json.JSONDecodeError as exc:
            logger.warning("Invalid JSON in %s: %s", p, exc)
            return {
                "format": "json_invalid",
                "content": None,
                "raw": raw,
                "error": str(exc),
            }

    if ext == ".jsonl":
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        parsed_lines: list[Any] = []
        parse_errors: list[dict[str, Any]] = []
        for i, ln in enumerate(lines):
            try:
                parsed_lines.append(json.loads(ln))
            except json.JSONDecodeError as exc:
                parse_errors.append({"line": i + 1, "error": str(exc)})
        return {
            "format": "jsonl",
            "line_count": len(lines),
            "content": parsed_lines,
            "parse_errors": parse_errors,
            "raw": None,
        }

    # fallback: text
    return {
        "format": "text",
        "content": raw,
        "raw": None,
    }


def _build_zip_for_dir(folder: Path) -> BytesIO:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(folder):
            for fname in files:
                fpath = Path(root) / fname
                if fpath.suffix.lower() in ALLOWED_EXTENSIONS:
                    arcname = fpath.relative_to(DATA_DIR).as_posix()
                    zf.write(fpath, arcname=arcname)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------
class DataItem(BaseModel):
    name: str
    path: str
    type: str
    extension: str | None = None
    size_bytes: int | None = None
    size_kb: float | None = None
    modified_at: str | None = None


class ListResponse(BaseModel):
    path: str
    abs_path: str
    count: int
    items: list[DataItem]


class FileContentResponse(BaseModel):
    path: str
    abs_path: str
    name: str
    extension: str
    size_bytes: int
    modified_at: str
    format: str
    content: Any = None
    raw: str | None = None
    line_count: int | None = None
    parse_errors: list[dict[str, Any]] | None = None
    error: str | None = None


class InfoResponse(BaseModel):
    path: str
    abs_path: str
    type: str
    exists: bool
    size_bytes: int | None = None
    size_kb: float | None = None
    child_count: int | None = None
    modified_at: str | None = None


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@router.get(
    "/tree",
    summary="Recursive tree of the data folder",
)
async def data_tree(
    path: str | None = Query(
        None,
        description="Optional sub-path to start the tree from (default: root).",
    ),
    max_depth: int = Query(
        MAX_TREE_DEPTH,
        ge=1,
        le=MAX_TREE_DEPTH,
        description="Maximum recursion depth.",
    ),
):
    """Return a nested tree structure of the data/ folder."""
    _ensure_data_dir()
    root = _resolve_rel_path(path)

    if not root.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path is not a directory.",
        )

    global MAX_TREE_DEPTH
    original = MAX_TREE_DEPTH
    MAX_TREE_DEPTH = max_depth
    try:
        tree = _build_tree(root)
    finally:
        MAX_TREE_DEPTH = original

    return {
        "root": _rel(root) if root != DATA_DIR else "",
        "abs_root": str(root),
        "tree": tree,
    }


@router.get(
    "/list",
    response_model=ListResponse,
    summary="List files and folders in a directory",
)
async def data_list(
    path: str | None = Query(
        None,
        description="Relative folder path (default: root).",
    ),
):
    """List immediate children of the given folder."""
    _ensure_data_dir()
    target = _resolve_rel_path(path)

    if not target.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path is not a directory. Use /api/data/file for files.",
        )

    try:
        listing = _list_dir(target)
        return ListResponse(**listing)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to list data dir %s: %s", target, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list directory: {exc}",
        )


@router.get(
    "/info",
    response_model=InfoResponse,
    summary="Metadata for a file or folder",
)
async def data_info(
    path: str = Query(..., description="Relative path (file or folder)."),
):
    """Return metadata for the given path."""
    _ensure_data_dir()
    target = _resolve_rel_path(path)

    if target.is_file():
        meta = _file_meta(target)
        return InfoResponse(
            path=meta["path"],
            abs_path=str(target),
            type="file",
            exists=True,
            size_bytes=meta["size_bytes"],
            size_kb=meta["size_kb"],
            modified_at=meta["modified_at"],
        )

    if target.is_dir():
        children = list(target.iterdir())
        return InfoResponse(
            path=_rel(target) if target != DATA_DIR else "",
            abs_path=str(target),
            type="dir",
            exists=True,
            child_count=len(children),
            modified_at=datetime.fromtimestamp(
                target.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
        )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Path not found: {path}",
    )


@router.get(
    "/file",
    response_model=FileContentResponse,
    summary="Read content of a specific file",
)
async def data_file(
    path: str = Query(
        ..., description="Relative file path (e.g. runtime/service_state.json)."
    ),
):
    """
    Read a file from the data folder.

    - `.json`  -> parsed JSON
    - `.jsonl` -> parsed line-by-line
    - others   -> raw text
    """
    _ensure_data_dir()
    target = _resolve_rel_path(path)

    if not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path is not a file.",
        )

    ext = target.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Extension '{ext}' not allowed. Allowed: {sorted(ALLOWED_EXTENSIONS)}",
        )

    try:
        meta = _file_meta(target)
        parsed = _read_file_content(target)
        return FileContentResponse(
            path=meta["path"],
            abs_path=str(target),
            name=meta["name"],
            extension=meta["extension"],
            size_bytes=meta["size_bytes"],
            modified_at=meta["modified_at"],
            **parsed,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to read data file %s: %s", target, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read file: {exc}",
        )


@router.get(
    "/download",
    summary="Download a specific file",
    response_class=FileResponse,
)
async def data_download(
    path: str = Query(..., description="Relative file path."),
):
    """Download the specified file as an attachment."""
    target = _resolve_rel_path(path)

    if not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path is not a file.",
        )

    return FileResponse(
        path=str(target),
        media_type="application/octet-stream",
        filename=target.name,
    )


@router.get(
    "/download-folder",
    summary="Download a folder as ZIP",
)
async def data_download_folder(
    path: str | None = Query(
        None,
        description="Relative folder path (default: root).",
    ),
):
    """Zip and download the specified folder."""
    _ensure_data_dir()
    folder = _resolve_rel_path(path)

    if not folder.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path is not a directory.",
        )

    buf = _build_zip_for_dir(folder)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base = folder.name or "data"
    zip_name = f"{base}_{ts}.zip"

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_name}"',
        },
    )


@router.delete(
    "/file",
    summary="Delete a file (guarded)",
)
async def data_delete_file(
    path: str = Query(..., description="Relative file path."),
    confirm: bool = Query(False, description="Set true to confirm deletion."),
):
    """Delete a specific file. Requires confirm=true."""
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Deletion requires confirm=true query param.",
        )

    target = _resolve_rel_path(path)

    if not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path is not a file.",
        )

    try:
        target.unlink()
        logger.info("Deleted data file: %s", _rel(target))
        return {"deleted": True, "path": _rel(target)}
    except Exception as exc:
        logger.exception("Failed to delete %s: %s", target, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete file: {exc}",
        )
