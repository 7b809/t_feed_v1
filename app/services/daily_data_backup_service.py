# app/services/daily_data_backup_service.py
"""Daily ZIP archive of DATA_DIR uploaded to a MEGA folder.

Remote layout:
    <MEGA_BACKUP_ROOT>/<YYYYMMDD>_data.zip

The service runs independently of market-data processing. It writes a
date-keyed status document to MongoDB and sends best-effort Telegram
notifications. The local ZIP is staged outside DATA_DIR and removed after
the upload attempt.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class DailyDataBackupService:
    """Create one daily data ZIP and upload it to MEGA."""

    def __init__(
        self,
        config: Any,
        telegram: Any,
        state_collection: Any,
        mega_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._config = config
        self._telegram = telegram
        self._state_collection = state_collection
        self._mega_factory = mega_factory
        self._lock = asyncio.Lock()

    async def run_daily_backup(self, reason: str = "scheduled") -> dict[str, Any]:
        if not self._config.daily_data_backup_enabled:
            return {"status": "disabled", "reason": "backup_disabled"}

        if not self._config.mega_enabled:
            return {"status": "disabled", "reason": "mega_disabled"}

        async with self._lock:
            await self._notify(
                "Daily data backup started",
                f"Source: {self._config.data_dir}\n"
                f"Destination: {self._config.mega_backup_root}/\n"
                f"Reason: {reason}",
                "INFO",
            )
            try:
                result = await asyncio.to_thread(self._run_sync, reason)
            except Exception as exc:
                logger.exception("daily MEGA data backup failed")
                result = {
                    "status": "failed",
                    "reason": reason,
                    "date": datetime.now(IST).strftime("%Y%m%d"),
                    "error": f"{type(exc).__name__}: {exc}",
                }
                self._write_state(result)

            level = "SUCCESS" if result.get("status") == "uploaded" else "ERROR"
            await self._notify(
                "Daily data backup " + str(result.get("status", "unknown")),
                self._format_result(result),
                level,
            )
            return result

    def _run_sync(self, reason: str) -> dict[str, Any]:
        now = datetime.now(IST)
        date_key = now.strftime("%Y%m%d")
        filename = f"{date_key}_data.zip"
        archive_path: Path | None = None
        try:
            archive_path, stats = self._create_archive(date_key)
            checksum = self._sha256(archive_path)
            size_bytes = archive_path.stat().st_size
            mega_link, remote_path = self._upload_archive(archive_path, filename)
            result = {
                "status": "uploaded",
                "reason": reason,
                "date": date_key,
                "filename": filename,
                "remote_path": remote_path,
                "mega_link": mega_link,
                "size_bytes": size_bytes,
                "size_human": self._human_size(size_bytes),
                "sha256": checksum,
                "files_added": stats["files_added"],
                "files_skipped": stats["files_skipped"],
                "updated_at": datetime.now(timezone.utc),
                "updated_at_ist": datetime.now(IST).isoformat(),
            }
            self._write_state(result)
            logger.info(
                "[daily-backup] uploaded %s -> %s (%s, files=%d)",
                filename, remote_path, result["size_human"], stats["files_added"],
            )
            return result
        except Exception as exc:
            result = {
                "status": "failed",
                "reason": reason,
                "date": date_key,
                "filename": filename,
                "remote_path": f"{self._config.mega_backup_root}/{filename}",
                "error": f"{type(exc).__name__}: {exc}",
                "updated_at": datetime.now(timezone.utc),
                "updated_at_ist": datetime.now(IST).isoformat(),
            }
            self._write_state(result)
            raise
        finally:
            if archive_path is not None:
                try:
                    archive_path.unlink(missing_ok=True)
                except OSError:
                    logger.exception("could not remove staged backup ZIP: %s", archive_path)

    def _create_archive(self, date_key: str) -> tuple[Path, dict[str, int]]:
        source = Path(self._config.data_dir).expanduser().resolve()
        staging = Path(self._config.daily_data_backup_dir).expanduser().resolve()
        if not source.exists() or not source.is_dir():
            raise FileNotFoundError(f"DATA_DIR does not exist: {source}")
        if staging == source or source in staging.parents:
            raise ValueError("DAILY_DATA_BACKUP_DIR must be outside DATA_DIR")

        staging.mkdir(parents=True, exist_ok=True)
        final_path = staging / f"{date_key}_data.zip"
        temp_path = staging / f".{date_key}_data.{uuid4().hex}.tmp"
        files_added = 0
        files_skipped = 0
        try:
            with zipfile.ZipFile(
                temp_path, "w", compression=zipfile.ZIP_DEFLATED,
                compresslevel=6, allowZip64=True,
            ) as archive:
                for current_root, dirs, filenames in os.walk(
                    source, topdown=True, followlinks=False
                ):
                    current = Path(current_root)
                    dirs[:] = [d for d in dirs if not (current / d).is_symlink()]
                    for name in filenames:
                        path = current / name
                        if path.is_symlink() or not path.is_file() or name.endswith(".tmp"):
                            files_skipped += 1
                            continue
                        try:
                            archive.write(
                                path,
                                arcname=(Path(source.name) / path.relative_to(source)).as_posix(),
                            )
                            files_added += 1
                        except (OSError, PermissionError, zipfile.BadZipFile):
                            files_skipped += 1
                            logger.exception("skipping unreadable backup file: %s", path)

                if files_added == 0:
                    raise ValueError("DATA_DIR contains no readable files; refusing empty backup")
                archive.writestr(
                    f"{source.name}/BACKUP_MANIFEST.txt",
                    f"Project: {self._config.app_name}\n"
                    f"Date (IST): {date_key}\n"
                    f"Created (IST): {datetime.now(IST).isoformat()}\n"
                    f"Files included: {files_added}\n"
                    f"Files skipped: {files_skipped}\n",
                )
            with zipfile.ZipFile(temp_path, "r") as check:
                bad_member = check.testzip()
                if bad_member:
                    raise zipfile.BadZipFile(f"ZIP verification failed: {bad_member}")
            temp_path.replace(final_path)
            return final_path, {"files_added": files_added, "files_skipped": files_skipped}
        except Exception:
            temp_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise

    def _login_mega(self) -> Any:
        if self._mega_factory is not None:
            mega = self._mega_factory()
        else:
            try:
                from mega_sdk import Mega
            except ImportError as exc:
                raise RuntimeError("Install mega-sdk>=0.1.1 to enable MEGA backups") from exc
            mega = Mega()

        email = (self._config.mega_email or "").strip()
        password = (self._config.mega_password or "").strip()
        if not email or not password:
            raise RuntimeError("MEGA_EMAIL and MEGA_PASSWORD must be configured in .env")
        mega.login(email, password)
        mega.get_files()
        return mega

    @staticmethod
    def _find_folder(files: dict[str, Any], parent_id: str, name: str) -> str | None:
        for handle, node in files.items():
            if node.get("p") == parent_id and node.get("t") == 1:
                if (node.get("a") or {}).get("n") == name:
                    return handle
        return None

    def _ensure_remote_folder(self, mega: Any) -> str:
        parent_id = mega.root_id
        for folder_name in [self._config.mega_backup_root]:
            files = mega.get_files()
            found = self._find_folder(files, parent_id, folder_name)
            if found:
                parent_id = found
            else:
                parent_id = mega.create_folder_at(folder_name, parent_id)
        return parent_id

    def _upload_archive(self, archive_path: Path, filename: str) -> tuple[str | None, str]:
        mega = self._login_mega()
        folder_id = self._ensure_remote_folder(mega)

        # Upload directly under the final filename. Date-keyed names are
        # unique per day, so no temporary-name + rename dance is needed.
        mega.upload(str(archive_path), dest=folder_id, dest_filename=filename)

        # Verify the upload by filename + size.
        files = mega.get_files()
        expected_size = archive_path.stat().st_size
        uploaded_handle = None
        for handle, node in files.items():
            if node.get("p") != folder_id:
                continue
            if (node.get("a") or {}).get("n") != filename:
                continue
            if int(node.get("s", -1)) == expected_size:
                uploaded_handle = handle
                break

        if not uploaded_handle:
            # Best-effort cleanup of any residue with the same name, then fail.
            for handle, node in files.items():
                if node.get("p") == folder_id and (node.get("a") or {}).get("n") == filename:
                    try:
                        mega.destroy(handle)
                    except Exception:
                        logger.exception("could not clean failed MEGA upload node %s", handle)
            raise RuntimeError("MEGA upload could not be verified by remote filename and size")

        # Remove any older node with the same name (defensive re-run case).
        for handle, node in files.items():
            if handle == uploaded_handle:
                continue
            if node.get("p") == folder_id and (node.get("a") or {}).get("n") == filename:
                try:
                    mega.destroy(handle)
                except Exception:
                    logger.exception("could not remove older MEGA archive node %s", handle)

        link = None
        try:
            node = files.get(uploaded_handle)
            if node is not None:
                link = mega.get_link((uploaded_handle, node))
        except Exception:
            logger.exception("could not create MEGA public link for %s", filename)

        return link, f"{self._config.mega_backup_root}/{filename}"

    def _write_state(self, result: dict[str, Any]) -> None:
        if self._state_collection is None:
            logger.error("daily backup MongoDB collection is unavailable")
            return
        date_key = str(result.get("date") or datetime.now(IST).strftime("%Y%m%d"))
        document = dict(result)
        document["_id"] = date_key
        document["date"] = date_key
        try:
            self._state_collection.replace_one({"_id": date_key}, document, upsert=True)
        except Exception:
            logger.exception("could not persist daily backup state for %s", date_key)

    async def _notify(self, title: str, message: str, level: str) -> None:
        try:
            await self._telegram.send_message(title, message, level=level)
        except Exception:
            logger.exception("daily backup Telegram notification failed")

    @staticmethod
    def _format_result(result: dict[str, Any]) -> str:
        lines = [
            f"Date: {result.get('date', '—')} (IST)",
            f"Status: {result.get('status', 'unknown')}",
        ]
        if result.get("remote_path"):
            lines.append(f"MEGA path: {result['remote_path']}")
        if result.get("size_human"):
            lines.append(f"Archive size: {result['size_human']}")
        if result.get("files_added") is not None:
            lines.append(f"Files: {result['files_added']} (skipped: {result.get('files_skipped', 0)})")
        if result.get("mega_link"):
            lines.append(f"MEGA link: {result['mega_link']}")
        if result.get("error"):
            lines.append(f"Error: {result['error']}")
        return "\n".join(lines)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _human_size(size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{size} B"