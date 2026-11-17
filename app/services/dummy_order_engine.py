"""First post-isolation SR and Supertrend dummy-entry logic.

Only local simulation records are created. This module has no broker client.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo
from ..config import Settings
from .dummy_order_store import DummyOrderStore
from .live_feed_state_store import LiveFeedStateStore
from .telegram_message_formatter import (
    format_order_completion_notification,
    format_order_entry_notification,
    format_recovery_notification,
)

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
                 mongo_store: Any = None, candle_store: Any = None) -> None:
        self._config, self._store = config, store
        self._live_feed, self._telegram = live_feed_store, telegram
        self._mongo_store = mongo_store
        self._candle_store = candle_store
        self._startup_at = _now()
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
            "lot_size": payload.get("lot_size") or (payload.get("meta") or {}).get("lot_size"),
            "opposite_lot_size": opposite.get("lot_size") or (opposite.get("meta") or {}).get("lot_size"),
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
                                 "trading_symbol": instrument_context.get("trading_symbol"),
                                 "lot_size": instrument_context.get("lot_size")},
                    "opposite": {"instrument_key": legs.get("opposite"),
                                 "trading_symbol": instrument_context.get("opposite_trading_symbol"),
                                 "lot_size": instrument_context.get("opposite_lot_size")},
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
            if payload.get("event") == "snapshot" and self._is_mid_session_startup():
                await self._recover_supertrend_history(isolation_id, payload)
                recovered = await asyncio.to_thread(self._store.load, isolation_id)
                recovery_status = (recovered or {}).get("supertrend_startup_recovery", {}).get("status")
                if recovery_status in {"COMPLETED", "SKIPPED_EXISTING_ORDER"}:
                    return
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
            if indicator == "supertrend" and state.get("supertrend_daily_target_locked"):
                return
            recovery = state.get("supertrend_startup_recovery") or {}
            if indicator == "supertrend" and recovery.get("status") == "WAITING_FOR_HISTORY":
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
                units = int(existing_order.get("quantity_units") or existing_order.get("quantity") or 1)
                pnl_points = round(old_price - float(existing_order.get("entry_price") or 0), 4)
                existing_order.update({"pnl_points": pnl_points, "pnl_gross": round(pnl_points * units, 2),
                                       "estimated_costs": 0.0, "pnl_net": round(pnl_points * units, 2),
                                       "pnl_currency": "INR", "pnl_cost_model": "gross_only_costs_not_configured"})
                if close_reason == "TARGET":
                    state["supertrend_daily_target_locked"] = True
                    state["supertrend_daily_target_locked_at"] = close_time.isoformat()
                    state["supertrend_daily_target_order_id"] = existing_order.get("order_id")
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
                "lot_size": int(target_meta.get("lot_size") or 1),
                "quantity_units": int(self._config.dummy_order_quantity) * int(target_meta.get("lot_size") or 1),
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
            await self._notify_entry(state, order)



    def _is_mid_session_startup(self) -> bool:
        start = self._config.live_feed_start_hour * 60 + self._config.live_feed_start_minute
        return self._startup_at.hour * 60 + self._startup_at.minute > start + 2

    @staticmethod
    def _candle_dt(candle: dict) -> datetime | None:
        return _dt(candle.get("ts") or candle.get("timestamp"))

    def _simulate_candles(self, order: dict, candles: list[dict],
                          after: datetime, until: datetime) -> tuple[bool, dict | None, datetime | None]:
        """Replay OHLC conservatively: stop-loss wins if both levels touch in one bar."""
        stop, target = float(order["stop_loss_price"]), float(order["target_price"])
        last_stamp = None
        for candle in candles:
            stamp = self._candle_dt(candle)
            if stamp is None or stamp < after or stamp >= until:
                continue
            last_stamp = stamp
            high = float(candle.get("high", candle.get("close", 0)) or 0)
            low = float(candle.get("low", candle.get("close", 0)) or 0)
            if high > float(order.get("high_price") or high):
                order["high_price"], order["high_time_ist"] = high, stamp.isoformat()
            if low < float(order.get("low_price") or low):
                order["low_price"], order["low_time_ist"] = low, stamp.isoformat()
            if low <= stop:
                return True, {"reason": "STOP_LOSS", "price": stop, "time": stamp}, stamp
            if high >= target:
                return True, {"reason": "TARGET", "price": target, "time": stamp}, stamp
        return False, None, last_stamp

    def _recovery_order(self, isolation_id: str, signal: dict, role: str,
                        key: str, candle: dict, meta: dict) -> dict:
        entry = float(candle.get("open") or candle.get("close"))
        entry_time = self._candle_dt(candle) or signal["dt"]
        target_points = self._target_points(entry)
        stop_percent = float(getattr(self._config, "dummy_order_stop_loss_percent", 30))
        quantity = int(getattr(self._config, "dummy_order_quantity", 1))
        lot_size = int((meta.get("instrument_meta") or {}).get(role, {}).get("lot_size") or 1)
        return {
            "order_id": f"RECOVERY-SUPERTREND-{isolation_id}-{signal['dt'].strftime('%H%M%S')}",
            "mode": "DUMMY_ONLY", "simulation_mode": "STARTUP_RECOVERY",
            "indicator": "SUPERTREND", "side": "BUY", "signal_direction": signal["direction"],
            "source_instrument_key": meta["legs"].get("isolated"), "source_leg_role": "isolated",
            "instrument_key": key, "target_instrument_key": key, "target_leg_role": role,
            "trading_symbol": (meta.get("instrument_meta") or {}).get(role, {}).get("trading_symbol"),
            "quantity": quantity, "lot_size": lot_size, "quantity_units": quantity * lot_size,
            "entry_price": entry, "entry_time_ist": entry_time.isoformat(),
            "started_at_ist": entry_time.isoformat(), "high_price": entry,
            "high_time_ist": entry_time.isoformat(), "low_price": entry,
            "low_time_ist": entry_time.isoformat(), "stop_loss_percent": stop_percent,
            "stop_loss_price": round(entry * (1 - stop_percent / 100), 2),
            "target_points": target_points, "target_price": round(entry + target_points, 2),
            "target_rule": "configured entry-price target bands",
            "trigger": {"direction": signal["direction"], "confirmed_time": signal["time"],
                        "source_event": "startup_history_replay"},
            "price_source": "persisted_one_minute_ohlc", "status": "DUMMY_PLACED",
            "exit_reason": None, "exit_price": None, "completed_at_ist": None,
            "duration_seconds": None, "broker_order_id": None, "real_order_submitted": False,
        }

    def _finish_recovery_order(self, order: dict, reason: str,
                               price: float, stamp: datetime) -> None:
        started = _dt(order.get("started_at_ist"))
        units = int(order.get("quantity_units") or order.get("quantity") or 1)
        points = round(float(price) - float(order["entry_price"]), 4)
        gross = round(points * units, 2)
        order.update({
            "status": ("COMPLETED_TARGET" if reason == "TARGET" else
                       "COMPLETED_STOP_LOSS" if reason == "STOP_LOSS" else
                       "COMPLETED_SIGNAL_REVERSAL"),
            "exit_reason": reason, "exit_price": round(float(price), 2),
            "completed_at_ist": stamp.isoformat(),
            "duration_seconds": round((stamp - started).total_seconds(), 3) if started else None,
            "pnl_points": points, "pnl_gross": gross, "estimated_costs": 0.0,
            "pnl_net": gross, "pnl_currency": "INR",
            "pnl_cost_model": "gross_only_costs_not_configured",
        })

    async def _recover_supertrend_history(self, isolation_id: str, payload: dict) -> None:
        if self._candle_store is None:
            return
        async with self._lock(isolation_id):
            state = await asyncio.to_thread(self._store.load, isolation_id)
            if not state or state.get("status") != "ACTIVE":
                return
            previous_recovery = state.get("supertrend_startup_recovery") or {}
            if previous_recovery.get("status") in {"COMPLETED", "SKIPPED_EXISTING_ORDER"}:
                return
            trade_date = state.get("trade_date_ist") or isolation_id.split("__", 1)[0]
            cutoff = _dt(state.get("isolation_time")) or self._startup_at
            signals = sorted((item for item in _st_candidates(payload)
                              if cutoff < item["dt"] <= self._startup_at),
                             key=lambda item: item["dt"])
            meta = self._meta.get(isolation_id) or {
                "legs": state.get("legs") or {}, "instrument_context": state.get("isolated_instrument") or {},
                "instrument_meta": {}, "date_key": trade_date,
            }
            keys = {role: meta["legs"].get(role) for role in ("isolated", "opposite")}
            candles = {role: self._candle_store.read_day(key, trade_date) if key else []
                       for role, key in keys.items()}
            required_roles = {("isolated" if x["direction"] == "BULLISH" else "opposite") for x in signals}
            missing = [role for role in required_roles if not candles.get(role)]
            if missing:
                state["supertrend_startup_recovery"] = {
                    "status": "WAITING_FOR_HISTORY", "started_at_ist": self._startup_at.isoformat(),
                    "trade_date_ist": trade_date, "signals_found": len(signals),
                    "missing_candle_roles": sorted(missing),
                    "message": "Historical one-minute candles are required; Supertrend entries are paused rather than estimating unavailable prices.",
                }
                state["updated_at"] = _now().isoformat()
                await asyncio.to_thread(self._store.save, isolation_id, state)
                logger.error("Supertrend startup recovery waiting for candles iso=%s missing=%s",
                             isolation_id, missing)
                return

            persisted_order = (state.get("orders") or {}).get("supertrend")
            if isinstance(persisted_order, dict) and persisted_order.get("status") == "DUMMY_PLACED":
                state["supertrend_startup_recovery"] = {
                    "status": "SKIPPED_EXISTING_ORDER", "started_at_ist": self._startup_at.isoformat(),
                    "message": "An open order already exists; preserving it prevents duplicate replay. Gap reconciliation for this pre-existing order is not performed.",
                }
                await asyncio.to_thread(self._store.save, isolation_id, state)
                self._index_open_orders(isolation_id, state)
                return

            history = list(state.get("order_history") or [])
            active, active_role, active_candles, last_processed = None, None, None, None
            locked, replayed = False, 0

            async def persist_closed(order: dict):
                self._finish_recovery_order(order, order["exit_reason"],
                                            float(order["exit_price"]), _dt(order["completed_at_ist"]))
                history.append(dict(order))
                await self._persist_mongo_order(trade_date, isolation_id, "supertrend",
                                                order, meta.get("instrument_context") or {})
                await self._persist_mongo_order_update(trade_date, isolation_id, "supertrend",
                                                       order, meta.get("instrument_context") or {}, completed=True)

            for signal in signals:
                role = "isolated" if signal["direction"] == "BULLISH" else "opposite"
                key, role_candles = keys.get(role), candles.get(role) or []
                if active is not None:
                    hit, result, _ = self._simulate_candles(
                        active, active_candles or [], last_processed or _dt(active["started_at_ist"]),
                        signal["dt"])
                    if hit:
                        active["exit_reason"], active["exit_price"] = result["reason"], result["price"]
                        active["completed_at_ist"] = result["time"].isoformat()
                        await persist_closed(active)
                        if result["reason"] == "TARGET":
                            locked = True
                            break
                        active = active_role = active_candles = last_processed = None
                    elif role != active_role:
                        prior = [bar for bar in (active_candles or [])
                                 if (self._candle_dt(bar) or self._startup_at) < signal["dt"]]
                        if not prior:
                            state["supertrend_startup_recovery"] = {
                                "status": "WAITING_FOR_HISTORY",
                                "message": "No completed candle exists to price a historical reversal.",
                                "signals_found": len(signals)}
                            await asyncio.to_thread(self._store.save, isolation_id, state)
                            return
                        last = prior[-1]
                        active["exit_reason"], active["exit_price"] = "SIGNAL_REVERSAL", float(last["close"])
                        active["completed_at_ist"] = self._candle_dt(last).isoformat()
                        await persist_closed(active)
                        active = active_role = active_candles = last_processed = None
                    else:
                        continue
                if locked:
                    break
                entry_candle = next((bar for bar in role_candles
                                     if self._candle_dt(bar) is not None and
                                     self._candle_dt(bar) > signal["dt"] and
                                     self._candle_dt(bar) <= self._startup_at), None)
                if entry_candle is None:
                    state["supertrend_startup_recovery"] = {
                        "status": "WAITING_FOR_HISTORY", "started_at_ist": self._startup_at.isoformat(),
                        "message": f"No post-confirmation entry candle is available for signal at {signal['time']}.",
                        "unpriced_signal_time": signal["time"],
                    }
                    await asyncio.to_thread(self._store.save, isolation_id, state)
                    return
                active = self._recovery_order(isolation_id, signal, role, key, entry_candle, meta)
                active_role, active_candles = role, role_candles
                last_processed = self._candle_dt(entry_candle)
                replayed += 1

            if active is not None and not locked:
                hit, result, _ = self._simulate_candles(
                    active, active_candles or [], last_processed or _dt(active["started_at_ist"]),
                    self._startup_at + timedelta(seconds=1))
                if hit:
                    active["exit_reason"], active["exit_price"] = result["reason"], result["price"]
                    active["completed_at_ist"] = result["time"].isoformat()
                    await persist_closed(active)
                    if result["reason"] == "TARGET":
                        locked = True
                    active = None

            if locked:
                state["supertrend_daily_target_locked"] = True
                state["supertrend_daily_target_locked_at"] = _now().isoformat()
                state["supertrend_daily_target_lock_reason"] = "STARTUP_RECOVERY_TARGET"
            if active is not None:
                state.setdefault("orders", {})["supertrend"] = active
                await self._persist_mongo_order(trade_date, isolation_id, "supertrend",
                                                active, meta.get("instrument_context") or {})
            elif history:
                state.setdefault("orders", {})["supertrend"] = history[-1]
            state["order_history"] = history[-500:]
            state["supertrend_startup_recovery"] = {
                "status": "COMPLETED", "started_at_ist": self._startup_at.isoformat(),
                "completed_at_ist": _now().isoformat(), "signals_found": len(signals),
                "orders_replayed": replayed, "orders_completed": len(history),
                "daily_target_locked": locked,
                "method": "one_minute_ohlc_stop_first_if_both_levels_touch",
            }
            state["updated_at"] = _now().isoformat()
            await asyncio.to_thread(self._store.save, isolation_id, state)
            self._index_open_orders(isolation_id, state)
            logger.info("Supertrend startup recovery complete iso=%s signals=%d replayed=%d completed=%d locked=%s",
                        isolation_id, len(signals), replayed, len(history), locked)
            await self._notify_recovery(state)

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
                if indicator == "supertrend" and reason == "TARGET":
                    state["supertrend_daily_target_locked"] = True
                    state["supertrend_daily_target_locked_at"] = stamp
                    state["supertrend_daily_target_order_id"] = order.get("order_id")
                units = int(order.get("quantity_units") or order.get("quantity") or 1)
                pnl_points = round(price - float(order.get("entry_price") or 0), 4)
                order.update({"pnl_points": pnl_points, "pnl_gross": round(pnl_points * units, 2),
                              "estimated_costs": 0.0, "pnl_net": round(pnl_points * units, 2),
                              "pnl_currency": "INR", "pnl_cost_model": "gross_only_costs_not_configured"})
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

    async def _send_formatted_telegram(self, message: str, title: str,
                                       level: str = "INFO") -> None:
        sender = getattr(self._telegram, "send_formatted_message", None)
        if callable(sender):
            await sender(message)
        else:
            # Backward compatibility for small notifier adapters used by tests
            # or deployments that still expose only send_message(title, body).
            await self._telegram.send_message(title, message, level=level)

    async def _notify_entry(self, state: dict, order: dict) -> None:
        if self._telegram is None:
            return
        try:
            await self._send_formatted_telegram(
                format_order_entry_notification(state, order),
                "Simulated entry",
                "SUCCESS",
            )
        except Exception:
            logger.exception("Dummy order entry Telegram notification failed")

    async def _notify_completion(self, state: dict, indicator: str, order: dict) -> None:
        if self._telegram is None:
            return
        try:
            await self._send_formatted_telegram(
                format_order_completion_notification(state, indicator, order),
                "Simulated order closed",
                "SUCCESS",
            )
        except Exception:
            logger.exception("Dummy order completion Telegram notification failed")

    async def _notify_recovery(self, state: dict) -> None:
        if self._telegram is None:
            return
        try:
            await self._send_formatted_telegram(
                format_recovery_notification(state),
                "Startup recovery",
                "INFO",
            )
        except Exception:
            logger.exception("Dummy order recovery Telegram notification failed")

    async def get_orders(self, isolation_id: str | None = None) -> Any:
        if isolation_id:
            return await asyncio.to_thread(self._store.load, isolation_id)
        return await asyncio.to_thread(self._store.list_all)

    async def status(self) -> dict:
        result = await asyncio.to_thread(self._store.status)
        result["enabled"] = self._config.dummy_orders_enabled
        result["sr_orders_enabled"] = self._indicator_enabled("sr")
        result["supertrend_orders_enabled"] = self._indicator_enabled("supertrend")
        states = await asyncio.to_thread(self._store.list_all)
        result["supertrend_daily_target_locked_count"] = sum(bool(x.get("supertrend_daily_target_locked")) for x in states)
        result["startup_recovery_waiting_count"] = sum(
            (x.get("supertrend_startup_recovery") or {}).get("status") == "WAITING_FOR_HISTORY"
            for x in states)
        return result
