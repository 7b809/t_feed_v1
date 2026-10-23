from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from threading import Lock
from typing import Any
import uuid


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobSnapshot:
    job_name: str = "mega-archive"
    state: str = "idle"
    last_trigger: str | None = None
    last_job_id: str | None = None
    last_start_time: str | None = None
    last_completion_time: str | None = None
    last_duration_seconds: float | None = None
    last_successful_refresh_time: str | None = None
    last_error_summary: str | None = None
    archive_filename: str | None = None
    downloaded_archive_size_bytes: int | None = None
    extracted_file_count: int | None = None
    extracted_destination: str | None = None
    archive_sha256: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobState:
    def __init__(self) -> None:
        self._lock = Lock()
        self._data = JobSnapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._data.public_dict()

    def start(self, trigger: str) -> str:
        with self._lock:
            job_id = str(uuid.uuid4())
            self._data.state = "running"
            self._data.last_trigger = trigger
            self._data.last_job_id = job_id
            self._data.last_start_time = utc_now()
            self._data.last_completion_time = None
            self._data.last_duration_seconds = None
            self._data.last_error_summary = None
            return job_id

    def finish(self, *, success: bool, duration: float, filename: str | None = None,
               size: int | None = None, count: int | None = None,
               destination: str | None = None, sha256: str | None = None,
               error: str | None = None) -> None:
        with self._lock:
            self._data.state = "success" if success else "failed"
            self._data.last_completion_time = utc_now()
            self._data.last_duration_seconds = round(duration, 3)
            self._data.last_error_summary = error
            if success:
                self._data.last_successful_refresh_time = self._data.last_completion_time
                self._data.archive_filename = filename
                self._data.downloaded_archive_size_bytes = size
                self._data.extracted_file_count = count
                self._data.extracted_destination = destination
                self._data.archive_sha256 = sha256


job_state = JobState()
