# app/services/archive_service.py
#
# Safe ZIP validation + extraction into the managed tree.
#
# Preservation rules during replacement:
#   * all_instruments.json : the archive's version wins when present;
#                            the on-disk copy is preserved ONLY if the
#                            archive does not include one (fallback).
#   * _ema_state/          : ALWAYS preserved (pure runtime state produced
#                            by Job 2 that must survive refreshes).
#
# Windows swap strategy
# ---------------------
# os.replace() on Windows refuses to replace an existing DIRECTORY with a
# directory, so the swap must be: move old → backup, then rename stage →
# destination. Both renames can transiently fail with WinError 5 when file
# handles are still open (antivirus, file indexer, another thread writing
# inside destination). Each rename is retried with linear backoff before
# being treated as a real failure.
#
# The startup race (EMA live loop writing _ema_state/ while archive
# refresh swaps the tree) is handled at the caller (app/main.py) by
# running refresh to completion BEFORE the EMA service is constructed.

from __future__ import annotations

import hashlib
import logging
import shutil
import stat
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from app.config import Settings

logger = logging.getLogger(__name__)

# Directories that ALWAYS survive a refresh (pure runtime state).
ALWAYS_PRESERVE_DIRS = ("_ema_state",)
# Files that survive a refresh only if the archive does not provide a new one.
FALLBACK_PRESERVE_FILES = ("all_instruments.json",)

# Retry tuning for the Windows directory swap.
_REPLACE_MAX_ATTEMPTS = 5
_REPLACE_BASE_DELAY = 0.2  # seconds; linear backoff (0.2, 0.4, 0.6, ...)


class ArchiveError(RuntimeError):
    pass


class ArchiveService:
    def __init__(self, config: Settings):
        self.config = config

    # ------------------------------------------------------------------ #
    def validate_zip(self, archive_path: Path) -> None:
        if not archive_path.is_file() or archive_path.stat().st_size <= 0:
            raise ArchiveError("Archive is missing or empty")
        if archive_path.stat().st_size > self.config.archive_limit_bytes:
            raise ArchiveError("Archive exceeds MAX_ARCHIVE_SIZE_MB")
        if not zipfile.is_zipfile(archive_path):
            raise ArchiveError("Downloaded file is not a valid ZIP archive")
        try:
            with zipfile.ZipFile(archive_path) as zf:
                bad = zf.testzip()
                if bad is not None:
                    raise ArchiveError("ZIP CRC validation failed")
        except (OSError, zipfile.BadZipFile) as exc:
            raise ArchiveError("ZIP archive is corrupted or unreadable") from exc

    # ------------------------------------------------------------------ #
    def extract_and_replace(self, archive_path: Path) -> tuple[int, str]:
        self.validate_zip(archive_path)
        destination = self.config.destination_dir
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(
            prefix=f".{destination.name}-stage-", dir=destination.parent
        ))
        backup = destination.with_name(f".{destination.name}.backup")
        file_count = 0
        total_written = 0
        try:
            with zipfile.ZipFile(archive_path) as zf:
                infos = zf.infolist()
                if len(infos) > self.config.max_archive_files:
                    raise ArchiveError("Archive exceeds MAX_ARCHIVE_FILES")
                seen: set[str] = set()
                for info in infos:
                    name = info.filename.replace("\\", "/")
                    path = PurePosixPath(name)
                    if path.is_absolute() or not name or any(
                        part in {"..", ""} for part in path.parts
                    ):
                        raise ArchiveError("ZIP contains an unsafe path")
                    if path.parts and ":" in path.parts[0]:
                        raise ArchiveError("ZIP contains a drive-qualified path")
                    normalized = path.as_posix()
                    if normalized in seen:
                        raise ArchiveError("ZIP contains duplicate entry names")
                    seen.add(normalized)
                    mode = (info.external_attr >> 16) & 0xFFFF
                    if stat.S_ISLNK(mode):
                        raise ArchiveError("ZIP symbolic links are not permitted")
                    target = (stage / Path(*path.parts)).resolve()
                    if stage.resolve() not in target.parents and target != stage.resolve():
                        raise ArchiveError("ZIP entry resolves outside extraction root")
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info, "r") as source, target.open("xb") as output:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            total_written += len(chunk)
                            if total_written > self.config.extracted_limit_bytes:
                                raise ArchiveError(
                                    "Archive exceeds MAX_EXTRACTED_SIZE_MB"
                                )
                            output.write(chunk)
                    file_count += 1

            # ---- Preserve Job 2 runtime artefacts across replacement ----
            self._preserve_runtime_state(destination, stage)

            # ---- Swap stage → destination, with retry + recovery ----
            self._swap_into_place(stage, destination, backup)

            return file_count, self.sha256(archive_path)

        except (zipfile.BadZipFile, OSError) as exc:
            raise ArchiveError(
                "ZIP extraction or destination replacement failed"
            ) from exc
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

    # ------------------------------------------------------------------ #
    def _swap_into_place(self, stage: Path, destination: Path, backup: Path) -> None:
        """Move stage → destination, moving old destination to backup first."""
        # Clean any stale backup from a previous interrupted run.
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)

        # 1. Move current destination to backup (if it exists).
        moved_old = False
        if destination.exists():
            self._replace_dir_with_retry(
                destination, backup, label="dest->backup"
            )
            moved_old = True

        # 2. Move stage to destination.
        try:
            self._replace_dir_with_retry(
                stage, destination, label="stage->dest"
            )
        except Exception:
            # Recovery: restore the previous tree so the app keeps running.
            if moved_old and backup.exists():
                try:
                    if destination.exists():
                        shutil.rmtree(destination, ignore_errors=True)
                    self._replace_dir_with_retry(
                        backup, destination, label="backup->dest (recovery)"
                    )
                    logger.warning(
                        "Recovered previous tree after failed swap "
                        "(destination=%s)", destination,
                    )
                except Exception:
                    logger.exception(
                        "Recovery failed: could not restore %s -> %s",
                        backup, destination,
                    )
            raise

        # 3. Success — clean backup.
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _replace_dir_with_retry(
        src: Path,
        dst: Path,
        *,
        label: str,
        max_attempts: int = _REPLACE_MAX_ATTEMPTS,
        base_delay: float = _REPLACE_BASE_DELAY,
    ) -> None:
        """
        Rename src → dst with retries.

        On Windows, os.replace() for directories fails transiently when
        handles are still open (antivirus, indexing service, another thread
        writing inside src or dst). Linear backoff usually clears these.
        """
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                src.replace(dst)
                if attempt > 0:
                    logger.info(
                        "Replace [%s] %s -> %s succeeded on attempt %d/%d",
                        label, src.name, dst.name, attempt + 1, max_attempts,
                    )
                return
            except (PermissionError, OSError) as exc:
                last_exc = exc
                if attempt + 1 < max_attempts:
                    delay = base_delay * (attempt + 1)
                    logger.warning(
                        "Replace [%s] %s -> %s failed (attempt %d/%d): %s; "
                        "retrying in %.1fs",
                        label, src.name, dst.name,
                        attempt + 1, max_attempts, exc, delay,
                    )
                    time.sleep(delay)
        assert last_exc is not None
        logger.error(
            "Replace [%s] %s -> %s exhausted %d attempts: %s",
            label, src.name, dst.name, max_attempts, last_exc,
        )
        raise last_exc

    # ------------------------------------------------------------------ #
    @staticmethod
    def _preserve_runtime_state(destination: Path, stage: Path) -> None:
        """
        Copy Job-2 artefacts from the previous tree into the staged tree.

        * ``_ema_state/``            — always preserved.
        * ``all_instruments.json``   — preserved ONLY if the archive does not
                                       provide its own copy. This lets a fresh
                                       archive update the catalog when it
                                       ships new instruments, while still
                                       keeping the on-disk copy as fallback
                                       on cold installs where the archive
                                       omits the file.
        """
        if not destination.exists():
            return

        for rel in ALWAYS_PRESERVE_DIRS:
            src = destination / rel
            if not src.exists():
                continue
            dst = stage / rel
            try:
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            except OSError:
                logger.warning("Failed to preserve runtime path %s", rel)

        for rel in FALLBACK_PRESERVE_FILES:
            src = destination / rel
            dst = stage / rel
            if not src.exists() or dst.exists():
                # Nothing to preserve, or the archive already provides it.
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                logger.info("Preserved %s (archive did not provide one)", rel)
            except OSError:
                logger.warning("Failed to preserve fallback file %s", rel)

    # ------------------------------------------------------------------ #
    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()