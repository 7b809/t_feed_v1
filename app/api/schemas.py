from pydantic import BaseModel
from typing import Any
class ActionResponse(BaseModel):
    status: str; changed: bool
class HealthResponse(BaseModel):
    status: str; in_market_hours: bool; supervisor_running: bool; instruments: int
    active_workers: int; connected_workers: int; total_messages_written: int; last_message_at: str|None
class StatusResponse(BaseModel):
    in_market_hours: bool; next_wakeup_at: str|None; instruments: int; workers: list[dict[str,Any]]
