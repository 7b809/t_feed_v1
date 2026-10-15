"""Project transient folder cleanup for startup, daily refresh, and hard refresh.

Removes a configurable list of transient folders (``data``, ``temp``,
``downloads``, ``output``, ``__pycache__``, ...) so each refresh starts
from a clean state. A second, reserved list always wins on conflict:
if a folder name appears in both lists, the reserved list protects it.

Safety guarantees
-----------------
* Every target is resolved strictly inside the project root. Anything
  that escapes (absolute paths outside, ``..``, symlinks that leave
  the tree) is refused and reported in ``errors``.
* Symlinked targets are removed with ``unlink`` — never with
  ``rmtree`` — so we cannot follow a link and delete the wrong tree.
* On Windows, an open file handle blocks ``rmtree``. In that case the
  service falls back to clearing the directory's *contents*, leaving
  the directory itself in place so locked files continue to be
  accessible.
* The service never raises for expected failure modes — the caller
  always receives a summary dict.

Two config-driven lists (see ``core.config``):

    CLEANUP_REMOVE_FOLDERS     comma-separated, relative to project root
    CLEANUP_RESERVED_FOLDERS   comma-separated, wins on conflict
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import config

logger = logging.getLogger(__name__)


def _normalise(name: str) -> str:
    """Trim whitespace and trailing slashes so lists compare cleanly."""
    return str(name or "").strip().rstrip("/\\")


class CleanupService:
    """Removes configured transient folders under the project root."""

    def __init__(self) -> None:
        self._last_status: dict[str, Any] = {
            "ran_at": None,
            "reason": None,
            "removed": [],
            "skipped": [],
            "errors": [],
        }

    # ------------------------------------------------------------------ #
    # Path safety                                                        #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _project_root() -> Path:
        return Path(config.PROJECT_ROOT).resolve()

    def _resolve_inside(self, name: str) -> Path | None:
        """Return the resolved path for ``name`` inside the project root.

        Returns None when the resolved path escapes the project root.
        """
        root = self._project_root()
        try:
            target = (root / name).resolve()
        except Exception:
            return None
        try:
            target.relative_to(root)
        except ValueError:
            return None
        return target

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def run(self, reason: str = "startup") -> dict[str, Any]:
        """Execute one cleanup pass. Never raises for expected failures."""
        root = self._project_root()

        reserved = {
            _normalise(s)
            for s in config.CLEANUP_RESERVED_FOLDERS
            if _normalise(s)
        }
        targets = [
            _normalise(s)
            for s in config.CLEANUP_REMOVE_FOLDERS
            if _normalise(s)
        ]

        removed: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []

        logger.info(
            "[cleanup:%s] start — root=%s targets=%s reserved=%s",
            reason, root, targets, sorted(reserved),
        )

        for name in targets:
            # Reserved wins on conflict.
            if name in reserved:
                skipped.append(name)
                logger.info("[cleanup:%s] preserve %s (reserved)", reason, name)
                continue

            target = self._resolve_inside(name)
            if target is None:
                errors.append(f"{name}: escapes project root")
                logger.warning(
                    "[cleanup:%s] refuse %s (escapes project root)",
                    reason, name,
                )
                continue

            if not target.exists():
                skipped.append(name)
                logger.debug("[cleanup:%s] skip %s (missing)", reason, name)
                continue

            # Symlinks are unlinked, never recursed into.
            if target.is_symlink():
                try:
                    target.unlink()
                    removed.append(name)
                    logger.info("[cleanup:%s] removed symlink %s", reason, name)
                except Exception as exc:
                    errors.append(f"{name}: {type(exc).__name__}: {exc}")
                    logger.exception(
                        "[cleanup:%s] symlink removal failed for %s", reason, name
                    )
                continue

            if not target.is_dir():
                skipped.append(name)
                logger.info(
                    "[cleanup:%s] skip %s (not a directory)", reason, name
                )
                continue

            try:
                shutil.rmtree(target)
                removed.append(name)
                logger.info("[cleanup:%s] removed %s", reason, name)
            except OSError as exc:
                # Likely a Windows file lock. Fall back to clearing
                # the contents so at least the files get removed.
                logger.warning(
                    "[cleanup:%s] rmtree failed for %s (%s); clearing contents",
                    reason, name, type(exc).__name__,
                )
                try:
                    self._clear_contents(target)
                    removed.append(f"{name}/*")
                    logger.info(
                        "[cleanup:%s] cleared contents of %s", reason, name
                    )
                except Exception as inner:
                    errors.append(f"{name}: {type(inner).__name__}: {inner}")
                    logger.exception(
                        "[cleanup:%s] clearing %s failed", reason, name
                    )
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                logger.exception(
                    "[cleanup:%s] removal failed for %s", reason, name
                )

        status = {
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "removed": removed,
            "skipped": skipped,
            "errors": errors,
            "project_root": str(root),
        }
        self._last_status = status
        logger.info(
            "[cleanup:%s] done — removed=%d skipped=%d errors=%d",
            reason, len(removed), len(skipped), len(errors),
        )
        return status

    @staticmethod
    def _clear_contents(path: Path) -> None:
        """Best-effort clear of a directory's contents, ignoring locks."""
        for child in path.iterdir():
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            except Exception:
                # Individual entries may be locked; keep going.
                continue

    def last_status(self) -> dict[str, Any]:
        return dict(self._last_status)


cleanup_service = CleanupService()