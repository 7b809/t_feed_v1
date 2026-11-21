# services/daily_data_backup_service.py
"""Daily ZIP backup of the isolation project's data directory to MEGA."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import json
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class DailyDataBackupService:
    """Create, verify and upload a date-keyed project data archive."""

    def __init__(
        self,
        *,
        data_dir: str,
        staging_dir: str,
        app_name: str,
        mega_enabled: bool,
        mega_email: str,
        mega_password: str,
        mega_backup_root: str,
        cleanup_enabled: bool = True,
        telegram_sender=None,
        state_collection: Any = None,
    ) -> None:
        self.data_dir = data_dir
        self.staging_dir = staging_dir
        self.app_name = app_name
        self.mega_enabled = mega_enabled
        self.mega_email = mega_email
        self.mega_password = mega_password
        self.mega_backup_root = mega_backup_root.strip().strip("/")
        self.cleanup_enabled = bool(cleanup_enabled)
        self.telegram_sender = telegram_sender or (lambda message: None)
        self.state_collection = state_collection
        self._lock = asyncio.Lock()

    async def run_daily_backup(self, reason: str = "scheduled") -> dict[str, Any]:
        if not self.mega_enabled:
            return {"status": "disabled", "reason": "mega_disabled"}
        async with self._lock:
            await asyncio.to_thread(
                self._notify,
                f"Isolation MEGA backup started | {reason}",
                f"Source: {self.data_dir}\nDestination: {self.mega_backup_root}/",
            )
            try:
                result = await asyncio.to_thread(self._run_sync, reason)
            except Exception as exc:
                logger.exception("Isolation MEGA backup failed")
                result = {
                    "status": "failed",
                    "reason": reason,
                    "date": datetime.now(IST).strftime("%Y%m%d"),
                    "error": f"{type(exc).__name__}: {exc}",
                }
                self._write_state(result)
            level = "SUCCESS" if result.get("status") == "uploaded" else "ERROR"
            await asyncio.to_thread(
                self._notify,
                f"Isolation MEGA backup {result.get('status', 'unknown')}",
                self._format_result(result),
            )
            return result

    def _run_sync(self, reason: str) -> dict[str, Any]:
        date_key = datetime.now(IST).strftime("%Y%m%d")

        # Resume a previously verified upload if the process stopped during cleanup.
        previous = self._read_state(date_key)
        if previous and previous.get("status") in ("uploaded", "uploaded_cleanup_failed"):
            if self.cleanup_enabled and previous.get("cleanup_status") != "complete":
                logger.warning("[isolation-backup] resuming post-upload cleanup for %s", date_key)
                return self._complete_cleanup(previous)
            if previous.get("cleanup_status") == "complete" or not self.cleanup_enabled:
                logger.info("[isolation-backup] backup already completed for %s", date_key)
                return previous

        filename = f"{date_key}_data.zip"
        archive_path = None
        try:
            archive_path, stats = self._create_archive(date_key)
            size_bytes = archive_path.stat().st_size
            checksum = self._sha256(archive_path)

            # This returns only after MEGA confirms remote filename and byte size.
            link = self._upload_archive(archive_path, filename)
            result = {
                "status": "uploaded",
                "reason": reason,
                "date": date_key,
                "filename": filename,
                "remote_path": f"{self.mega_backup_root}/{filename}",
                "mega_link": link,
                "size_bytes": size_bytes,
                "size_human": self._human_size(size_bytes),
                "sha256": checksum,
                "files_added": stats["files_added"],
                "files_skipped": stats["files_skipped"],
                "updated_at": datetime.now(timezone.utc),
                "updated_at_ist": datetime.now(IST).isoformat(),
            }

            if self.cleanup_enabled:
                pending = dict(result, cleanup_status="pending")
                # Never perform destructive cleanup without a durable receipt.
                if not self._write_state(pending):
                    blocked = dict(
                        result,
                        status="uploaded_cleanup_failed",
                        cleanup_status="blocked_state_not_persisted",
                        cleanup_error=(
                            "MEGA upload succeeded, but no durable cleanup receipt "
                            "could be persisted; DATA_DIR was retained."
                        ),
                    )
                    logger.error(
                        "[isolation-backup] refusing DATA_DIR cleanup: "
                        "pending receipt could not be persisted"
                    )
                    self._write_state(blocked)
                    return blocked
                result = self._complete_cleanup(pending)
            else:
                result["cleanup_status"] = "disabled"
                self._write_state(result)

            logger.info(
                "[isolation-backup] %s %s -> %s (%s; %d files; cleanup=%s)",
                result.get("status"), filename, result["remote_path"],
                result["size_human"], stats["files_added"], result.get("cleanup_status"),
            )
            return result
        except Exception as exc:
            self._write_state({
                "status": "failed",
                "reason": reason,
                "date": date_key,
                "filename": filename,
                "remote_path": f"{self.mega_backup_root}/{filename}",
                "error": f"{type(exc).__name__}: {exc}",
                "updated_at": datetime.now(timezone.utc),
                "updated_at_ist": datetime.now(IST).isoformat(),
            })
            raise
        finally:
            if archive_path is not None:
                try:
                    archive_path.unlink(missing_ok=True)
                except OSError:
                    logger.exception("Could not remove staged backup %s", archive_path)

    def _state_path(self, date_key: str) -> Path:
        return Path(self.staging_dir).expanduser().resolve() / f".{date_key}_data_backup_state.json"

    def _read_state(self, date_key: str) -> dict[str, Any] | None:
        # Local receipt allows recovery even if MongoDB is unavailable on restart.
        try:
            path = self._state_path(date_key)
            if path.is_file():
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    return value
        except Exception:
            logger.exception("Could not read local MEGA backup receipt for %s", date_key)
        if self.state_collection is not None:
            try:
                value = self.state_collection.find_one({"_id": date_key})
                if isinstance(value, dict):
                    return value
            except Exception:
                logger.exception("Could not read MongoDB MEGA backup state for %s", date_key)
        return None

    def _write_state(self, result: dict[str, Any]) -> bool:
        date_key = str(result.get("date") or datetime.now(IST).strftime("%Y%m%d"))
        document = dict(result)
        document["_id"] = date_key
        document["date"] = date_key
        persisted = False
        try:
            path = self._state_path(date_key)
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            temp.write_text(
                json.dumps(document, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(temp, path)
            persisted = True
        except Exception:
            logger.exception("Could not persist local MEGA backup receipt for %s", date_key)
        if self.state_collection is not None:
            try:
                self.state_collection.replace_one({"_id": date_key}, document, upsert=True)
                persisted = True
            except Exception:
                logger.exception("Could not persist MongoDB MEGA backup status for %s", date_key)
        if not persisted:
            logger.error("No durable MEGA backup state could be persisted for %s", date_key)
        return persisted

    def _complete_cleanup(self, upload_result: dict[str, Any]) -> dict[str, Any]:
        result = dict(upload_result)
        if not self.cleanup_enabled:
            result.update(status="uploaded", cleanup_status="disabled")
            self._write_state(result)
            return result
        try:
            stats = self._cleanup_data_dir()
            result.update(
                status="uploaded",
                cleanup_status="complete",
                cleaned_files=stats["removed_files"],
                cleaned_directories=stats["removed_directories"],
                cleaned_at_ist=datetime.now(IST).isoformat(),
            )
            logger.info(
                "[isolation-backup] DATA_DIR cleanup completed: files=%d directories=%d",
                stats["removed_files"], stats["removed_directories"],
            )
        except Exception as exc:
            result.update(
                status="uploaded_cleanup_failed",
                cleanup_status="failed",
                cleanup_error=f"{type(exc).__name__}: {exc}",
            )
            logger.exception("[isolation-backup] upload succeeded but DATA_DIR cleanup failed")
        result["updated_at"] = datetime.now(timezone.utc)
        result["updated_at_ist"] = datetime.now(IST).isoformat()
        self._write_state(result)
        return result

    def _cleanup_data_dir(self) -> dict[str, int]:
        """Remove every child of DATA_DIR while retaining the DATA_DIR directory."""
        source_arg = Path(self.data_dir).expanduser()
        if source_arg.is_symlink():
            raise ValueError(f"Refusing to clean symlink DATA_DIR: {source_arg}")
        source = source_arg.resolve()
        project_root = Path(__file__).resolve().parents[1]
        if (
            source == Path(source.anchor)
            or source == project_root
            or project_root.is_relative_to(source)
        ):
            raise ValueError(f"Refusing unsafe DATA_DIR cleanup target: {source}")

        staging = Path(self.staging_dir).expanduser().resolve()
        if staging == source or source in staging.parents:
            raise ValueError("DAILY_DATA_BACKUP_DIR must be outside DATA_DIR")
        if not source.exists() or not source.is_dir():
            raise FileNotFoundError(f"DATA_DIR does not exist or is not a directory: {source}")

        removed_files = 0
        removed_directories = 0
        for child in list(source.iterdir()):
            if child.is_symlink() or not child.is_dir():
                child.unlink()
                removed_files += 1
                continue
            for _, dirs, filenames in os.walk(child, topdown=True, followlinks=False):
                removed_directories += len(dirs)
                removed_files += len(filenames)
            shutil.rmtree(child)
            removed_directories += 1

        remaining = list(source.iterdir())
        if remaining:
            raise RuntimeError(
                "DATA_DIR cleanup incomplete; remaining entries: "
                + ", ".join(item.name for item in remaining[:10])
            )
        return {"removed_files": removed_files, "removed_directories": removed_directories}

    def _create_archive(self, date_key: str) -> tuple[Path, dict[str, int]]:
        source = Path(self.data_dir).expanduser().resolve()
        staging = Path(self.staging_dir).expanduser().resolve()
        if not source.is_dir():
            raise FileNotFoundError(f"DATA_DIR does not exist: {source}")
        if staging == source or source in staging.parents:
            raise ValueError("DAILY_DATA_BACKUP_DIR must be outside DATA_DIR")

        staging.mkdir(parents=True, exist_ok=True)
        final_path = staging / f"{date_key}_data.zip"
        temp_path = staging / f".{date_key}_{uuid4().hex}.tmp"
        added = skipped = 0
        try:
            with zipfile.ZipFile(
                temp_path, "w", compression=zipfile.ZIP_DEFLATED,
                compresslevel=6, allowZip64=True,
            ) as archive:
                for root, dirs, files in os.walk(source, topdown=True, followlinks=False):
                    current = Path(root)
                    dirs[:] = [d for d in dirs if not (current / d).is_symlink()]
                    for name in files:
                        path = current / name
                        if path.is_symlink() or not path.is_file() or name.endswith(".tmp"):
                            skipped += 1
                            continue
                        try:
                            archive.write(
                                path,
                                arcname=(Path(source.name) / path.relative_to(source)).as_posix(),
                            )
                            added += 1
                        except (OSError, PermissionError, zipfile.BadZipFile):
                            skipped += 1
                            logger.exception("Skipping unreadable backup file %s", path)
                if added == 0:
                    raise ValueError("DATA_DIR contains no readable files; refusing empty backup")
                archive.writestr(
                    f"{source.name}/BACKUP_MANIFEST.txt",
                    f"Project: {self.app_name}\nDate (IST): {date_key}\n"
                    f"Created (IST): {datetime.now(IST).isoformat()}\n"
                    f"Files included: {added}\nFiles skipped: {skipped}\n",
                )
            with zipfile.ZipFile(temp_path, "r") as check:
                bad = check.testzip()
                if bad:
                    raise zipfile.BadZipFile(f"ZIP verification failed: {bad}")
            temp_path.replace(final_path)
            return final_path, {"files_added": added, "files_skipped": skipped}
        except Exception:
            temp_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise

    def _login_mega(self):
        try:
            from mega_sdk import Mega
        except ImportError as exc:
            raise RuntimeError("Install mega-sdk>=0.1.1 to enable MEGA backups") from exc
        if not self.mega_email or not self.mega_password:
            raise RuntimeError("MEGA_EMAIL and MEGA_PASSWORD must be configured")
        mega = Mega()
        mega.login(self.mega_email, self.mega_password)
        mega.get_files()
        return mega

    @staticmethod
    def _find_folder(files, parent_id: str, name: str):
        for handle, node in files.items():
            if node.get("p") == parent_id and node.get("t") == 1:
                if (node.get("a") or {}).get("n") == name:
                    return handle
        return None

    def _upload_archive(self, archive_path: Path, filename: str):
        mega = self._login_mega()
        files = mega.get_files()
        folder_id = self._find_folder(files, mega.root_id, self.mega_backup_root)
        if not folder_id:
            folder_id = mega.create_folder_at(self.mega_backup_root, mega.root_id)

        mega.upload(str(archive_path), dest=folder_id, dest_filename=filename)
        files = mega.get_files()
        expected_size = archive_path.stat().st_size
        uploaded_handle = next((
            handle for handle, node in files.items()
            if node.get("p") == folder_id
            and (node.get("a") or {}).get("n") == filename
            and int(node.get("s", -1)) == expected_size
        ), None)
        if not uploaded_handle:
            # Do not leave a partial or unverifiable object under the final name.
            for handle, node in files.items():
                if node.get("p") == folder_id and (node.get("a") or {}).get("n") == filename:
                    try:
                        mega.destroy(handle)
                    except Exception:
                        logger.exception("Could not clean unverified MEGA node %s", handle)
            raise RuntimeError("MEGA upload could not be verified by filename and size")

        # A manual retry on the same date replaces the previous archive,
        # keeping exactly one date-keyed ZIP in the remote folder.
        for handle, node in files.items():
            if handle == uploaded_handle:
                continue
            if node.get("p") == folder_id and (node.get("a") or {}).get("n") == filename:
                try:
                    mega.destroy(handle)
                except Exception:
                    logger.exception("Could not remove older MEGA archive node %s", handle)

        link = None
        try:
            link = mega.get_link((uploaded_handle, files[uploaded_handle]))
        except Exception:
            logger.exception("Could not create MEGA share link for %s", filename)
        return link

    def _write_state(self, result: dict[str, Any]) -> None:
        if self.state_collection is None:
            logger.warning("MEGA backup status collection unavailable")
            return
        date_key = str(result.get("date") or datetime.now(IST).strftime("%Y%m%d"))
        document = dict(result)
        document["_id"] = date_key
        document["date"] = date_key
        try:
            self.state_collection.replace_one({"_id": date_key}, document, upsert=True)
        except Exception:
            logger.exception("Could not persist MEGA backup status for %s", date_key)

    def _notify(self, title: str, message: str) -> None:
        try:
            self.telegram_sender(f"*{title}*\n{message}")
        except Exception:
            logger.exception("Isolation MEGA backup Telegram notification failed")

    @staticmethod
    def _format_result(result: dict[str, Any]) -> str:
        lines = [
            f"Date: {result.get('date', '—')} (IST)",
            f"Status: {result.get('status', 'unknown')}",
        ]
        for key, label in (
            ("remote_path", "MEGA path"),
            ("size_human", "Archive size"),
            ("mega_link", "MEGA link"),
            ("error", "Error"),
            ("cleanup_status", "DATA_DIR cleanup"),
            ("cleanup_error", "Cleanup error"),
        ):
            if result.get(key):
                lines.append(f"{label}: {result[key]}")
        if result.get("cleaned_files") is not None:
            lines.append(
                f"DATA_DIR removed: {result['cleaned_files']} files, "
                f"{result.get('cleaned_directories', 0)} directories"
            )
        if result.get("files_added") is not None:
            lines.append(
                f"Files: {result['files_added']} (skipped: {result.get('files_skipped', 0)})"
            )
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
