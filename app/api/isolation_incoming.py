# app/api/isolation_incoming.py
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Request

from ..services.isolation_store import IsolationStore
from ..services.isolation_validator import validate_isolation_payload

logger = logging.getLogger(__name__)

router = APIRouter()


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


@router.post("/isolation/incoming")
async def receive_isolation(
    request: Request,
    overwrite: bool = Query(
        False,
        description="Overwrite existing isolation data (Mongo + data/ mirror + WS)",
    ),
) -> dict:
    config = request.app.state.config

    # ---- auth
    expected = config.isolation_notify_client_key
    if expected:
        provided = request.headers.get(config.isolation_notify_client_key_header)
        if not provided or provided != expected:
            raise HTTPException(status_code=401, detail="invalid isolation key")

    # ---- parse
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    # ---- overwrite: query param OR body field (stripped before persisting)
    if isinstance(body, dict):
        body_overwrite = body.pop("overwrite", None)
        if isinstance(body_overwrite, (bool, str)):
            overwrite = overwrite or _coerce_bool(body_overwrite)

    # ---- validate
    errors = validate_isolation_payload(body)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    isolation_id = IsolationStore.make_isolation_id(
        body["trade_date_ist"], body["instrument_key"]
    )

    # ---- persist (fast)
    isolation_store: IsolationStore = request.app.state.isolation_store
    try:
        await asyncio.to_thread(
            isolation_store.upsert, dict(body), isolation_id, overwrite
        )
    except Exception:
        logger.exception("failed to persist isolation %s", isolation_id)
        raise HTTPException(status_code=500, detail="persistence error")

    # ---- subscribe (spawns asyncio.Tasks, returns quickly)
    subscriber = request.app.state.subscriber
    try:
        result = await subscriber.subscribe_isolation(
            body, isolation_id, overwrite=overwrite
        )
    except Exception:
        logger.exception("subscribe failed for %s", isolation_id)
        result = {
            "subscribed": [],
            "rejected_legs": ["isolated", "opposite"],
            "web_socket_sessions": {},
        }

    try:
        telegram = request.app.state.telegram
        if telegram is not None:
            await telegram.notify_isolation(
                isolation_id, body["instrument_key"]
            )
    except Exception:
        logger.exception("telegram isolation notify failed")

    return {
        "received": True,
        "isolation_id": isolation_id,
        "overwrite": overwrite,
        "subscribed": result.get("subscribed", []),
        "rejected_legs": result.get("rejected_legs", []),
        "web_socket_sessions": result.get("web_socket_sessions", {}),
    }