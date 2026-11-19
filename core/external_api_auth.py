"""Shared-secret authentication for external order integrations."""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from core.config import (
    EXTERNAL_API_AUTH_ENABLED,
    EXTERNAL_API_DESTINATION_TOKEN,
    EXTERNAL_API_SOURCE_TOKEN,
)


def require_external_api_auth(
    source_token: str | None = Header(default=None, alias="X-Source-Token"),
    destination_token: str | None = Header(default=None, alias="X-Destination-Token"),
) -> None:
    """Require independently configured 32-character source and destination tokens.

    This dependency is attached only to /api/external/v1 mutation routes. The
    dashboard's same-origin /api/v1 routes intentionally do not use browser-held
    secrets.
    """
    if not EXTERNAL_API_AUTH_ENABLED:
        return

    configured = (EXTERNAL_API_SOURCE_TOKEN, EXTERNAL_API_DESTINATION_TOKEN)
    if any(len(value) != 32 for value in configured):
        raise HTTPException(
            status_code=503,
            detail="External API authentication is enabled but its server tokens are not configured correctly.",
        )

    if not source_token or not destination_token:
        raise HTTPException(
            status_code=401,
            detail="Both X-Source-Token and X-Destination-Token headers are required.",
        )

    if len(source_token) != 32 or len(destination_token) != 32:
        raise HTTPException(status_code=401, detail="Invalid external API credentials.")

    source_ok = hmac.compare_digest(source_token, EXTERNAL_API_SOURCE_TOKEN)
    destination_ok = hmac.compare_digest(
        destination_token, EXTERNAL_API_DESTINATION_TOKEN
    )
    if not (source_ok and destination_ok):
        raise HTTPException(status_code=401, detail="Invalid external API credentials.")
