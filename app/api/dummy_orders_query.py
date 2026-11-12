from __future__ import annotations
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

@router.get("/dummy-orders/status")
async def dummy_orders_status(request: Request) -> dict:
    return await request.app.state.dummy_order_engine.status()

@router.get("/dummy-orders")
async def list_dummy_orders(request: Request) -> list:
    return await request.app.state.dummy_order_engine.get_orders()

@router.get("/dummy-orders/{isolation_id}")
async def get_dummy_orders(isolation_id: str, request: Request) -> dict:
    state = await request.app.state.dummy_order_engine.get_orders(isolation_id)
    if state is None:
        raise HTTPException(status_code=404, detail="no dummy-order state for isolation")
    return state
