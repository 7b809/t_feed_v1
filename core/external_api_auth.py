"""Single shared-secret authentication for external order integrations."""
from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from core.config import EXTERNAL_API_AUTH_ENABLED, EXTERNAL_API_SOURCE_TOKEN


async def require_external_api_auth(request: Request) -> None:
    """Match payload.api_token against the single server-side source token."""
    if not EXTERNAL_API_AUTH_ENABLED:
        return

    expected = EXTERNAL_API_SOURCE_TOKEN
    if len(expected) != 32:
        raise HTTPException(
            status_code=503,
            detail="External API authentication is enabled but EXTERNAL_API_SOURCE_TOKEN is not configured as a 32-character token.",
        )

    try:
        payload = await request.json()
    except Exception:
        payload = None

    supplied = payload.get("api_token") if isinstance(payload, dict) else None
    if not isinstance(supplied, str) or len(supplied) != 32:
        raise HTTPException(
            status_code=401,
            detail="A valid 32-character api_token is required in the JSON request payload.",
        )

    if not hmac.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="Invalid external API token.")
