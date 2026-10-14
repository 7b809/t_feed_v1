"""Fetch the candle-backup archive from MongoDB and extract it locally.

The candle-backup project upserts two documents into MongoDB:

    metadata doc (BACKUP_METADATA_DOC_ID)  -> run summary + MEGA link
    blob doc     (BACKUP_BLOB_DOC_ID)      -> the archive bytes (Binary)
                                              or a metadata-only doc with
                                              `file_link` when too large.

This service:

    1. Reads the blob document.
    2. If `data` (Binary) is present, writes it to a temp file and
       extracts it into `LIVE_DATA_DIR`.
    3. If `data` is null (archive too large for Mongo), logs a clear
       warning including the MEGA link for manual download.
    4. Returns a small summary dict so callers can Telegram-notify.

The extracted tree preserves the archive layout:

    data/
    ├── index/<underlying>/candles.json
    │                       /ema_crosses.json
    ├── nifty/<strike>_<type>/candles.json
    │                         /ema_crosses.json
    └── sensex/<strike>_<type>/candles.json
                              /ema_crosses.json

`_metadata.json` and `_manifest.txt` are also written at the archive
root — harmless here, useful for debugging.
"""

from __future__ import annotations

import io
import logging
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import MongoClient

from core import config

logger = logging.getLogger(__name__)


class BackupDataService:
    def __init__(self) -> None:
        self._client: MongoClient | None = None
        self._last_extracted_at: datetime | None = None
        self._last_archive_size: int | None = None
        self._last_status: str = "never"
        self._last_error: str | None = None

    # ------------------------------------------------------------------ #
    # Mongo helpers                                                      #
    # ------------------------------------------------------------------ #
    def _mongo(self) -> MongoClient:
        if self._client is None:
            if not config.MONGO_URI or not config.MONGO_DB:
                raise RuntimeError(
                    "MONGO_URL / MONGO_DB must be set to extract the backup"
                )
            self._client = MongoClient(config.MONGO_URI)
        return self._client

    def _blob_doc(self) -> dict[str, Any] | None:
        coll = self._mongo()[config.MONGO_DB][config.BACKUP_BLOB_COLLECTION]
        return coll.find_one({"_id": config.BACKUP_BLOB_DOC_ID})

    def _metadata_doc(self) -> dict[str, Any] | None:
        coll = self._mongo()[config.MONGO_DB][config.BACKUP_METADATA_COLLECTION]
        return coll.find_one({"_id": config.BACKUP_METADATA_DOC_ID})

    # ------------------------------------------------------------------ #
    # Extraction                                                         #
    # ------------------------------------------------------------------ #
    def _extract_bytes(self, raw: bytes, dest_dir: Path) -> int:
        """Extract the archive payload into dest_dir; return file count."""
        with tempfile.TemporaryDirectory(prefix="live_data_") as tmp:
            tmp_zip = Path(tmp) / "backup.zip"
            tmp_zip.write_bytes(raw)

            if not zipfile.is_zipfile(tmp_zip):
                raise RuntimeError("Backup payload is not a valid zip archive")

            # Stage into a temp folder first so a corrupt archive never
            # destroys the existing data tree.
            stage = Path(tmp) / "stage"
            stage.mkdir()
            with zipfile.ZipFile(tmp_zip) as zf:
                zf.extractall(stage)

            # Replace dest_dir atomically-ish.
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            dest_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(stage), str(dest_dir))

        return sum(1 for p in dest_dir.rglob("*") if p.is_file())

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def refresh(self) -> dict[str, Any]:
        """Fetch + extract the latest backup.

        Returns a summary dict — never raises for expected failure modes.
        """
        dest = Path(config.LIVE_DATA_DIR)
        summary: dict[str, Any] = {
            "status": "unknown",
            "extracted_to": str(dest),
            "file_count": 0,
            "archive_size_bytes": None,
            "file_link": None,
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
        }

        try:
            blob = self._blob_doc()
            if not blob:
                summary["status"] = "no_blob_doc"
                summary["error"] = (
                    f"blob doc {config.BACKUP_BLOB_DOC_ID!r} not found in "
                    f"{config.MONGO_DB}.{config.BACKUP_BLOB_COLLECTION}"
                )
                logger.warning(summary["error"])
                self._record(summary)
                return summary

            summary["file_link"] = blob.get("file_link")
            summary["archive_size_bytes"] = blob.get("archive_size_bytes")

            raw = blob.get("data")

            # bson.Binary is bytes-like; some setups may store as base64 str.
            if raw is None:
                summary["status"] = "blob_missing"
                summary["error"] = (
                    "Blob doc has no inline `data`. Archive was probably "
                    "too large for Mongo — download it manually from "
                    "`file_link` and extract into the data folder."
                )
                logger.warning(summary["error"])
                self._record(summary)
                return summary

            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            if not isinstance(raw, (bytes, bytearray)):
                # bson.Binary already behaves like bytes; anything else is a bug.
                raw = bytes(raw)

            file_count = self._extract_bytes(bytes(raw), dest)
            summary["status"] = "ok"
            summary["file_count"] = file_count
            summary["archive_size_bytes"] = len(raw)
            logger.info(
                "Backup extracted: files=%d bytes=%d dest=%s",
                file_count,
                len(raw),
                dest,
            )
            self._record(summary)
            return summary

        except Exception as exc:
            summary["status"] = "error"
            summary["error"] = f"{type(exc).__name__}: {exc}"
            logger.exception("Backup refresh failed")
            self._record(summary)
            return summary

    def last_status(self) -> dict[str, Any]:
        return {
            "status": self._last_status,
            "extracted_at": (
                self._last_extracted_at.isoformat()
                if self._last_extracted_at
                else None
            ),
            "archive_size_bytes": self._last_archive_size,
            "error": self._last_error,
            "data_dir": str(config.LIVE_DATA_DIR),
        }

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #
    def _record(self, summary: dict[str, Any]) -> None:
        self._last_status = str(summary.get("status") or "unknown")
        self._last_error = summary.get("error")
        self._last_archive_size = summary.get("archive_size_bytes")
        if summary.get("status") == "ok":
            self._last_extracted_at = datetime.now(timezone.utc)


backup_data_service = BackupDataService()