from datetime import datetime
from typing import Literal
from pydantic import BaseModel

class UpdateAccepted(BaseModel):
    job_id: str
    project: str
    status: Literal["queued"] = "queued"

class JobStatus(BaseModel):
    job_id: str
    project: str
    status: Literal["queued", "running", "succeeded", "failed", "timed_out"]
    requested_by: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    return_code: int | None = None
    output_tail: str = ""
    error: str | None = None
