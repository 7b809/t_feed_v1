"""Read-only dashboard data APIs for live feed, isolations and dummy orders."""
from __future__ import annotations
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any
from ..services.market_session import market_session_status
from fastapi import APIRouter, Request

router = APIRouter()

def _first_text(source: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return str(value)
    return None

def _instrument_details(document: dict[str, Any], role: str) -> dict[str, Any]:
    source = document if role == "isolated" else (document.get("opposite_leg") or {}) if role == "opposite" else {}
    meta = source.get("meta") if isinstance(source.get("meta"), dict) else {}
    symbol = _first_text(source, "trading_symbol", "tradingsymbol", "symbol", "instrument_name")
    if not symbol and role == "underlying":
        symbol = "NIFTY 50"
    return {
        "trading_symbol": symbol or role.replace("_", " ").title(),
        "strike": source.get("strike") or source.get("strike_price") or meta.get("strike_price"),
        "option_type": source.get("option_type") or source.get("optionType") or meta.get("strike_type"),
        "expiry": source.get("expiry") or source.get("expiry_date") or meta.get("expiry"),
        "lot_size": source.get("lot_size") or meta.get("lot_size"),
        "exchange": source.get("exchange") or meta.get("exchange"),
        "segment": source.get("segment") or meta.get("segment"),
    }

def _connection(client: Any, key: str, role: str) -> dict[str, Any]:
    if client is None:
        return {"instrument_key": key, "leg_role": role, "ws_state": "idle",
                "last_frame_at": None, "last_error": None, "reconnect_attempts": 0}
    try:
        return client.status()
    except Exception as exc:
        return {"instrument_key": key, "leg_role": role, "ws_state": "unknown",
                "last_frame_at": None, "last_error": str(exc), "reconnect_attempts": 0}

async def _build_instruments(request: Request) -> tuple[list[dict], list[dict]]:
    subscriber = request.app.state.subscriber
    isolation_store = request.app.state.isolation_store
    live_status = subscriber.get_live_feed_client().status()
    instrument_map: dict[str, dict[str, Any]] = {}
    isolation_items: list[dict[str, Any]] = []
    for isolation_id in subscriber.list_isolation_ids():
        document = await asyncio.to_thread(isolation_store.get, isolation_id)
        if not isinstance(document, dict):
            continue
        isolated_key = document.get("instrument_key")
        opposite_key = (document.get("opposite_leg") or {}).get("instrument_key")
        legs = []
        for role, key in (("isolated", isolated_key), ("opposite", opposite_key)):
            if not key:
                continue
            details = _instrument_details(document, role)
            item = instrument_map.setdefault(str(key), {
                "instrument_key": str(key), **details, "roles": [], "isolation_ids": [],
            })
            item["roles"] = sorted(set(item["roles"]) | {role})
            item["isolation_ids"] = sorted(set(item["isolation_ids"]) | {isolation_id})
            legs.append({
                "role": role, "instrument_key": str(key), **details,
                "sr": _connection(subscriber.get_client(str(key)), str(key), role),
                "supertrend": _connection(subscriber.get_supertrend_client(str(key)), str(key), role),
            })
        isolation_items.append({
            "isolation_id": isolation_id, "trade_date_ist": document.get("trade_date_ist"),
            "isolation_time": document.get("isolation_time"),
            "status": document.get("consumer_status") or document.get("status") or "unknown",
            "received_at": document.get("received_at"), "instrument_key": isolated_key,
            "trading_symbol": _instrument_details(document, "isolated")["trading_symbol"],
            "legs": legs,
        })
    nifty_key = request.app.state.config.nifty_instrument_key
    desired_keys = set(live_status.get("desired_instrument_keys") or [])
    if nifty_key in desired_keys:
        instrument_map.setdefault(nifty_key, {
            "instrument_key": nifty_key, **_instrument_details({}, "underlying"),
            "roles": ["underlying"], "isolation_ids": [],
        })
    candle_store = request.app.state.live_feed_candle_store
    instruments = []
    for key, item in instrument_map.items():
        tick = await asyncio.to_thread(candle_store.latest_tick, key)
        item["ltp"] = tick.get("ltp") if tick else None
        item["last_tick_at"] = tick.get("last_tick_at") if tick else None
        item["tick_date"] = tick.get("date") if tick else None
        item["feed_details"] = tick or {}
        instruments.append(item)
    instruments.sort(key=lambda x: (0 if "underlying" in x["roles"] else 1, x["trading_symbol"]))
    return instruments, isolation_items

@router.get("/dashboard/home")
async def dashboard_home(request: Request) -> dict:
    subscriber = request.app.state.subscriber
    live = subscriber.get_live_feed_client().status()
    instruments, isolations = await _build_instruments(request)
    orders = _today_orders(_flatten_orders(await request.app.state.dummy_order_engine.get_orders()))
    session = market_session_status(request.app.state.config)
    return {"market_session": session, "summary": {
        "active_isolations": len(isolations),
        "subscribed_instruments": len(live.get("desired_instrument_keys") or []),
        "instruments_with_ltp": sum(1 for item in instruments if item.get("ltp") is not None),
        "open_orders": sum(1 for order in orders if order.get("status") == "DUMMY_PLACED"),
    }, "live_feed": live, "instruments": instruments}

@router.get("/dashboard/isolations")
async def dashboard_isolations(request: Request) -> dict:
    instruments, isolations = await _build_instruments(request)
    return {"market_session": market_session_status(request.app.state.config),
            "count": len(isolations), "isolations": isolations, "instruments": instruments}

@router.get("/dashboard/session")
async def dashboard_session(request: Request) -> dict:
    return {"market_session": market_session_status(request.app.state.config)}

@router.get("/dashboard/orders")
async def dashboard_orders(request: Request) -> dict:
    orders = _today_orders(_flatten_orders(await request.app.state.dummy_order_engine.get_orders()))
    completed = [x for x in orders if str(x.get("status", "")).startswith("COMPLETED")]
    pnl_values = [float(x["pnl_net"]) for x in completed if isinstance(x.get("pnl_net"), (int, float))]
    return {"summary": {
        "total_orders": len(orders),
        "open_orders": sum(1 for x in orders if x.get("status") == "DUMMY_PLACED"),
        "completed_orders": len(completed),
        "target_hits": sum(1 for x in completed if x.get("exit_reason") == "TARGET"),
        "stop_losses": sum(1 for x in completed if x.get("exit_reason") == "STOP_LOSS"),
        "realized_pnl": round(sum(pnl_values), 2) if pnl_values else None,
    }, "orders": orders}

def _today_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    return [item for item in orders if str(
        item.get("trade_date_ist") or item.get("trade_date") or
        (item.get("isolation_id") or "").split("__", 1)[0]
    )[:10] == today]

def _flatten_orders(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: dict[str, dict[str, Any]] = {}
    for state in states or []:
        isolation_id = state.get("isolation_id")
        for order in state.get("order_history") or []:
            if isinstance(order, dict):
                item = {**order, "isolation_id": isolation_id or order.get("isolation_id")}
                item["strategy"] = str(item.get("indicator") or item.get("strategy") or "unknown").upper()
                identity = str(item.get("order_id") or f"{isolation_id}:{item['strategy']}:{item.get('entry_time_ist')}")
                flattened[identity] = item
        for indicator, order in (state.get("orders") or {}).items():
            if isinstance(order, dict):
                item = {**order, "isolation_id": isolation_id or order.get("isolation_id")}
                item["strategy"] = str(item.get("indicator") or indicator or "unknown").upper()
                identity = str(item.get("order_id") or f"{isolation_id}:{indicator}:{item.get('entry_time_ist')}")
                flattened[identity] = item
    result = list(flattened.values())
    result.sort(key=lambda x: str(x.get("completed_at_ist") or x.get("entry_time_ist") or x.get("started_at_ist") or ""), reverse=True)
    return result
