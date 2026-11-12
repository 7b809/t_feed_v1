"""Daily ZIP backup of DATA_DIR and best-effort Telegram delivery."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

from ..config import Settings

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_PART_SIZE = 45 * 1024 * 1024


class DailyDataBackupService:
    """Create one daily project-data ZIP and send it in <=45 MiB Telegram parts.

    The archive is stored outside DATA_DIR by default so it can never include
    itself in tomorrow's archive. Failures are contained inside this service.
    """

    def __init__(self, config: Settings, telegram: Any,
                 part_size_bytes: int = DEFAULT_PART_SIZE) -> None:
        self._config = config
        self._telegram = telegram
        self._part_size = max(1, int(part_size_bytes))
        self._lock = asyncio.Lock()

    async def run_daily_backup(self) -> dict[str, Any]:
        if not self._config.daily_data_backup_enabled:
            logger.info("daily data backup skipped: DAILY_DATA_BACKUP_ENABLED=false")
            return {"status": "disabled"}

        async with self._lock:
            try:
                archive, stats = await asyncio.to_thread(self._create_archive)
                result = await self._send_archive(archive, stats)
                result.update({
                    "archive": str(archive),
                    "archive_size_bytes": archive.stat().st_size,
                    "files_added": stats["files_added"],
                    "files_skipped": stats["files_skipped"],
                })
                logger.info("daily project backup completed: %s", result)
                return result
            except Exception:
                logger.exception("daily project backup failed; application continues")
                try:
                    await self._telegram.send_message(
                        "Daily project backup failed",
                        "The scheduled data backup encountered an error. "
                        "The trading application and other scheduled jobs will continue. "
                        "Check application logs for details.",
                        level="ERROR",
                    )
                except Exception:
                    logger.exception("failed to send daily backup failure notification")
                return {"status": "failed"}

    def _create_archive(self) -> tuple[Path, dict[str, int]]:
        source = Path(self._config.data_dir).expanduser().resolve()
        backup_dir = Path(self._config.daily_data_backup_dir).expanduser().resolve()
        if not source.exists() or not source.is_dir():
            raise FileNotFoundError(f"DATA_DIR does not exist or is not a directory: {source}")
        if backup_dir == source or source in backup_dir.parents:
            raise ValueError(
                "DAILY_DATA_BACKUP_DIR must be outside DATA_DIR to prevent archive recursion"
            )

        backup_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now(IST).strftime("%Y%m%d")
        final_path = backup_dir / f"project_data_lifeline_{day}.zip"
        temp_path = backup_dir / f".{final_path.name}.tmp"
        files_added = 0
        files_skipped = 0

        try:
            with zipfile.ZipFile(
                temp_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as archive:
                # Keep the data/ root directory in the ZIP for easy restoration.
                for root, dirs, files in os.walk(source, topdown=True, followlinks=False):
                    root_path = Path(root)
                    dirs[:] = [
                        name for name in dirs
                        if not (root_path / name).is_symlink()
                    ]
                    for name in files:
                        file_path = root_path / name
                        if file_path.is_symlink() or not file_path.is_file():
                            files_skipped += 1
                            continue
                        arcname = (Path(source.name) / file_path.relative_to(source)).as_posix()
                        try:
                            archive.write(file_path, arcname=arcname)
                            files_added += 1
                        except (OSError, PermissionError, zipfile.BadZipFile):
                            files_skipped += 1
                            logger.exception("Skipping unreadable backup file: %s", file_path)

                archive.writestr(
                    f"{source.name}/BACKUP_MANIFEST.txt",
                    (
                        f"Project: {self._config.app_name}\n"
                        f"Created (IST): {datetime.now(IST).isoformat()}\n"
                        f"Source directory: {source}\n"
                        f"Files included: {files_added}\n"
                        f"Files skipped: {files_skipped}\n"
                    ),
                )
            temp_path.replace(final_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

        return final_path, {"files_added": files_added, "files_skipped": files_skipped}

    async def _send_archive(self, archive: Path, stats: dict[str, int]) -> dict[str, Any]:
        size = archive.stat().st_size
        day = datetime.now(IST).strftime("%d %B %Y")
        project = self._config.app_name
        if size <= self._part_size:
            caption = (
                f"📦 {project} — Daily Project Lifeline Backup\n"
                f"Date: {day} (IST)\n"
                f"Files: {stats['files_added']} | Skipped: {stats['files_skipped']}\n"
                f"Archive: {archive.name}\n"
                "This ZIP contains the project's complete data/ directory."
            )
            ok = await self._telegram.send_document(str(archive), caption)
            return {"status": "sent" if ok else "telegram_failed",
                    "parts_total": 1, "parts_sent": 1 if ok else 0}

        parts = await asyncio.to_thread(self._split_archive, archive)
        sent = 0
        failed_parts: list[str] = []
        total = len(parts)
        intro = (
            f"📦 {project} — Daily Project Lifeline Backup\n"
            f"Date: {day} (IST)\n"
            f"Original ZIP: {archive.name}\n"
            f"ZIP size: {size / (1024 * 1024):.2f} MiB\n"
            f"Split into {total} parts (maximum 45 MiB each).\n"
            "Download every part and concatenate them in part-number order "
            "to reconstruct the original ZIP."
        )
        try:
            await self._telegram.send_message("Daily project backup", intro, level="INFO")
        except Exception:
            logger.exception("backup introduction message failed; continuing with file parts")

        for index, part in enumerate(parts, start=1):
            caption = (
                f"📦 {project} — Lifeline Backup\n"
                f"Date: {day} | Part {index}/{total}\n"
                f"File: {part.name}\n"
                "Reassemble parts in ascending numeric order."
            )
            try:
                if await self._telegram.send_document(str(part), caption):
                    sent += 1
                else:
                    failed_parts.append(part.name)
            except Exception:
                logger.exception("Telegram backup part failed: %s", part.name)
                failed_parts.append(part.name)

        for part in parts:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove temporary backup part: %s", part)

        status = "sent" if not failed_parts else ("partially_sent" if sent else "telegram_failed")
        try:
            await self._telegram.send_message(
                "Daily project backup delivery result",
                f"Archive: {archive.name}\nParts sent: {sent}/{total}\n"
                f"Failed parts: {', '.join(failed_parts) if failed_parts else 'None'}",
                level="SUCCESS" if not failed_parts else "WARNING",
            )
        except Exception:
            logger.exception("backup delivery result notification failed")
        return {"status": status, "parts_total": total, "parts_sent": sent,
                "failed_parts": failed_parts}

    def _split_archive(self, archive: Path) -> list[Path]:
        parts: list[Path] = []
        part_index = 0
        try:
            with archive.open("rb") as source:
                while True:
                    chunk = source.read(self._part_size)
                    if not chunk:
                        break
                    part_index += 1
                    part_path = archive.with_name(f"{archive.name}.part{part_index:03d}")
                    with part_path.open("wb") as target:
                        target.write(chunk)
                    parts.append(part_path)
        except Exception:
            for part in parts:
                part.unlink(missing_ok=True)
            raise
        return parts
