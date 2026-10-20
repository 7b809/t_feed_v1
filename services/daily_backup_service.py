"""Daily zip-and-upload-to-MEGA backup with MongoDB progress tracking.

Schedule
--------
Runs at ``DAILY_BACKUP_HOUR:DAILY_BACKUP_MINUTE`` on ``DAILY_BACKUP_DAYS``
(default **16:00 IST** every day) via the shared APScheduler.

What it does
------------
1. For each folder in ``DAILY_BACKUP_FOLDERS`` (default ``["data"]``):
     * Zip its contents into a temp ``<folder>_<YYYY-MM-DD>.zip``.
2. Log in to MEGA using ``MEGA_EMAIL`` / ``MEGA_PASSWORD`` from ``.env``.
3. Ensure the remote folder layout exists:

       <MEGA_BACKUP_ROOT>/<folder>/
           <folder>_<YYYY-MM-DD>.zip

   Default root: ``market_engine_backup``.
4. If a remote archive with the same name already exists, it is
   deleted before re-uploading, so a repeated manual run on the same
   day overwrites cleanly.
5. Capture each upload's public MEGA link.
6. Write a **progress-state document** to MongoDB under
   ``DAILY_BACKUP_STATE_COLLECTION`` (default ``daily_backup_state``)
   keyed by the date. One document per date, upserted on every run:

       {
         _id:              "2026-10-20",
         date:             "2026-10-20",
         status:           "uploaded" | "partial" | "failed" | "no_data",
         run_reason:       "scheduled" | "manual",
         root_folder:      "market_engine_backup",
         folders:          ["data"],
         files: [
           {
             folder:       "data",
             filename:     "data_2026-10-20.zip",
             size_bytes:   8806042,
             size_human:   "8.4 MB",
             mega_link:    "https://mega.nz/file/...",
             uploaded:     true,
             error:        null,
             remote_path:  "market_engine_backup/data/data_2026-10-20.zip",
             file_count:   3412,
           }
         ],
         total_size_bytes: ...,
         total_size_human: "...",
         updated_at:       ISODate(...),
         updated_at_ist:   "2026-10-20 16:00:22 IST",
         error:            null,
       }

7. Return a summary dict; the caller (task wrapper) sends Telegram.

Last-7-days history
-------------------
``daily_backup_service.get_recent_history(days=7)`` returns the last
``N`` date documents from MongoDB, newest first. This is what
``/admin/daily-backup-status`` renders.

Design notes
------------
* The zip is built **in a temp file on disk**, never fully in memory.
* Temp files are always deleted, even on failure.
* A non-blocking lock prevents scheduled and manual runs from
  overlapping.
* The service **never raises**; every failure mode is reflected in the
  returned summary and in the MongoDB state document.
* MEGA upload failures are recorded per-folder, so a partial run
  (data uploaded, logs failed) is still auditable from the state doc.
* The daily zip is **not** stored in ``backup_collection`` — that
  collection remains reserved for the candle-backup extract that is
  re-consumed on every refresh.

Typing note
-----------
``mega-sdk`` is an optional dependency at import time. To keep the
type annotations accurate without forcing an import failure on a
fresh checkout, ``Mega`` is imported **only for type checkers** (via
``TYPE_CHECKING``) at module scope, and imported **for real** inside
``_login()`` the moment the feature actually runs. That way:

* Pylance sees ``Mega`` as a real class → all ``"Mega"`` annotations
  resolve correctly → no "Variable not allowed in type expression".
* The runtime never touches ``mega-sdk`` until the daily backup runs,
  so a machine without it installed can still import the rest of the
  app.
"""

from __future__ import annotations

import os
import tempfile
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from pymongo import MongoClient

from core import config
from core.logger import get_logger

# --------------------------------------------------------------------------- #
# Optional runtime dependency — declared for the type checker only.           #
# The real import happens inside ``_login()`` (see its docstring).            #
# --------------------------------------------------------------------------- #
if TYPE_CHECKING:  # pragma: no cover - type-checker only
    from mega_sdk import Mega

logger = get_logger(__file__)

# --------------------------------------------------------------------------- #
# Small helpers                                                                #
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

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _now_ist_str() -> str:
    return datetime.now(_market_tz()).strftime("%Y-%m-%d %H:%M:%S IST")

def _today_iso() -> str:
    return datetime.now(_market_tz()).date().isoformat()

# --------------------------------------------------------------------------- #
# Service                                                                      #
# --------------------------------------------------------------------------- #
class DailyBackupService:
    """Zips configured folders, uploads them to MEGA, tracks state in Mongo."""

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
                    "MONGO_URL must be set to track daily backup progress"
                )
            self._client = MongoClient(config.MONGO_URI)
        return self._client

    def _state_collection(self):
        return self._mongo()[config.DAILY_BACKUP_MONGO_DB][
            config.DAILY_BACKUP_STATE_COLLECTION
        ]

    def _write_state(self, state: dict[str, Any]) -> None:
        try:
            self._state_collection().replace_one(
                {"_id": state["_id"]},
                state,
                upsert=True,
            )
        except Exception:
            logger.exception(
                "[daily-backup] failed writing state doc _id=%s",
                state.get("_id"),
            )

    # ------------------------------------------------------------------ #
    # Zip builder (per folder)                                           #
    # ------------------------------------------------------------------ #
    def _build_folder_zip(
        self,
        folder_name: str,
        date_str: str,
    ) -> dict[str, Any]:
        """Zip ``project/<folder_name>`` into a temp file.

        Returns a dict with:
            folder, filename, tmp_path, size_bytes, file_count,
            skipped (bool), reason (str, only when skipped).
        The caller is responsible for deleting ``tmp_path``.
        """
        root = Path(config.PROJECT_ROOT).resolve()
        folder_path = (root / folder_name).resolve()

        try:
            folder_path.relative_to(root)
        except ValueError:
            logger.warning(
                "[daily-backup] refusing %r (escapes project root)",
                folder_name,
            )
            return {
                "folder": folder_name,
                "filename": None,
                "tmp_path": None,
                "size_bytes": 0,
                "file_count": 0,
                "skipped": True,
                "reason": "escapes_project_root",
            }

        if not folder_path.exists() or not folder_path.is_dir():
            logger.warning("[daily-backup] folder missing: %s", folder_name)
            return {
                "folder": folder_name,
                "filename": None,
                "tmp_path": None,
                "size_bytes": 0,
                "file_count": 0,
                "skipped": True,
                "reason": "folder_missing",
            }

        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=f"daily_backup_{folder_name}_", suffix=".zip"
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        file_count = 0

        try:
            with zipfile.ZipFile(
                tmp_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                allowZip64=True,
            ) as zf:
                for current_root, dirs, files in os.walk(
                    folder_path, followlinks=False
                ):
                    cur = Path(current_root)
                    dirs[:] = [
                        d for d in dirs if not (cur / d).is_symlink()
                    ]
                    for f in files:
                        fp = cur / f
                        if f.endswith(".tmp"):
                            continue
                        try:
                            if fp.is_symlink():
                                continue
                            rel = fp.relative_to(folder_path)
                        except (OSError, ValueError):
                            continue
                        try:
                            zf.write(fp, arcname=str(rel))
                            file_count += 1
                        except OSError:
                            logger.warning(
                                "[daily-backup] skipping unreadable %s", fp
                            )

            size_bytes = tmp_path.stat().st_size
            return {
                "folder": folder_name,
                "filename": f"{folder_name}_{date_str}.zip",
                "tmp_path": tmp_path,
                "size_bytes": size_bytes,
                "file_count": file_count,
                "skipped": False,
            }
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ #
    # MEGA helpers                                                       #
    # ------------------------------------------------------------------ #
    def _login(self) -> "Mega":
        """Log in to MEGA and return a primed ``Mega`` client.

        The ``mega-sdk`` import is done **here**, not at module scope,
        so a checkout without ``mega-sdk`` installed can still import
        the rest of the application — only the daily backup run fails,
        with a clear error message.
        """
        try:
            from mega_sdk import Mega  # runtime import
        except ImportError as exc:
            raise RuntimeError(
                "mega-sdk is not installed; add `mega-sdk>=0.1.1` to "
                "requirements.txt"
            ) from exc

        email = (config.MEGA_EMAIL or "").strip()
        password = (config.MEGA_PASSWORD or "").strip()
        if not email or not password:
            raise RuntimeError(
                "MEGA_EMAIL / MEGA_PASSWORD are not configured in .env"
            )

        mega = Mega()
        mega.login(email, password)
        mega.get_files()  # primes mega.root_id
        return mega

    @staticmethod
    def _find_child_folder(
        files: dict[str, Any], parent_id: str, name: str
    ) -> str | None:
        for handle, node in files.items():
            if node.get("p") != parent_id:
                continue
            if node.get("t") != 1:  # folder
                continue
            attribs = node.get("a") or {}
            if attribs.get("n") == name:
                return handle
        return None

    @staticmethod
    def _find_child_file(
        files: dict[str, Any], parent_id: str, name: str
    ) -> str | None:
        for handle, node in files.items():
            if node.get("p") != parent_id:
                continue
            if node.get("t") != 0:  # file
                continue
            attribs = node.get("a") or {}
            if attribs.get("n") == name:
                return handle
        return None

    def _ensure_folder_path(
        self,
        mega: "Mega",
        parts: list[str],
    ) -> str:
        """Walk/create nested folders under the MEGA root; return leaf id."""
        parent_id = mega.root_id
        for name in parts:
            files = mega.get_files()
            existing = self._find_child_folder(files, parent_id, name)
            if existing:
                parent_id = existing
                continue
            parent_id = mega.create_folder_at(name, parent_id)
            logger.info(
                "[daily-backup] created MEGA folder %r under %s",
                name, parent_id,
            )
        return parent_id

    def _delete_existing_file(
        self,
        mega: "Mega",
        folder_id: str,
        filename: str,
    ) -> None:
        files = mega.get_files()
        handle = self._find_child_file(files, folder_id, filename)
        if not handle:
            return
        try:
            mega.destroy(handle)
            logger.info(
                "[daily-backup] deleted stale remote file %s", filename
            )
        except Exception:
            logger.exception(
                "[daily-backup] destroy failed for %s; falling back", filename
            )
            try:
                mega.delete(handle)
                mega.empty_trash()
            except Exception:
                logger.exception(
                    "[daily-backup] delete+empty_trash failed for %s", filename
                )

    def _upload_to_mega(
        self,
        mega: "Mega",
        local_path: Path,
        root_folder: str,
        sub_folder: str,
        filename: str,
    ) -> tuple[str | None, str]:
        """Upload one file, replacing any same-named file.

        Returns ``(public_link | None, remote_path)``.
        """
        leaf_id = self._ensure_folder_path(
            mega, [root_folder, sub_folder]
        )
        self._delete_existing_file(mega, leaf_id, filename)
        mega.upload(str(local_path), dest=leaf_id, dest_filename=filename)

        files = mega.get_files()
        link: str | None = None
        for handle, node in files.items():
            if node.get("p") != leaf_id:
                continue
            if (node.get("a") or {}).get("n") != filename:
                continue
            try:
                link = mega.get_link((handle, node))
            except Exception:
                logger.exception(
                    "[daily-backup] get_link failed for %s", filename
                )
            break
        return link, f"{root_folder}/{sub_folder}/{filename}"

    # ------------------------------------------------------------------ #
    # Public entry point                                                 #
    # ------------------------------------------------------------------ #
    def run(self, reason: str = "scheduled") -> dict[str, Any]:
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
                return self._record({
                    "status": "no_folders",
                    "reason": "DAILY_BACKUP_FOLDERS is empty",
                })

            if not config.MEGA_ENABLED:
                return self._record({
                    "status": "disabled",
                    "reason": "MEGA_ENABLED is False",
                })

            date_str = _today_iso()
            root_folder = config.MEGA_BACKUP_ROOT or "market_engine_backup"

            logger.info(
                "[daily-backup] START — reason=%s folders=%s date=%s root=%s",
                reason, folders, date_str, root_folder,
            )

            built: list[dict[str, Any]] = []
            tmp_paths: list[Path] = []
            files_result: list[dict[str, Any]] = []
            total_size = 0
            any_uploaded = False
            any_failed = False

            try:
                # ---- 1. Build zips ---------------------------------- #
                for folder_name in folders:
                    folder_name = str(folder_name).strip()
                    if not folder_name:
                        continue
                    info = self._build_folder_zip(folder_name, date_str)
                    if info.get("tmp_path"):
                        tmp_paths.append(info["tmp_path"])
                    built.append(info)

                # ---- 2. MEGA login ---------------------------------- #
                mega = self._login()
                logger.info(
                    "[daily-backup] MEGA login OK (root=%s)", root_folder
                )

                # ---- 3. Upload each zip ----------------------------- #
                for info in built:
                    folder_name = info["folder"]
                    if info.get("skipped"):
                        files_result.append({
                            "folder": folder_name,
                            "filename": None,
                            "size_bytes": 0,
                            "size_human": "0 B",
                            "mega_link": None,
                            "uploaded": False,
                            "error": info.get("reason") or "skipped",
                            "remote_path": None,
                            "file_count": 0,
                        })
                        continue

                    filename = info["filename"]
                    try:
                        link, remote_path = self._upload_to_mega(
                            mega,
                            info["tmp_path"],
                            root_folder,
                            folder_name,
                            filename,
                        )
                        files_result.append({
                            "folder": folder_name,
                            "filename": filename,
                            "size_bytes": info["size_bytes"],
                            "size_human": _human_size(info["size_bytes"]),
                            "mega_link": link,
                            "uploaded": True,
                            "error": None,
                            "remote_path": remote_path,
                            "file_count": info["file_count"],
                        })
                        total_size += info["size_bytes"]
                        any_uploaded = True
                        logger.info(
                            "[daily-backup] uploaded %s (%s) -> %s",
                            filename,
                            _human_size(info["size_bytes"]),
                            link,
                        )
                    except Exception as exc:
                        any_failed = True
                        files_result.append({
                            "folder": folder_name,
                            "filename": filename,
                            "size_bytes": info["size_bytes"],
                            "size_human": _human_size(info["size_bytes"]),
                            "mega_link": None,
                            "uploaded": False,
                            "error": f"{type(exc).__name__}: {exc}",
                            "remote_path": (
                                f"{root_folder}/{folder_name}/{filename}"
                            ),
                            "file_count": info["file_count"],
                        })
                        logger.exception(
                            "[daily-backup] upload failed for %s", filename
                        )

            finally:
                # Always delete temp zips.
                for p in tmp_paths:
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        logger.warning(
                            "[daily-backup] cleanup failed: %s", p
                        )

            # ---- 4. Overall status -------------------------------- #
            if any_failed and not any_uploaded:
                status = "failed"
            elif any_failed and any_uploaded:
                status = "partial"
            elif any_uploaded:
                status = "uploaded"
            else:
                status = "no_data"

            # ---- 5. Persist progress state ------------------------ #
            state = {
                "_id": date_str,
                "date": date_str,
                "status": status,
                "run_reason": reason,
                "root_folder": root_folder,
                "folders": folders,
                "files": files_result,
                "total_size_bytes": total_size,
                "total_size_human": _human_size(total_size),
                "updated_at": _now_utc(),
                "updated_at_ist": _now_ist_str(),
                "error": (
                    None
                    if status in {"uploaded", "partial"}
                    else (
                        "all folders failed to upload"
                        if status == "failed"
                        else None
                    )
                ),
            }
            self._write_state(state)

            summary = {
                "status": status,
                "reason": reason,
                "date": date_str,
                "root_folder": root_folder,
                "folders": folders,
                "files": files_result,
                "total_size_bytes": total_size,
                "total_size_human": _human_size(total_size),
                "uploaded_count": sum(
                    1 for f in files_result if f.get("uploaded")
                ),
                "failed_count": sum(
                    1 for f in files_result
                    if f.get("error") and not f.get("uploaded")
                ),
                "state_doc_id": date_str,
            }
            logger.info(
                "[daily-backup] COMPLETE — status=%s uploaded=%d failed=%d "
                "size=%s",
                status,
                summary["uploaded_count"],
                summary["failed_count"],
                summary["total_size_human"],
            )
            return self._record(summary)

        except Exception as exc:
            logger.exception("[daily-backup] run failed")
            date_str = _today_iso()
            try:
                self._write_state({
                    "_id": date_str,
                    "date": date_str,
                    "status": "failed",
                    "run_reason": reason,
                    "root_folder": config.MEGA_BACKUP_ROOT,
                    "folders": list(config.DAILY_BACKUP_FOLDERS),
                    "files": [],
                    "total_size_bytes": 0,
                    "total_size_human": "0 B",
                    "updated_at": _now_utc(),
                    "updated_at_ist": _now_ist_str(),
                    "error": f"{type(exc).__name__}: {exc}",
                })
            except Exception:
                pass
            return self._record({
                "status": "failed",
                "reason": reason,
                "error": f"{type(exc).__name__}: {exc}",
            })
        finally:
            self._lock.release()

    # ------------------------------------------------------------------ #
    # History / status accessors                                         #
    # ------------------------------------------------------------------ #
    def get_recent_history(
        self,
        days: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return the last ``days`` date-state docs from MongoDB.

        Newest first. Each item is a dict with ``date``, ``status``,
        ``files``, ``total_size_bytes``, ``updated_at``, ``error`` —
        exactly as stored in ``daily_backup_state``.
        """
        n = days or config.DAILY_BACKUP_STATE_DAYS
        try:
            cursor = (
                self._state_collection()
                .find()
                .sort("_id", -1)
                .limit(n)
            )
            out: list[dict[str, Any]] = []
            for doc in cursor:
                doc["_id"] = str(doc["_id"])
                if isinstance(doc.get("updated_at"), datetime):
                    doc["updated_at"] = doc["updated_at"].isoformat()
                out.append(doc)
            return out
        except Exception:
            logger.exception("[daily-backup] failed reading history")
            return []

    def last_status(self) -> dict[str, Any]:
        return dict(self._last_status)

    def _record(self, status: dict[str, Any]) -> dict[str, Any]:
        status.setdefault("ran_at", _now_utc().isoformat())
        self._last_status = status
        return status

daily_backup_service = DailyBackupService()