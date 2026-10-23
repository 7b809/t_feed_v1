# app/services/mega_archive_service.py
#
# Job 1 orchestrator (MongoDB -> MEGA ZIP -> safe extraction).
#
# Cleanup policy
# --------------
# * trigger="startup"   -> NO cleanup here. Lifespan already wiped the
#                          configured folders before this thread started.
# * trigger="scheduled" -> clean CLEANUP_DIRS, then download / extract.
# * trigger="manual"    -> clean CLEANUP_DIRS, then download / extract.
#
# Telegram notifications
# ----------------------
# When a ProjectNotifier is injected, this service sends:
#   * ``refresh started``   (right after job_state.start())
#   * ``refresh success``   (on the success path)
#   * ``refresh failed``    (on the failure path)
# Notifications are best-effort — a Telegram outage never aborts a
# refresh (the notifier itself swallows network errors).

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from app.config import Settings
from app.models.job_status import job_state
from app.services.archive_service import ArchiveService
from app.services.cleanup_service import clean_directories
from app.services.mega_download_service import (
    MegaDownloadError,
    MegaDownloadService,
    validate_mega_url,
)
from app.services.mongodb_service import MongoDocumentService

if TYPE_CHECKING:  # pragma: no cover
    from app.services.telegram_service import ProjectNotifier

logger = logging.getLogger(__name__)

class RefreshConflict(RuntimeError):
    pass

class RefreshError(RuntimeError):
    pass

class MegaArchiveService:
    def __init__(
        self,
        config: Settings,
        mongo: MongoDocumentService | None = None,
        notifier: "ProjectNotifier | None" = None,
    ):
        self.config = config
        self.mongo = mongo
        self.notifier = notifier
        self.downloader = MegaDownloadService(config)
        self.archive = ArchiveService(config)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def _notify(self, method: str, **kwargs) -> None:
        """Fire a notifier method, swallowing any exception."""
        if self.notifier is None:
            return
        try:
            getattr(self.notifier, method)(**kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("Telegram %s notification failed", method)

    # ------------------------------------------------------------------ #
    def refresh(self, trigger: str) -> dict:
        if trigger not in {"startup", "scheduled", "manual"}:
            raise ValueError("Unsupported trigger")
        if not self._lock.acquire(blocking=False):
            raise RefreshConflict("A refresh is already running")

        started = time.monotonic()
        job_id = job_state.start(trigger)
        logger.info("Job refresh started trigger=%s job_id=%s", trigger, job_id)
        self._notify("notify_refresh_started", trigger=trigger, job_id=job_id)

        try:
            # ------------------------------------------------------------------
            # Pre-refresh cleanup (skipped for startup — see module docstring).
            # ------------------------------------------------------------------
            if trigger != "startup":
                try:
                    removed = clean_directories(self.config, trigger=trigger)
                    if removed:
                        logger.info(
                            "Pre-refresh cleanup [%s] removed %d path(s): %s",
                            trigger,
                            len(removed),
                            removed,
                        )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Pre-refresh cleanup [%s] failed; continuing anyway",
                        trigger,
                    )

            if self.mongo is None:
                raise RefreshError("MongoDB service is not configured")

            document = self.mongo.get_source_document()

            file_link = document.get("file_link")
            if not isinstance(file_link, str) or not file_link:
                raise RefreshError("MongoDB source document is missing file_link")
            validate_mega_url(file_link)

            archive_name = document.get("archive_name") or self.config.mega_archive_filename
            if (
                not isinstance(archive_name, str)
                or Path(archive_name).name != archive_name
                or archive_name in {"", ".", ".."}
            ):
                raise RefreshError("archive_name must be a safe filename")

            expected_size = document.get("archive_size_bytes")
            if expected_size is not None and (
                not isinstance(expected_size, (int, float)) or expected_size < 0
            ):
                raise RefreshError("archive_size_bytes must be a non-negative number")

            archive_path = self.downloader.download(
                file_link,
                archive_name,
                int(expected_size) if expected_size is not None else None,
            )
            file_count, sha = self.archive.extract_and_replace(archive_path)
            size = archive_path.stat().st_size
            duration = time.monotonic() - started

            job_state.finish(
                success=True,
                duration=duration,
                filename=archive_path.name,
                size=size,
                count=file_count,
                destination=str(self.config.destination_dir),
                sha256=sha,
            )

            result = {
                "job_id": job_id,
                "state": "success",
                "trigger": trigger,
                "archive_filename": archive_path.name,
                "archive_size_bytes": size,
                "extracted_file_count": file_count,
                "sha256": sha,
                "destination": str(self.config.destination_dir),
                "duration_seconds": round(duration, 3),
            }
            logger.info(
                "Job refresh succeeded trigger=%s job_id=%s bytes=%s files=%s duration=%.3f",
                trigger,
                job_id,
                size,
                file_count,
                duration,
            )
            self._notify(
                "notify_refresh_success",
                trigger=trigger,
                job_id=job_id,
                archive_filename=archive_path.name,
                archive_size_bytes=size,
                extracted_file_count=file_count,
                sha256=sha,
                duration_seconds=duration,
                destination=str(self.config.destination_dir),
            )
            return result

        except Exception as exc:
            duration = time.monotonic() - started
            safe_message = str(exc)[:300] or type(exc).__name__
            job_state.finish(success=False, duration=duration, error=safe_message)
            logger.exception("Job refresh failed trigger=%s job_id=%s", trigger, job_id)
            self._notify(
                "notify_refresh_failed",
                trigger=trigger,
                job_id=job_id,
                duration_seconds=duration,
                error=safe_message,
            )
            raise RefreshError(safe_message) from exc

        finally:
            self._lock.release()