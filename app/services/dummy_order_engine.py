"""First post-isolation SR and Supertrend dummy-entry logic.

Only local simulation records are created. This module has no broker client.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo
from ..config import Settings
from .dummy_order_store import DummyOrderStore
from .live_feed_state_store import LiveFeedStateStore

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
SR_TIMES = ("confirmed_time", "confirmed_at", "confirmation_time", "confirmation_timestamp",
            "confirmed_timestamp", "confirmed_candle_time", "confirmation_candle_time",
            "confirmed_candle_timestamp", "confirmed_ts", "confirmation_ts")
SR_PRICES = ("price", "level_price", "sr_price", "pivot_price", "level", "value")

def _now() -> datetime:
    return datetime.now(IST)

def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=IST)
    return result.astimezone(IST)

def _text_time(value: Any) -> str | None:
    parsed = _dt(value)
    return parsed.isoformat() if parsed else (str(value) if value else None)

def _first(obj: dict, keys: tuple) -> Any:
    return next((obj[k] for k in keys if obj.get(k) not in (None, "")), None)

def _kind(item: dict, inferred: str | None = None) -> str | None:
    raw = _first(item, ("level_type", "sr_type", "type", "kind", "category", "side")) or inferred
    value = str(raw or "").strip().lower()
    if value in {"r", "res", "resistance"} or "resistance" in value:
        return "RESISTANCE"
    if value in {"s", "sup", "support"} or "support" in value:
        return "SUPPORT"
    return None

def _sr_candidates(payload: dict) -> list[dict]:
    found = []
    def add(item: Any, inferred: str | None = None):
        if not isinstance(item, dict):
            return
        kind, ts, price = _kind(item, inferred), _first(item, SR_TIMES), _first(item, SR_PRICES)
        parsed = _dt(ts)
        if kind and parsed and price is not None:
            found.append({"kind": kind, "time": _text_time(ts), "dt": parsed,
                          "price": price, "raw": item})
    for name in ("levels", "confirmed_levels", "new_levels"):
        if isinstance(payload.get(name), list):
            for item in payload[name]:
                add(item)
    for name, kind in (("resistances", "RESISTANCE"), ("supports", "SUPPORT")):
        if isinstance(payload.get(name), list):
            for item in payload[name]:
                add(item, kind)
    for name in ("confirmed_level", "level"):
        add(payload.get(name))
    return found

def _direction(value: Any) -> str | None:
    value = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if value in {"BULLISH", "BULL", "BUY", "UP", "UPTREND", "GREEN"}:
        return "BULLISH"
    if value in {"BEARISH", "BEAR", "SELL", "DOWN", "DOWNTREND", "RED"}:
        return "BEARISH"
    return None

def _st_candidates(payload: dict) -> list[dict]:
    items = list(payload.get("signals") or []) if isinstance(payload.get("signals"), list) else []
    if payload.get("event") == "supertrend_signal" or payload.get("signal"):
        items.append(payload)
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ts = _first(item, ("confirmed_time", "confirmed_at", "confirmation_time", "timestamp"))
        direction = _direction(item.get("signal") or item.get("trend"))
        parsed = _dt(ts)
        if direction and parsed:
            result.append({"direction": direction, "time": _text_time(ts), "dt": parsed, "raw": item})
    return result

def _price(value: Any) -> float | None:
    if isinstance(value, dict):
        for key in ("ltp", "last_price", "lastPrice", "price", "close"):
            v = value.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                return float(v)
        for child in value.values():
            result = _price(child)
            if result is not None:
                return result
    elif isinstance(value, list):
        for child in value:
            result = _price(child)
            if result is not None:
                return result
    return None

class DummyOrderEngine:
    def __init__(self, config: Settings, store: DummyOrderStore,
                 live_feed_store: LiveFeedStateStore, telegram: Any = None) -> None:
        self._config, self._store = config, store
        self._live_feed, self._telegram = live_feed_store, telegram
        self._meta: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def register_isolation(self, isolation_id: str, payload: dict,
                                 legs: dict[str, str], overwrite: bool = False) -> None:
        if not self._config.dummy_orders_enabled:
            return
        async with self._lock(isolation_id):
            old = await asyncio.to_thread(self._store.load, isolation_id)
            if overwrite:
                await asyncio.to_thread(self._store.delete, isolation_id)
                old = None
            if old is None or old.get("legs") != legs:
                iso_time = next((payload.get(k) for k in
                    ("isolation_time", "isolated_at", "isolation_timestamp",
                     "created_at", "received_at", "timestamp") if payload.get(k)), None)
                state = {
                    "schema_version": 1, "isolation_id": isolation_id,
                    "trade_date_ist": payload.get("trade_date_ist"),
                    "isolation_time": _text_time(iso_time) or _now().isoformat(),
                    "legs": dict(legs), "trading_symbol": payload.get("trading_symbol"),
                    "status": "ACTIVE", "orders": {"sr": None, "supertrend": None},
                    "created_at": _now().isoformat(), "updated_at": _now().isoformat(),
                }
            else:
                state = old
                state["status"] = "ACTIVE"
                state["trading_symbol"] = payload.get("trading_symbol") or state.get("trading_symbol")
                state["updated_at"] = _now().isoformat()
            await asyncio.to_thread(self._store.save, isolation_id, state)
            self._meta[isolation_id] = {
                "legs": dict(legs), "cutoff": _dt(state.get("isolation_time")) or _now()
            }

    async def mark_closed(self, isolation_id: str) -> None:
        state = await asyncio.to_thread(self._store.load, isolation_id)
        if state:
            state["status"], state["updated_at"] = "CLOSED", _now().isoformat()
            await asyncio.to_thread(self._store.save, isolation_id, state)

    async def on_sr_frame(self, payload: dict, instrument_key: str,
                          leg_role: str, isolation_id: str) -> None:
        if not self._config.dummy_orders_enabled or payload.get("event") not in {"snapshot", "sr_update"}:
            return
        if leg_role != "isolated":
            return
        try:
            await self._process("sr", payload, instrument_key, leg_role, isolation_id)
        except Exception:
            logger.exception("Dummy SR entry failed for %s", isolation_id)

    async def on_supertrend_frame(self, payload: dict, instrument_key: str,
                                  leg_role: str, isolation_id: str) -> None:
        if not self._config.dummy_orders_enabled or payload.get("event") not in {"snapshot", "supertrend_signal"}:
            return
        if leg_role != "isolated":
            return
        try:
            await self._process("supertrend", payload, instrument_key, leg_role, isolation_id)
        except Exception:
            logger.exception("Dummy Supertrend entry failed for %s", isolation_id)

    async def _process(self, indicator: str, payload: dict, source_key: str,
                       source_role: str, isolation_id: str) -> None:
        async with self._lock(isolation_id):
            state = await asyncio.to_thread(self._store.load, isolation_id)
            if not state or state.get("status") != "ACTIVE":
                return
            if (state.get("orders") or {}).get(indicator):
                return
            meta = self._meta.get(isolation_id) or {
                "legs": state.get("legs") or {},
                "cutoff": _dt(state.get("isolation_time")) or _now(),
            }
            if indicator == "sr":
                eligible = [x for x in _sr_candidates(payload) if x["dt"] > meta["cutoff"]]
                if not eligible:
                    return
                signal = min(eligible, key=lambda x: x["dt"])
                role = "opposite" if signal["kind"] == "RESISTANCE" else "isolated"
                trigger = {"level_type": signal["kind"], "level_price": signal["price"],
                           "confirmed_time": signal["time"], "config": payload.get("config"),
                           "source_event": payload.get("event")}
                direction = signal["kind"]
            else:
                eligible = [x for x in _st_candidates(payload) if x["dt"] > meta["cutoff"]]
                if not eligible:
                    return
                signal = min(eligible, key=lambda x: x["dt"])
                role = "isolated" if signal["direction"] == "BULLISH" else "opposite"
                raw = signal["raw"]
                trigger = {"direction": signal["direction"], "signal": raw.get("signal") or raw.get("trend"),
                           "signal_price": raw.get("price"),
                           "confirmed_time_price": raw.get("confirmed_time_price"),
                           "confirmed_time": signal["time"], "source_event": payload.get("event")}
                direction = signal["direction"]
            target_key = meta["legs"].get(role)
            if not target_key:
                logger.warning("Dummy order target role %s missing for %s", role, isolation_id)
                return
            quote = await asyncio.to_thread(self._live_feed.latest, target_key)
            quote_received_at = (quote or {}).get("received_at")
            quote_time = _dt(quote_received_at)
            trade_date = state.get("trade_date_ist") or isolation_id.split("__", 1)[0]
            # latest.json survives restarts; never use a previous-session
            # quote as today's simulated entry reference.
            quote_is_today = bool(
                quote_time and quote_time.strftime("%Y-%m-%d") == trade_date
            )
            entry = _price((quote or {}).get("data") or {}) if quote_is_today else None
            if entry is None and target_key == source_key:
                fallback = trigger.get("signal_price", payload.get("current_price"))
                if isinstance(fallback, (int, float)) and fallback > 0:
                    entry = float(fallback)
            order = {
                "order_id": f"DUMMY-{indicator.upper()}-{isolation_id}",
                "mode": "DUMMY_ONLY", "indicator": indicator.upper(), "side": "BUY",
                "signal_direction": direction, "source_instrument_key": source_key,
                "source_leg_role": source_role, "target_instrument_key": target_key,
                "target_leg_role": role, "quantity": self._config.dummy_order_quantity,
                "entry_price": entry,
                "price_source": ("latest_local_market_feed" if quote and entry is not None else
                                 "indicator_payload_fallback" if entry is not None else None),
                "quote_received_at": quote_received_at if quote_is_today else None,
                "quote_age_seconds": round((_now() - quote_time).total_seconds(), 3) if quote_is_today else None,
                "trigger": trigger, "placed_at": _now().isoformat(),
                "status": "DUMMY_PLACED" if entry is not None else "DUMMY_SIGNAL_RECORDED_NO_QUOTE",
                "broker_order_id": None, "real_order_submitted": False,
            }
            state.setdefault("orders", {})[indicator] = order
            state["updated_at"] = _now().isoformat()
            await asyncio.to_thread(self._store.save, isolation_id, state)
            logger.info("Dummy %s entry saved iso=%s target=%s trigger=%s",
                        indicator, isolation_id, target_key, trigger.get("confirmed_time"))
            await self._notify(state)

    async def _notify(self, state: dict) -> None:
        if self._telegram is None:
            return
        lines = [
            "🧪 DUMMY ORDER UPDATE", f"Isolation: {state.get('isolation_id')}",
            f"Trade date (IST): {state.get('trade_date_ist') or '—'}",
            f"Isolation time: {state.get('isolation_time') or '—'}", "",
        ]
        for key, title in (("sr", "SR-BASED ENTRY"), ("supertrend", "SUPERTREND ENTRY")):
            order = (state.get("orders") or {}).get(key)
            lines.append(f"━━ {title} ━━")
            if not order:
                lines.append("Status: WAITING FOR FIRST CONFIRMED SIGNAL")
            else:
                trigger = order.get("trigger") or {}
                lines.extend([
                    f"Status: {order.get('status')}",
                    f"Signal: {order.get('signal_direction')}",
                    f"Confirmed candle: {trigger.get('confirmed_time') or '—'}",
                    f"Target: BUY {order.get('target_leg_role')} ({order.get('target_instrument_key')})",
                    f"Quantity: {order.get('quantity')}",
                    f"Entry reference: {order.get('entry_price') if order.get('entry_price') is not None else 'QUOTE NOT AVAILABLE'}",
                ])
                if trigger.get("level_price") is not None:
                    lines.append(f"SR level: {trigger['level_price']} | config: {trigger.get('config') or '—'}")
                if trigger.get("signal_price") is not None:
                    lines.append(f"Signal price: {trigger['signal_price']}")
            lines.append("")
        lines.extend(["Execution: LOCAL SIMULATION ONLY",
                      "No broker API call or real order was submitted."])
        try:
            await self._telegram.send_message("Dummy order update", "\n".join(lines), level="SUCCESS")
        except Exception:
            logger.exception("Dummy order Telegram send failed")

    async def get_orders(self, isolation_id: str | None = None) -> Any:
        if isolation_id:
            return await asyncio.to_thread(self._store.load, isolation_id)
        return await asyncio.to_thread(self._store.list_all)

    async def status(self) -> dict:
        result = await asyncio.to_thread(self._store.status)
        result["enabled"] = self._config.dummy_orders_enabled
        return result
