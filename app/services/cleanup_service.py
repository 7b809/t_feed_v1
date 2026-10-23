# app/services/cleanup_service.py
"""
Delete configured folders (``CLEANUP_DIRS``) so a refresh starts on a
truly clean slate.

Called from three places:

  1. ``app/main.py`` lifespan     -> trigger="startup"
       Wipes the configured folders BEFORE bootstrap runs and before the
       background startup refresh thread is scheduled.

  2. ``MegaArchiveService.refresh("scheduled")`` -> trigger="scheduled"
       Wipes BEFORE the daily download / extraction begins.

  3. ``MegaArchiveService.refresh("manual")``    -> trigger="manual"
       Wipes BEFORE the manual hard-refresh download / extraction begins.

Startup does *not* clean again inside ``refresh("startup")`` because the
lifespan already did it. Cleaning twice would race against the EMA
bootstrap, which writes ``all_instruments.json`` and ``_ema_state/``
into the same tree during the same window.

Safety
------
* Never deletes filesystem roots (``/``, ``C:\\``).
* Refuses to delete a path that is itself a symlink (could point anywhere).
* Refuses ``""``, ``.``, ``..``, ``/`` and ``\\`` from the config.
* Failures are logged and swallowed — a cleanup error must not crash a
  refresh. Leftover files will simply be overwritten by the extraction.
* Idempotent: missing paths are a no-op.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from app.config import Settings

logger = logging.getLogger(__name__)


_FORBIDDEN_TOKENS = {"", ".", "..", "/", "\\"}


class CleanupError(RuntimeError):
    """Raised only on invalid configuration, not on runtime I/O errors."""


def _validate_config_token(token: str) -> None:
    if token in _FORBIDDEN_TOKENS:
        raise CleanupError(f"Refusing unsafe CLEANUP_DIRS entry: {token!r}")


def clean_directories(config: Settings, *, trigger: str = "manual") -> list[str]:
    """
    Delete every folder listed in ``config.cleanup_dirs``.

    Returns the list of paths that were actually removed (for logging).
    Never raises on I/O errors — worst case, a folder is left in place
    and the subsequent extraction overwrites its contents.
    """
    # Validate the raw config first so an obvious typo fails loudly at
    # the moment cleanup runs (not silently mid-delete).
    raw = (config.cleanup_dirs or "").strip()
    if not raw:
        logger.debug("Cleanup [%s]: CLEANUP_DIRS is empty — nothing to do", trigger)
        return []
    for item in raw.split(","):
        _validate_config_token(item.strip())

    targets = config.cleanup_dirs_list
    if not targets:
        return []

    removed: list[str] = []

    for target in targets:
        # --- Safety: never delete a filesystem root ---
        if target == Path(target.anchor):
            logger.warning("Cleanup [%s]: refusing filesystem root %s", trigger, target)
            continue

        # --- Safety: never delete through a symlink ---
        # If the configured folder IS a symlink, its target could be anywhere.
        if target.is_symlink():
            logger.warning(
                "Cleanup [%s]: refusing symlinked target %s (resolves to %s)",
                trigger,
                target,
                target.resolve(),
            )
            continue

        # --- Idempotent no-op if already gone ---
        if not target.exists():
            logger.debug("Cleanup [%s]: %s does not exist, skipping", trigger, target)
            continue

        # --- Delete, with one silent retry for Windows file-in-use cases ---
        try:
            shutil.rmtree(target)
            removed.append(str(target))
            logger.info("Cleanup [%s]: removed %s", trigger, target)
            continue
        except OSError as exc:
            logger.warning(
                "Cleanup [%s]: %s failed (%s) — retrying with ignore_errors",
                trigger,
                target,
                exc,
            )
            shutil.rmtree(target, ignore_errors=True)

        if not target.exists():
            removed.append(str(target))
            logger.info("Cleanup [%s]: removed %s (after retry)", trigger, target)
        else:
            logger.error(
                "Cleanup [%s]: %s still present after retry — continuing anyway",
                trigger,
                target,
            )

    if removed:
        logger.info("Cleanup [%s]: %d path(s) removed", trigger, len(removed))
    return removed