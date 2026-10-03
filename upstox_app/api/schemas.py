"""
upstox_app/schemas.py
Request / response models for the streamer API.

Responses never expose the access token.
"""
from typing import List, Optional

from pydantic import BaseModel, Field

# ── Requests ─────────────────────────────────────────────────────
class SubscribeRequest(BaseModel):
    instrument_keys: List[str] = Field(..., min_length=1, description="e.g. ['NSE_EQ|INE020B01018']")
    mode: str = Field("full", description="ltpc | full | option_greeks | full_d30")

class UnsubscribeRequest(BaseModel):
    instrument_keys: List[str] = Field(..., min_length=1)

class ChangeModeRequest(BaseModel):
    instrument_keys: List[str] = Field(..., min_length=1)
    mode: str = Field(..., description="ltpc | full | option_greeks | full_d30")

class ReconnectRequest(BaseModel):
    auto_reconnect_enabled: bool = True
    interval_seconds: Optional[int] = None
    retry_count: Optional[int] = None

# ── Responses ────────────────────────────────────────────────────
class StreamerStatus(BaseModel):
    name: str
    connected: bool
    subscribed_count: int
    subscribed_keys: List[str]
    message_buffer_size: int
    message_count: int

class StreamerListStatus(BaseModel):
    market: StreamerStatus
    portfolio: StreamerStatus

class ActionResponse(BaseModel):
    ok: bool
    message: str
    requested: Optional[List[str]] = None
    applied: Optional[List[str]] = None
    skipped: Optional[List[str]] = None
    status: Optional[StreamerStatus] = None

class MessagesResponse(BaseModel):
    streamer: str
    returned: int
    messages: List[dict]

class ConnectionStateResponse(BaseModel):
    connected: bool
    name: str

class WSStatusResponse(BaseModel):
    """Snapshot of downstream WebSocket server state."""
    clients: int
    upstream_subscribed_keys: List[str]