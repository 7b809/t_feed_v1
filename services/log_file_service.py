# services/log_file_service.py
"""Safe listing and reading of project log files.

Only files that are **direct children** of the two known log directories
(``logs/`` and ``logs_text/``) are exposed. Any path containing a slash,
backslash or ``..`` is rejected before the filesystem is touched, so the
HTTP layer cannot be abused for path traversal.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from core.logger import (
    LOGS_DIR,
    TEXT_LOGS_DIR,
    get_logger,
)

logger = get_logger(__file__)

# Public category tokens -> on-disk directories.
# "log"  -> logs/*.log, logs/*.log.N          (rotating per-module & combined)
# "text" -> logs_text/*.txt                   (append-only mirrors)
CATEGORY_TO_DIR: dict[str, str] = {
    "log": LOGS_DIR,
    "text": TEXT_LOGS_DIR,
}

# Tail defaults / caps for the reader endpoint.
DEFAULT_TAIL_LINES = 500
HARD_MAX_TAIL_LINES = 5000


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _validate_category(category: str) -> str:
    """Return the directory for a category or raise ``ValueError``."""
    if category not in CATEGORY_TO_DIR:
        raise ValueError(
            f"Unknown category '{category}'. "
            f"Allowed: {sorted(CATEGORY_TO_DIR)}"
        )
    return CATEGORY_TO_DIR[category]


def _validate_filename(filename: str) -> str:
    """Reject any filename that could escape its base directory."""
    if not filename or filename in (".", ".."):
        raise ValueError("Invalid filename")
    if "/" in filename or "\\" in filename or "\x00" in filename:
        raise ValueError("Invalid filename")
    if ".." in filename:
        raise ValueError("Invalid filename")
    return filename


def _safe_path(category: str, filename: str) -> Path:
    """Return the resolved path if it points at an existing regular file."""
    base_dir = Path(_validate_category(category)).resolve()
    filename = _validate_filename(filename)

    candidate = (base_dir / filename).resolve()

    # Double-check the resolved path is still a direct child of base_dir.
    if candidate.parent != base_dir:
        raise ValueError("Invalid filename")

    if not candidate.is_file():
        raise FileNotFoundError(filename)

    return candidate


def _file_meta(category: str, path: Path) -> dict:
    stat = path.stat()
    return {
        "category": category,
        "filename": path.name,
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
    }


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #
def list_log_files() -> list[dict]:
    """Return metadata for every file in ``logs/`` and ``logs_text/``.

    Sorted by category, then filename (rotated files come after the base).
    """
    result: list[dict] = []

    for category, directory in CATEGORY_TO_DIR.items():
        base = Path(directory)
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir(), key=lambda p: (p.name, p.suffix)):
            if not entry.is_file():
                continue
            try:
                result.append(_file_meta(category, entry))
            except OSError:
                logger.exception("Skipping unreadable file: %s", entry)

    logger.debug("list_log_files returned %d file(s).", len(result))
    return result


def read_log_file(
    category: str,
    filename: str,
    lines: int = DEFAULT_TAIL_LINES,
    raw: bool = False,
) -> dict:
    """Return the tail of ``<category>/<filename>``.

    :param category: ``"log"`` or ``"text"``
    :param filename: file name only (no path separators)
    :param lines:    number of trailing lines to return (default 500,
                     capped at ``HARD_MAX_TAIL_LINES``)
    :param raw:      when ``True``, also include the raw file text in
                     ``"raw_content"``; otherwise only the tail is returned.
    """
    path = _safe_path(category, filename)

    try:
        tail = max(1, min(int(lines), HARD_MAX_TAIL_LINES))
    except (TypeError, ValueError):
        tail = DEFAULT_TAIL_LINES

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        all_lines = fh.readlines()

    total = len(all_lines)
    chunk = all_lines[-tail:]
    content = "".join(chunk)

    meta = _file_meta(category, path)
    meta.update(
        {
            "total_lines": total,
            "returned_lines": len(chunk),
            "tail_lines": tail,
            "content": content,
        }
    )

    if raw:
        meta["raw_content"] = "".join(all_lines)

    logger.debug(
        "read_log_file %s/%s (total=%d, returned=%d)",
        category, filename, total, len(chunk),
    )
    return meta