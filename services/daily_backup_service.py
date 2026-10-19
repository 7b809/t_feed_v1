"""Daily zip-and-save-to-MongoDB backup of configured folders.

At the configured time (default **16:00 IST**, every day), this service:

    1. Zips the folders listed in ``DAILY_BACKUP_FOLDERS`` (default ``["data"]``)
       from the project root into a single archive in the OS temp dir.
    2. Measures the archive size.
    3. If size <= ``DAILY_BACKUP_MAX_BLOB_MB * 1024 * 1024`` (default 15 MB):
         * saves the archive bytes + full metadata to
           ``<DAILY_BACKUP_MONGO_DB>.backup_collection`` under the configured
           doc ``_id``, status = ``saved``.
    4. If size > the inline limit:
         * saves ONLY metadata (no ``data`` blob) with status =
           ``skipped_size_limit`` and a clear ``reason`` / ``note`` explaining
           that the archive was too large to store inline.

Destination document schema (matches the reference candle-backup shape):

    {
      "_id"                 : "market_engine_v1_daily_data_backup",
      "archive_name"        : "data.zip",
      "archive_size_bytes"  : int,
      "archive_size_human"  : "10.3 MB",
      "data"                : BinData(0, "...") | None,
      "folder"              : "<source label>",
      "folders"             : ["data", ...],
      "file_count"          : int,
      "updated_at"          : "2026-10-18 16:00:12 IST",
      "updated_at_utc"      : ISODate("2026-10-18T10:30:12.933Z"),
      "limit_bytes"         : int,
      "limit_human"         : "15.0 MB",
      "max_blob_mb"         : int,
      "note"                : "...",
      "reason"              : null | "...",
      "status"              : "saved" | "skipped_size_limit" | "error",
      "run_reason"          : "scheduled" | "manual"
    }

Design notes
------------
* Zip is built **in a temp file on disk**, never fully in memory. If the
  final size exceeds the limit, the temp file is deleted and only metadata
  is written — RAM usage stays low regardless of ``data/`` size.
* All writers in this project use the pattern ``tmp.write_text()`` +
  ``tmp.replace(final)``, so ``data/`` files are always atomic on disk
  when the zipper reads them — no half-written JSON can end up in the
  archive.
* A non-blocking lock prevents a scheduled run and a manual run from
  overlapping.
* The service never raises for expected failure modes — ``run()`` always
  returns a summary dict.
"""

from __future__ import annotations

import os
import tempfile
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bson import Binary
from pymongo import MongoClient

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _human_size(size_bytes: int) -> str:
    n = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{size_bytes} B"


def _market_tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.MARKET_TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


# --------------------------------------------------------------------------- #
# Service                                                                      #
# --------------------------------------------------------------------------- #
class DailyBackupService:
    """Zips configured folders and saves them to MongoDB once a day."""

    def __init__(self) -> None:
        self._client: MongoClient | None = None
        self._lock = threading.Lock()
        self._last_status: dict[str, Any] = {"status": "never"}

    # ------------------------------------------------------------------ #
    # Mongo helpers                                                      #
    # ------------------------------------------------------------------ #
    def _mongo(self) -> MongoClient:
        if self._client is None:
            if not config.MONGO_URI:
                raise RuntimeError(
                    "MONGO_URL must be set to save the daily backup"
                )
            self._client = MongoClient(config.MONGO_URI)
        return self._client

    def _collection(self):
        db_name = config.DAILY_BACKUP_MONGO_DB
        coll_name = config.DAILY_BACKUP_COLLECTION
        return self._mongo()[db_name][coll_name]

    # ------------------------------------------------------------------ #
    # Archive naming                                                     #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _archive_name(folders: list[str]) -> str:
        if not folders:
            return "backup.zip"
        if len(folders) == 1:
            base = folders[0].rstrip("/\\")
            return f"{base}.zip"
        if len(folders) == 2:
            a = folders[0].rstrip("/\\")
            b = folders[1].rstrip("/\\")
            return f"{a}+{b}.zip"
        return "backup.zip"

    # ------------------------------------------------------------------ #
    # Zip building (disk-backed, low RAM)                                #
    # ------------------------------------------------------------------ #
    def _build_zip(self, folders: list[str]) -> dict[str, Any]:
        """Zip ``folders`` under the project root into a temp file.

        Returns a dict with:
            tmp_path   : Path — the temp .zip on disk (caller must delete)
            size_bytes : int
            file_count : int
            skipped    : list[str] — folder names that were missing
        """
        root = Path(config.PROJECT_ROOT).resolve()

        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix="daily_backup_", suffix=".zip"
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)

        file_count = 0
        skipped: list[str] = []

        try:
            with zipfile.ZipFile(
                tmp_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                allowZip64=True,
            ) as zf:
                for folder_name in folders:
                    folder_name = str(folder_name or "").strip()
                    if not folder_name:
                        continue

                    folder_path = (root / folder_name).resolve()
                    try:
                        folder_path.relative_to(root)
                    except ValueError:
                        logger.warning(
                            "[daily-backup] refusing %s (escapes project root)",
                            folder_name,
                        )
                        skipped.append(folder_name)
                        continue

                    if not folder_path.exists() or not folder_path.is_dir():
                        logger.warning(
                            "[daily-backup] folder missing: %s", folder_name
                        )
                        skipped.append(folder_name)
                        continue

                    for current_root, dirs, files in os.walk(
                        folder_path, followlinks=False
                    ):
                        cur = Path(current_root)

                        # Skip symlinked subdirectories entirely.
                        dirs[:] = [
                            d for d in dirs if not (cur / d).is_symlink()
                        ]

                        for f in files:
                            fp = cur / f
                            try:
                                if fp.is_symlink():
                                    continue
                                # Skip transient .tmp files written by the
                                # atomic-write pattern elsewhere in the app.
                                if f.endswith(".tmp"):
                                    continue
                                rel = fp.relative_to(root)
                            except (OSError, ValueError):
                                continue

                            try:
                                zf.write(fp, arcname=str(rel))
                                file_count += 1
                            except OSError:
                                logger.warning(
                                    "[daily-backup] skipping unreadable %s",
                                    fp,
                                )

            size_bytes = tmp_path.stat().st_size
            return {
                "tmp_path": tmp_path,
                "size_bytes": size_bytes,
                "file_count": file_count,
                "skipped": skipped,
            }
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ #
    # Document builder                                                   #
    # ------------------------------------------------------------------ #
    def _build_doc(
        self,
        *,
        archive_bytes: bytes | None,
        archive_name: str,
        size_bytes: int,
        file_count: int,
        status: str,
        reason: str | None,
        note: str,
        run_reason: str,
        skipped_folders: list[str],
    ) -> dict[str, Any]:
        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone(_market_tz())
        limit_bytes = int(config.DAILY_BACKUP_MAX_BLOB_MB) * 1024 * 1024

        doc: dict[str, Any] = {
            "_id": config.DAILY_BACKUP_DOC_ID,
            "archive_name": archive_name,
            "archive_size_bytes": int(size_bytes),
            "archive_size_human": _human_size(size_bytes),
            "folder": config.DAILY_BACKUP_SOURCE_LABEL,
            "folders": list(config.DAILY_BACKUP_FOLDERS),
            "skipped_folders": skipped_folders,
            "file_count": int(file_count),
            "updated_at": now_local.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "updated_at_utc": now_utc,
            "limit_bytes": limit_bytes,
            "limit_human": _human_size(limit_bytes),
            "max_blob_mb": int(config.DAILY_BACKUP_MAX_BLOB_MB),
            "note": note,
            "reason": reason,
            "status": status,
            "run_reason": run_reason,
        }
        doc["data"] = Binary(archive_bytes) if archive_bytes else None
        return doc

    # ------------------------------------------------------------------ #
    # Public entry point                                                 #
    # ------------------------------------------------------------------ #
    def run(self, reason: str = "scheduled") -> dict[str, Any]:
        """Build the zip, enforce the inline limit, save to MongoDB."""
        if not config.DAILY_BACKUP_ENABLED:
            return self._record({
                "status": "disabled",
                "reason": reason,
                "message": "DAILY_BACKUP_ENABLED is False",
            })

        if not self._lock.acquire(blocking=False):
            logger.info(
                "[daily-backup] skipped — another run is in progress "
                "(reason=%s)",
                reason,
            )
            return self._record({
                "status": "already_running",
                "reason": reason,
            })

        try:
            folders = list(config.DAILY_BACKUP_FOLDERS)
            if not folders:
                logger.warning(
                    "[daily-backup] no folders configured — nothing to do"
                )
                return self._record({
                    "status": "no_folders",
                    "reason": "DAILY_BACKUP_FOLDERS is empty",
                    "run_reason": reason,
                })

            archive_name = self._archive_name(folders)
            limit_bytes = int(config.DAILY_BACKUP_MAX_BLOB_MB) * 1024 * 1024

            logger.info(
                "[daily-backup] START — reason=%s folders=%s limit=%s",
                reason,
                folders,
                _human_size(limit_bytes),
            )

            zip_info = self._build_zip(folders)
            tmp_path: Path = zip_info["tmp_path"]
            size_bytes = int(zip_info["size_bytes"])
            file_count = int(zip_info["file_count"])
            skipped_folders = list(zip_info.get("skipped") or [])

            try:
                # ---- Case A: fits under the inline limit --------------- #
                if size_bytes <= limit_bytes:
                    archive_bytes = tmp_path.read_bytes()
                    doc = self._build_doc(
                        archive_bytes=archive_bytes,
                        archive_name=archive_name,
                        size_bytes=size_bytes,
                        file_count=file_count,
                        status="saved",
                        reason=None,
                        note=(
                            f"Backup archive stored inline "
                            f"({_human_size(size_bytes)}). "
                            f"Doc _id={config.DAILY_BACKUP_DOC_ID!r}, "
                            f"database={config.DAILY_BACKUP_MONGO_DB!r}, "
                            f"collection={config.DAILY_BACKUP_COLLECTION!r}."
                        ),
                        run_reason=reason,
                        skipped_folders=skipped_folders,
                    )
                    self._collection().replace_one(
                        {"_id": config.DAILY_BACKUP_DOC_ID},
                        doc,
                        upsert=True,
                    )
                    logger.info(
                        "[daily-backup] SAVED — size=%s files=%d doc=%s",
                        _human_size(size_bytes),
                        file_count,
                        config.DAILY_BACKUP_DOC_ID,
                    )
                    return self._record({
                        "status": "saved",
                        "reason": reason,
                        "archive_name": archive_name,
                        "archive_size_bytes": size_bytes,
                        "archive_size_human": _human_size(size_bytes),
                        "file_count": file_count,
                        "skipped_folders": skipped_folders,
                        "limit_bytes": limit_bytes,
                        "limit_human": _human_size(limit_bytes),
                        "doc_id": config.DAILY_BACKUP_DOC_ID,
                        "database": config.DAILY_BACKUP_MONGO_DB,
                        "collection": config.DAILY_BACKUP_COLLECTION,
                    })

                # ---- Case B: exceeds the inline limit ------------------ #
                reason_text = (
                    f"Unable to save the {archive_name!r} archive to MongoDB "
                    f"inline because it exceeds the configured limit "
                    f"({_human_size(size_bytes)} > "
                    f"{_human_size(limit_bytes)}). "
                    f"Only metadata has been written. To capture the actual "
                    f"archive, either raise DAILY_BACKUP_MAX_BLOB_MB, "
                    f"reduce DAILY_BACKUP_FOLDERS, or copy the folder "
                    f"manually before the next refresh deletes it."
                )
                note_text = (
                    f"Backup archive NOT stored inline: final zip is "
                    f"{_human_size(size_bytes)} (limit "
                    f"{_human_size(limit_bytes)}). Metadata-only record."
                )
                doc = self._build_doc(
                    archive_bytes=None,
                    archive_name=archive_name,
                    size_bytes=size_bytes,
                    file_count=file_count,
                    status="skipped_size_limit",
                    reason=reason_text,
                    note=note_text,
                    run_reason=reason,
                    skipped_folders=skipped_folders,
                )
                self._collection().replace_one(
                    {"_id": config.DAILY_BACKUP_DOC_ID},
                    doc,
                    upsert=True,
                )
                logger.warning(
                    "[daily-backup] SKIPPED (size limit) — size=%s "
                    "limit=%s files=%d doc=%s",
                    _human_size(size_bytes),
                    _human_size(limit_bytes),
                    file_count,
                    config.DAILY_BACKUP_DOC_ID,
                )
                return self._record({
                    "status": "skipped_size_limit",
                    "reason": reason_text,
                    "archive_name": archive_name,
                    "archive_size_bytes": size_bytes,
                    "archive_size_human": _human_size(size_bytes),
                    "file_count": file_count,
                    "skipped_folders": skipped_folders,
                    "limit_bytes": limit_bytes,
                    "limit_human": _human_size(limit_bytes),
                    "doc_id": config.DAILY_BACKUP_DOC_ID,
                    "database": config.DAILY_BACKUP_MONGO_DB,
                    "collection": config.DAILY_BACKUP_COLLECTION,
                })

            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "[daily-backup] failed cleaning temp zip %s",
                        tmp_path,
                    )

        except Exception as exc:
            logger.exception("[daily-backup] run failed")
            return self._record({
                "status": "error",
                "reason": reason,
                "error": f"{type(exc).__name__}: {exc}",
            })
        finally:
            self._lock.release()

    # ------------------------------------------------------------------ #
    # Status accessor                                                    #
    # ------------------------------------------------------------------ #
    def last_status(self) -> dict[str, Any]:
        return dict(self._last_status)

    def _record(self, status: dict[str, Any]) -> dict[str, Any]:
        status.setdefault(
            "ran_at", datetime.now(timezone.utc).isoformat()
        )
        self._last_status = status
        return status


daily_backup_service = DailyBackupService()