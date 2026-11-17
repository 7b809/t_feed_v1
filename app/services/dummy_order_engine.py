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
    if value in {"BULLISH", "BULL", "BUY", "UP", "UPTREND", "GREEN", "SUPPORT"}:
        return "BULLISH"
    if value in {"BEARISH", "BEAR", "SELL", "DOWN", "DOWNTREND", "RED", "RESISTANCE"}:
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


def _market_ltp(value: Any) -> float | None:
    """Read only traded LTP fields, never bid/ask/volume values."""
    if isinstance(value, dict):
        for key in ("ltp", "last_price", "lastPrice"):
            candidate = value.get(key)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool) and candidate > 0:
                return float(candidate)
        for child in value.values():
            result = _market_ltp(child)
            if result is not None:
                return result
    elif isinstance(value, list):
        for child in value:
            result = _market_ltp(child)
            if result is not None:
                return result
    return None


class DummyOrderEngine:
    def __init__(self, config: Settings, store: DummyOrderStore,
                 live_feed_store: LiveFeedStateStore, telegram: Any = None,
                 mongo_store: Any = None) -> None:
        self._config, self._store = config, store
        self._live_feed, self._telegram = live_feed_store, telegram
        self._mongo_store = mongo_store
        self._meta: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._watch_index: dict[str, set[tuple[str, str]]] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    @staticmethod
    def _symbol(instrument: Any) -> str | None:
        if not isinstance(instrument, dict):
            return None
        for field in ("trading_symbol", "tradingsymbol", "symbol", "instrument_name"):
            value = instrument.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _indicator_enabled(self, indicator: str) -> bool:
        if not getattr(self._config, "dummy_orders_enabled", True):
            return False
        field = "dummy_sr_orders_enabled" if indicator == "sr" else "dummy_supertrend_orders_enabled"
        return bool(getattr(self._config, field, True))

    def _date_key(self, payload: dict, isolation_id: str) -> str:
        raw = payload.get("trade_date_ist") or isolation_id.split("__", 1)[0]
        try:
            return datetime.fromisoformat(str(raw)[:10]).strftime("%Y-%m-%d")
        except ValueError:
            return _now().strftime("%Y-%m-%d")

    def _instrument_context(self, payload: dict, legs: dict[str, str],
                            isolation_id: str, iso_time: str) -> dict:
        opposite = payload.get("opposite_leg") or {}
        return {
            "isolation_id": isolation_id,
            "instrument_key": legs.get("isolated"),
            "trading_symbol": self._symbol(payload),
            "opposite_instrument_key": legs.get("opposite"),
            "opposite_trading_symbol": self._symbol(opposite),
            "isolated_time_ist": iso_time,
        }

    def _target_points(self, entry: float) -> float:
        if entry > float(getattr(self._config, "dummy_target_upper_threshold", 200)):
            return float(getattr(self._config, "dummy_target_points_above_upper", 50))
        if entry >= float(getattr(self._config, "dummy_target_lower_threshold", 100)):
            return float(getattr(self._config, "dummy_target_points_middle", 30))
        return float(getattr(self._config, "dummy_target_points_below_lower", 15))

    async def _persist_mongo_isolation(self, date_key: str, isolation_id: str,
                                       isolated_instrument: dict) -> None:
        if self._mongo_store is None:
            return
        try:
            await asyncio.to_thread(
                self._mongo_store.save_isolation, date_key, isolation_id, isolated_instrument
            )
        except Exception:
            logger.exception("Mongo dummy-order isolation persistence failed date=%s iso=%s",
                             date_key, isolation_id)

    async def _persist_mongo_order(self, date_key: str, isolation_id: str,
                                   indicator: str, order: dict,
                                   isolated_instrument: dict) -> None:
        if self._mongo_store is None:
            return
        try:
            await asyncio.to_thread(
                self._mongo_store.save_order, date_key, isolation_id,
                indicator, order, isolated_instrument
            )
        except Exception:
            logger.exception("Mongo dummy-order persistence failed date=%s iso=%s indicator=%s",
                             date_key, isolation_id, indicator)

    def _index_open_orders(self, isolation_id: str, state: dict) -> None:
        for key in list(self._watch_index):
            self._watch_index[key] = {item for item in self._watch_index[key] if item[0] != isolation_id}
            if not self._watch_index[key]:
                self._watch_index.pop(key, None)
        for indicator, order in (state.get("orders") or {}).items():
            if isinstance(order, dict) and order.get("status") == "DUMMY_PLACED":
                key = order.get("target_instrument_key") or order.get("instrument_key")
                if key and order.get("entry_price") is not None:
                    self._watch_index.setdefault(key, set()).add((isolation_id, indicator))

    async def register_isolation(self, isolation_id: str, payload: dict,
                                 legs: dict[str, str], overwrite: bool = False) -> None:
        async with self._lock(isolation_id):
            old = await asyncio.to_thread(self._store.load, isolation_id)
            if overwrite:
                await asyncio.to_thread(self._store.delete, isolation_id)
                old = None

            iso_time_raw = next((payload.get(k) for k in
                ("isolation_time", "isolated_at", "isolation_timestamp",
                 "created_at", "received_at", "timestamp") if payload.get(k)), None)
            iso_time = _text_time(iso_time_raw) or _now().isoformat()
            instrument_context = self._instrument_context(payload, legs, isolation_id, iso_time)

            if old is None or old.get("legs") != legs:
                state = {
                    "schema_version": 2, "isolation_id": isolation_id,
                    "trade_date_ist": payload.get("trade_date_ist") or self._date_key(payload, isolation_id),
                    "isolation_time": iso_time,
                    "legs": dict(legs), "trading_symbol": self._symbol(payload),
                    "isolated_instrument": instrument_context,
                    "status": "ACTIVE", "orders": {"sr": None, "supertrend": None},
                    "created_at": _now().isoformat(), "updated_at": _now().isoformat(),
                }
            else:
                state = old
                state["status"] = "ACTIVE"
                state["trading_symbol"] = self._symbol(payload) or state.get("trading_symbol")
                state["isolated_instrument"] = instrument_context
                state["updated_at"] = _now().isoformat()

            # Per-indicator flags are independent. A disabled strategy is
            # explicitly recorded, rather than silently appearing to wait.
            for indicator in ("sr", "supertrend"):
                if self._indicator_enabled(indicator):
                    existing = (state.get("orders") or {}).get(indicator)
                    if isinstance(existing, dict) and existing.get("status") == "DISABLED":
                        state["orders"][indicator] = None
                else:
                    state.setdefault("orders", {})[indicator] = {
                        "indicator": indicator.upper(),
                        "status": "DISABLED",
                        "reason": "orders disabled due to flag false",
                        "placed_at": None,
                        "real_order_submitted": False,
                    }

            await asyncio.to_thread(self._store.save, isolation_id, state)
            date_key = self._date_key(payload, isolation_id)
            await self._persist_mongo_isolation(date_key, isolation_id, instrument_context)
            for indicator in ("sr", "supertrend"):
                disabled = (state.get("orders") or {}).get(indicator)
                if isinstance(disabled, dict) and disabled.get("status") == "DISABLED":
                    await self._persist_mongo_order(
                        date_key, isolation_id, indicator, disabled, instrument_context
                    )

            self._index_open_orders(isolation_id, state)
            self._meta[isolation_id] = {
                "legs": dict(legs),
                "cutoff": _dt(state.get("isolation_time")) or _now(),
                "instrument_context": instrument_context,
                "instrument_meta": {
                    "isolated": {"instrument_key": legs.get("isolated"),
                                 "trading_symbol": instrument_context.get("trading_symbol")},
                    "opposite": {"instrument_key": legs.get("opposite"),
                                 "trading_symbol": instrument_context.get("opposite_trading_symbol")},
                },
                "date_key": date_key,
            }

    async def mark_closed(self, isolation_id: str) -> None:
        state = await asyncio.to_thread(self._store.load, isolation_id)
        if state:
            state["status"], state["updated_at"] = "CLOSED", _now().isoformat()
            await asyncio.to_thread(self._store.save, isolation_id, state)

    async def on_sr_frame(self, payload: dict, instrument_key: str,
                          leg_role: str, isolation_id: str) -> None:
        if not self._indicator_enabled("sr") or payload.get("event") not in {"snapshot", "sr_update"}:
            return
        if leg_role != "isolated":
            return
        try:
            await self._process("sr", payload, instrument_key, leg_role, isolation_id)
        except Exception:
            logger.exception("Dummy SR entry failed for %s", isolation_id)

    async def on_supertrend_frame(self, payload: dict, instrument_key: str,
                                  leg_role: str, isolation_id: str) -> None:
        if not self._indicator_enabled("supertrend") or payload.get("event") not in {"snapshot", "supertrend_signal"}:
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
            if not self._indicator_enabled(indicator):
                return
            existing_order = (state.get("orders") or {}).get(indicator)
            # SR orders remain one-shot. Supertrend orders may be reversed by
            # a later opposite confirmed signal while the current order is open.
            if existing_order and existing_order.get("status") != "DISABLED":
                if indicator != "supertrend" or existing_order.get("status") != "DUMMY_PLACED":
                    return
            meta = self._meta.get(isolation_id) or {
                "legs": state.get("legs") or {},
                "cutoff": _dt(state.get("isolation_time")) or _now(),
                "instrument_context": state.get("isolated_instrument") or {},
                "instrument_meta": {},
                "date_key": self._date_key(state, isolation_id),
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
            # A new opposite Supertrend signal reverses an open dummy order.
            # Close at the latest same-session LTP, but never replace an order
            # if that quote already satisfies its target or stop.
            if isinstance(existing_order, dict) and existing_order.get("status") == "DUMMY_PLACED":
                old_key = existing_order.get("target_instrument_key") or existing_order.get("instrument_key")
                if old_key == target_key:
                    return  # duplicate/same-side signal; keep the current trade open
                old_quote = await asyncio.to_thread(self._live_feed.latest, old_key)
                old_quote_time = _dt((old_quote or {}).get("received_at"))
                trade_date = state.get("trade_date_ist") or isolation_id.split("__", 1)[0]
                old_price = (_price((old_quote or {}).get("data") or {})
                             if old_quote_time and old_quote_time.strftime("%Y-%m-%d") == trade_date
                             else None)
                if old_price is None:
                    logger.warning("Cannot reverse Supertrend order without a same-session LTP iso=%s key=%s",
                                   isolation_id, old_key)
                    return
                old_stop = existing_order.get("stop_loss_price")
                old_target = existing_order.get("target_price")
                if old_target is not None and old_price >= float(old_target):
                    close_reason = "TARGET"
                elif old_stop is not None and old_price <= float(old_stop):
                    close_reason = "STOP_LOSS"
                else:
                    close_reason = "SIGNAL_REVERSAL"
                close_time = _now()
                started = _dt(existing_order.get("started_at_ist") or existing_order.get("entry_time_ist"))
                existing_order.update({
                    "status": ("COMPLETED_TARGET" if close_reason == "TARGET" else
                               "COMPLETED_STOP_LOSS" if close_reason == "STOP_LOSS" else
                               "COMPLETED_SIGNAL_REVERSAL"),
                    "exit_reason": close_reason,
                    "exit_price": old_price,
                    "completed_at_ist": close_time.isoformat(),
                    "duration_seconds": round((close_time - started).total_seconds(), 3) if started else None,
                    "reversed_by_signal": trigger,
                    "replaced_by_target_instrument_key": target_key if close_reason == "SIGNAL_REVERSAL" else None,
                })
                state.setdefault("order_history", []).append(dict(existing_order))
                state["updated_at"] = close_time.isoformat()
                await asyncio.to_thread(self._store.save, isolation_id, state)
                await self._persist_mongo_order_update(
                    meta.get("date_key") or self._date_key(state, isolation_id),
                    isolation_id, indicator, existing_order,
                    meta.get("instrument_context") or {}, completed=True,
                )
                self._index_open_orders(isolation_id, state)
                await self._notify_completion(state, indicator, existing_order)
                if close_reason != "SIGNAL_REVERSAL":
                    logger.info("Supertrend signal arrived after existing order exit threshold iso=%s reason=%s",
                                isolation_id, close_reason)
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
            now_ist = _now()
            target_meta = (meta.get("instrument_meta") or {}).get(role) or {}
            stop_loss_percent = float(getattr(self._config, "dummy_order_stop_loss_percent", 30))
            target_points = self._target_points(entry) if entry is not None else None
            order = {
                "order_id": f"DUMMY-{indicator.upper()}-{isolation_id}-{signal['dt'].strftime('%H%M%S')}",
                "mode": "DUMMY_ONLY", "indicator": indicator.upper(), "side": "BUY",
                "signal_direction": direction, "source_instrument_key": source_key,
                "source_leg_role": source_role, "source_trading_symbol": (
                    (meta.get("instrument_meta") or {}).get(source_role) or {}
                ).get("trading_symbol"),
                "instrument_key": target_key,
                "trading_symbol": target_meta.get("trading_symbol"),
                "target_instrument_key": target_key,
                "target_leg_role": role, "quantity": self._config.dummy_order_quantity,
                "entry_price": entry,
                "entry_time_ist": now_ist.isoformat(),
                "started_at_ist": now_ist.isoformat(),
                "high_price": entry,
                "high_time_ist": now_ist.isoformat() if entry is not None else None,
                "low_price": entry,
                "low_time_ist": now_ist.isoformat() if entry is not None else None,
                "completed_at_ist": None,
                "duration_seconds": None,
                "exit_reason": None,
                "exit_price": None,
                "stop_loss_percent": stop_loss_percent,
                "stop_loss_price": round(entry * (1 - stop_loss_percent / 100), 2) if entry is not None else None,
                "target_points": target_points,
                "target_price": round(entry + target_points, 2) if entry is not None and target_points is not None else None,
                "target_rule": (
                    "entry > upper threshold" if entry is not None and entry > float(getattr(self._config, "dummy_target_upper_threshold", 200))
                    else "lower threshold <= entry <= upper threshold" if entry is not None and entry >= float(getattr(self._config, "dummy_target_lower_threshold", 100))
                    else "entry < lower threshold" if entry is not None else None
                ),
                "price_source": ("latest_local_market_feed" if quote and entry is not None else
                                 "indicator_payload_fallback" if entry is not None else None),
                "quote_received_at": quote_received_at if quote_is_today else None,
                "quote_age_seconds": round((_now() - quote_time).total_seconds(), 3) if quote_is_today else None,
                "trigger": trigger, "placed_at": now_ist.isoformat(),
                "status": "DUMMY_PLACED" if entry is not None else "DUMMY_SIGNAL_RECORDED_NO_QUOTE",
                "broker_order_id": None, "real_order_submitted": False,
            }
            state.setdefault("orders", {})[indicator] = order
            state["updated_at"] = now_ist.isoformat()
            await asyncio.to_thread(self._store.save, isolation_id, state)
            await self._persist_mongo_order(
                meta.get("date_key") or self._date_key(state, isolation_id),
                isolation_id, indicator, order, meta.get("instrument_context") or {}
            )
            self._index_open_orders(isolation_id, state)
            logger.info("Dummy %s entry saved iso=%s target=%s trigger=%s",
                        indicator, isolation_id, target_key, trigger.get("confirmed_time"))
            await self._notify(state)


    async def on_market_tick(self, instrument_key: str, data: dict,
                             received_at: str | None = None) -> None:
        price = _market_ltp(data)
        if price is None or price <= 0:
            return
        tick_time = _dt(received_at) or _now()
        for isolation_id, indicator in list(self._watch_index.get(instrument_key, set())):
            try:
                await self._update_order_from_tick(isolation_id, indicator, instrument_key, price, tick_time)
            except Exception:
                logger.exception("Dummy order tick failed iso=%s indicator=%s key=%s",
                                 isolation_id, indicator, instrument_key)

    async def _update_order_from_tick(self, isolation_id: str, indicator: str,
                                      instrument_key: str, price: float,
                                      tick_time: datetime) -> None:
        async with self._lock(isolation_id):
            state = await asyncio.to_thread(self._store.load, isolation_id)
            if not state:
                self._index_open_orders(isolation_id, {})
                return
            order = (state.get("orders") or {}).get(indicator)
            if not isinstance(order, dict) or order.get("status") != "DUMMY_PLACED":
                self._index_open_orders(isolation_id, state)
                return
            if (order.get("target_instrument_key") or order.get("instrument_key")) != instrument_key:
                return
            stamp = tick_time.isoformat()
            changed = False
            high, low = order.get("high_price"), order.get("low_price")
            if high is None or price > float(high):
                order["high_price"], order["high_time_ist"] = price, stamp
                changed = True
            if low is None or price < float(low):
                order["low_price"], order["low_time_ist"] = price, stamp
                changed = True
            reason = None
            stop, target = order.get("stop_loss_price"), order.get("target_price")
            if stop is not None and price <= float(stop):
                reason = "STOP_LOSS"
            elif target is not None and price >= float(target):
                reason = "TARGET"
            if reason:
                started = _dt(order.get("started_at_ist") or order.get("entry_time_ist"))
                order.update({
                    "status": "COMPLETED_STOP_LOSS" if reason == "STOP_LOSS" else "COMPLETED_TARGET",
                    "exit_reason": reason, "exit_price": price,
                    "completed_at_ist": stamp,
                    "duration_seconds": round((tick_time - started).total_seconds(), 3) if started else None,
                })
                changed = True
            if not changed:
                return
            state["updated_at"] = stamp
            await asyncio.to_thread(self._store.save, isolation_id, state)
            await self._persist_mongo_order_update(
                self._date_key(state, isolation_id), isolation_id, indicator,
                order, state.get("isolated_instrument") or {}, completed=bool(reason),
            )
            if reason:
                self._index_open_orders(isolation_id, state)
                logger.info("Dummy %s completed iso=%s reason=%s entry=%s exit=%s high=%s low=%s duration=%s",
                            indicator, isolation_id, reason, order.get("entry_price"),
                            price, order.get("high_price"), order.get("low_price"),
                            order.get("duration_seconds"))
                await self._notify_completion(state, indicator, order)

    async def _persist_mongo_order_update(self, date_key: str, isolation_id: str,
                                          indicator: str, order: dict,
                                          isolated_instrument: dict,
                                          completed: bool) -> None:
        if self._mongo_store is None:
            return
        try:
            await asyncio.to_thread(self._mongo_store.update_order, date_key, isolation_id,
                                    indicator, order, isolated_instrument, completed)
        except Exception:
            logger.exception("Mongo dummy-order update failed date=%s iso=%s indicator=%s",
                             date_key, isolation_id, indicator)

    async def _notify_completion(self, state: dict, indicator: str, order: dict) -> None:
        if self._telegram is None:
            return
        message = "\n".join([
            "🧪 DUMMY ORDER COMPLETED", f"Isolation: {state.get('isolation_id')}",
            f"Strategy: {indicator.upper()}",
            f"Instrument: {order.get('trading_symbol') or order.get('target_instrument_key')}",
            f"Result: {order.get('exit_reason')}", f"Entry: {order.get('entry_price')}",
            f"Exit: {order.get('exit_price')}",
            f"High: {order.get('high_price')} at {order.get('high_time_ist')}",
            f"Low: {order.get('low_price')} at {order.get('low_time_ist')}",
            f"Started: {order.get('started_at_ist')}",
            f"Completed: {order.get('completed_at_ist')}",
            f"Duration (seconds): {order.get('duration_seconds')}",
            "Execution: LOCAL SIMULATION ONLY",
        ])
        try:
            await self._telegram.send_message("Dummy order completed", message, level="SUCCESS")
        except Exception:
            logger.exception("Dummy order completion Telegram send failed")

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
        result["sr_orders_enabled"] = self._indicator_enabled("sr")
        result["supertrend_orders_enabled"] = self._indicator_enabled("supertrend")
        return result
