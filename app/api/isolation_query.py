# app/api/isolation_query.py
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Request

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/isolation/current")
async def get_current(request: Request) -> dict:
    store = request.app.state.isolation_store
    doc = await asyncio.to_thread(store.get_current)
    if not doc:
        raise HTTPException(status_code=404, detail="no isolation found")
    return doc


@router.get("/isolation/history")
async def get_history(
    request: Request,
    limit: int = Query(20, ge=1, le=200),
) -> list:
    store = request.app.state.isolation_store
    docs = await asyncio.to_thread(store.list_history, limit)
    return docs