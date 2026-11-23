"""Post-isolation Supertrend-only dummy-entry logic.

Only local simulation records are created. This module has no broker client.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo
from ..config import Settings
from .dummy_order_store import DummyOrderStore
from .live_feed_state_store import LiveFeedStateStore
from .pivot_levels_service import PivotLevelsService
from .telegram_message_formatter import (
    format_order_completion_notification,
    format_order_partial_exit_notification,
    format_second_entry_blocked_notification,
    format_order_entry_notification,
    format_recovery_notification,
)

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
MAX_SUPERTREND_ORDERS_PER_ISOLATION = 2
SUPERTREND_ENTRY_START = time(9, 15)
SUPERTREND_ENTRY_CUTOFF = time(9, 40)

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
                 mongo_store: Any = None, candle_store: Any = None,
                 pivot_service: PivotLevelsService | None = None) -> None:
        self._config, self._store = config, store
        self._live_feed, self._telegram = live_feed_store, telegram
        self._mongo_store = mongo_store
        self._candle_store = candle_store
        self._pivot_service = pivot_service
        self._startup_at = _now()
        self._meta: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._watch_index: dict[str, set[tuple[str, str]]] = {}
        self._pivot_refresh_last_minute: dict[str, str] = {}
        self._startup_backfill_isolation_ids: set[str] = set()

    def mark_startup_backfill_isolations(self, isolation_ids: list[str]) -> None:
        """Prevent delayed per-isolation recovery pings after the startup digest."""
        self._startup_backfill_isolation_ids.update(
            str(value) for value in isolation_ids if value
        )

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

    def _refresh_pivot_levels(
        self, instrument_keys: list[str], trade_date: str,
        candles_by_key: dict[str, list[dict]] | None = None,
    ) -> None:
        """Refresh local pivots, using supplied HistoryV3 candles when local files are absent."""
        supplied = candles_by_key or {}
        if self._pivot_service is None or (self._candle_store is None and not supplied):
            return
        today = str(trade_date or "")[:10]
        if not today:
            return
        now = _now()
        for key in sorted({str(value) for value in instrument_keys if value}):
            try:
                local_days = self._candle_store.list_days(key) if self._candle_store is not None else []
                supplied_candles = list(supplied.get(key) or [])
                supplied_days = sorted({
                    stamp.date().isoformat()
                    for item in supplied_candles
                    if (stamp := self._candle_dt(item)) is not None
                })
                available_days = sorted(set(local_days) | set(supplied_days))
                prior_days = [day for day in available_days if day < today]
                previous_day = prior_days[-1] if prior_days else None
                local_today = (
                    self._candle_store.read_day(key, today)
                    if self._candle_store is not None else []
                )
                today_by_time = {
                    str(item.get("ts") or item.get("timestamp")): item
                    for item in supplied_candles
                    if (stamp := self._candle_dt(item)) is not None
                    and stamp.date().isoformat() == today
                }
                today_by_time.update({
                    str(item.get("ts") or item.get("timestamp")): item
                    for item in local_today if self._candle_dt(item) is not None
                })
                today_candles = list(today_by_time.values())
                # The first opening candle must be complete before its OHLC
                # becomes a stable pivot input.
                completed_today = [
                    item for item in today_candles
                    if (stamp := self._candle_dt(item)) is not None
                    and stamp + timedelta(minutes=1) <= now
                ]
                if not completed_today:
                    continue
                local_previous = (
                    self._candle_store.read_day(key, previous_day)
                    if self._candle_store is not None and previous_day in local_days else []
                )
                previous_by_time = {
                    str(item.get("ts") or item.get("timestamp")): item
                    for item in supplied_candles
                    if (stamp := self._candle_dt(item)) is not None
                    and previous_day and stamp.date().isoformat() == previous_day
                }
                previous_by_time.update({
                    str(item.get("ts") or item.get("timestamp")): item
                    for item in local_previous if self._candle_dt(item) is not None
                })
                previous_candles = list(previous_by_time.values())
                record = self._pivot_service.calculate(
                    key, today, completed_today, previous_day, previous_candles
                )
                if record is not None:
                    roles = set()
                    for meta in self._meta.values():
                        for role, role_key in (meta.get("legs") or {}).items():
                            if role_key == key:
                                roles.add(role)
                    if key == getattr(self._config, "nifty_instrument_key", ""):
                        roles.add("main_index")
                    record["roles"] = sorted(roles)
                    self._pivot_service.update(record)
            except Exception:
                logger.exception("Pivot-level refresh failed instrument=%s date=%s", key, today)

    def _pivot_adjusted_stop_loss(
        self, instrument_key: str, trade_date: str, entry: float, raw_stop: float
    ) -> tuple[float, dict[str, Any]]:
        """Snap a percentage stop to the nearest opening-range support.

        Choose the highest S-level at/below the percentage stop. If the
        percentage stop is below S3, use S3 as the defined support floor.
        If pivot data is missing or invalid, preserve the percentage stop.
        """
        record = (
            self._pivot_service.get(instrument_key, trade_date)
            if self._pivot_service is not None else None
        )
        pivots = (record or {}).get("pivot_levels") or {}
        supports = [
            (name, _valid)
            for name in ("S1", "S2", "S3")
            if (_valid := self._safe_positive_float(pivots.get(name))) is not None
            and _valid < entry
        ]
        selected = [(name, value) for name, value in supports if value <= raw_stop]
        if selected:
            level_name, final_stop = max(selected, key=lambda item: item[1])
        else:
            s3 = self._safe_positive_float(pivots.get("S3"))
            if s3 is not None and s3 < entry:
                level_name, final_stop = "S3_FLOOR", s3
            else:
                level_name, final_stop = "PERCENTAGE", raw_stop
        return round(final_stop, 2), {
            "instrument_key": instrument_key,
            "trade_date_ist": trade_date,
            "raw_percentage_stop_loss": round(raw_stop, 2),
            "final_stop_loss": round(final_stop, 2),
            "selected_level": level_name,
            "pivot_levels": pivots or None,
            "rule": "nearest_support_at_or_below_percentage_stop; use_S3_if_percentage_stop_below_S3",
        }

    @staticmethod
    def _safe_positive_float(value: Any) -> float | None:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if result > 0 and result < float("inf") else None

    def _indicator_enabled(self) -> bool:
        return bool(
            getattr(self._config, "dummy_orders_enabled", True)
            and getattr(self._config, "dummy_supertrend_orders_enabled", True)
        )

    @staticmethod
    def _supertrend_order_count(state: dict) -> int:
        """Return the durable number of Supertrend entries for this isolation."""
        try:
            stored_count = max(0, int(state.get("supertrend_order_count") or 0))
        except (TypeError, ValueError):
            stored_count = 0
        identities = set()
        candidates = list(state.get("order_history") or [])
        current = (state.get("orders") or {}).get("supertrend")
        if isinstance(current, dict):
            candidates.append(current)
        for order in candidates:
            if not isinstance(order, dict):
                continue
            status = str(order.get("status") or "").upper()
            if status != "DUMMY_PLACED" and not status.startswith("COMPLETED"):
                continue
            indicator = str(order.get("indicator") or "").upper()
            if indicator and "SUPERTREND" not in indicator:
                continue
            identity = order.get("order_id") or order.get("started_at_ist") or order.get("entry_time_ist") or order.get("placed_at")
            if identity:
                identities.add(str(identity))
        return max(stored_count, len(identities))

    @staticmethod
    def _startup_recovery_order_counts(orders: list[dict] | None) -> tuple[int, int]:
        """Count unique persisted startup-recovery orders and their completions.

        A completed recovery order can remain in orders.supertrend while
        order_history is empty (for example, after restoring a Mongo snapshot).
        Count both locations and deduplicate by order identity.
        """
        unique: dict[str, dict] = {}
        for order in orders or []:
            if not isinstance(order, dict):
                continue
            status = str(order.get("status") or "").upper()
            if status != "DUMMY_PLACED" and not status.startswith("COMPLETED"):
                continue
            order_id = str(order.get("order_id") or "").strip()
            is_recovery = (
                str(order.get("simulation_mode") or "").upper() == "STARTUP_RECOVERY"
                or str(order.get("price_source") or "").lower() == "persisted_one_minute_ohlc"
                or order_id.startswith("RECOVERY-SUPERTREND-")
            )
            if not is_recovery:
                continue
            identity = order_id or str(
                order.get("started_at_ist") or order.get("entry_time_ist")
                or order.get("placed_at") or id(order)
            )
            previous = unique.get(identity)
            if previous is None or (
                not str(previous.get("status") or "").upper().startswith("COMPLETED")
                and status.startswith("COMPLETED")
            ):
                unique[identity] = order
        completed = sum(
            str(order.get("status") or "").upper().startswith("COMPLETED")
            for order in unique.values()
        )
        return len(unique), completed

    @staticmethod
    def _resolve_supertrend_entry_limit(state: dict, first_signal: dict | None = None) -> int:
        """Return the durable limit selected by the first eligible signal."""
        mode = state.get("supertrend_entry_window_mode")
        if mode == "LATE_SINGLE":
            return 1
        if mode == "EARLY_TWO":
            return 2
        prior = list(state.get("order_history") or [])
        current = (state.get("orders") or {}).get("supertrend")
        if isinstance(current, dict):
            prior.append(current)
        confirmed_times = [
            parsed for order in prior if isinstance(order, dict)
            if (parsed := _dt((order.get("trigger") or {}).get("confirmed_time"))) is not None
        ]
        first_time = min(confirmed_times) if confirmed_times else (
            first_signal.get("dt") if isinstance(first_signal, dict) else None
        )
        if first_time is not None and first_time.timetz().replace(tzinfo=None) >= SUPERTREND_ENTRY_CUTOFF:
            return 1
        return 2

    @staticmethod
    def _signal_in_entry_session(signal: dict) -> bool:
        return signal["dt"].timetz().replace(tzinfo=None) >= SUPERTREND_ENTRY_START

    @staticmethod
    def _archive_supertrend_order(state: dict, order: dict) -> None:
        """Keep a completed current-slot order before replacing it with a new entry."""
        if not isinstance(order, dict):
            return
        history = state.setdefault("order_history", [])
        identity = order.get("order_id") or order.get("started_at_ist") or order.get("entry_time_ist")
        if not any(
            isinstance(item, dict) and
            (item.get("order_id") or item.get("started_at_ist") or item.get("entry_time_ist")) == identity
            for item in history
        ):
            history.append(dict(order))

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

    @staticmethod
    def _apply_partial_target(order: dict, stamp: datetime, target_price: float | None = None) -> bool:
        """Exit one lot at target and move the runner stop to half the target distance."""
        if order.get("partial_exit_done"):
            return False
        lots = int(order.get("quantity") or 1)
        if lots <= 1:
            return False
        entry = float(order.get("entry_price") or 0)
        target = float(target_price if target_price is not None else order.get("target_price"))
        lot_size = max(1, int(order.get("lot_size") or 1))
        original_units = int(order.get("original_quantity_units") or order.get("quantity_units") or lots * lot_size)
        remaining_lots = lots - 1
        remaining_units = max(0, int(order.get("quantity_units") or lots * lot_size) - lot_size)
        points = round(target - entry, 4)
        realized = round(points * lot_size, 2)
        new_stop = round(entry + (target - entry) / 2.0, 2)
        order.setdefault("original_quantity", lots)
        order.setdefault("original_quantity_units", original_units)
        order["quantity"] = remaining_lots
        order["quantity_units"] = remaining_units
        order["remaining_quantity_lots"] = remaining_lots
        order["remaining_quantity_units"] = remaining_units
        order["partial_exit_done"] = True
        order["first_target_reached"] = True
        order["partial_exit"] = {
            "status": "COMPLETED_TARGET",
            "quantity_lots": 1,
            "quantity_units": lot_size,
            "exit_price": round(target, 2),
            "exit_time_ist": stamp.isoformat(),
            "pnl_points": points,
            "pnl_gross": realized,
            "pnl_net": realized,
        }
        order["realized_pnl_gross"] = round(float(order.get("realized_pnl_gross") or 0) + realized, 2)
        order["pnl_points"] = round(order["realized_pnl_gross"] / original_units, 4) if original_units else 0.0
        order["pnl_gross"] = order["realized_pnl_gross"]
        order["pnl_net"] = order["realized_pnl_gross"]
        order["estimated_costs"] = 0.0
        order["pnl_currency"] = "INR"
        order["pnl_cost_model"] = "gross_only_costs_not_configured"
        order["stop_loss_before_partial_target"] = order.get("stop_loss_price")
        order["stop_loss_price"] = new_stop
        order["stop_loss_adjustment"] = {
            "reason": "FIRST_LOT_TARGET_REACHED",
            "original_target_price": round(target, 2),
            "target_distance_points": round(target - entry, 2),
            "half_target_distance_points": round((target - entry) / 2.0, 2),
            "new_stop_loss_price": new_stop,
            "updated_at_ist": stamp.isoformat(),
        }
        order["updated_at_ist"] = stamp.isoformat()
        return True

    @staticmethod
    def _total_order_pnl(order: dict, exit_price: float) -> tuple[float, float, float]:
        entry = float(order.get("entry_price") or 0)
        remaining_units = int(order.get("quantity_units") or order.get("quantity") or 1)
        original_units = int(order.get("original_quantity_units") or (
            remaining_units + int((order.get("partial_exit") or {}).get("quantity_units") or 0)
        ))
        realized = float(order.get("realized_pnl_gross") or 0)
        gross = round(realized + (float(exit_price) - entry) * remaining_units, 2)
        points = round(gross / original_units, 4) if original_units else 0.0
        return points, gross, gross

    def _live_entry_eligibility(
        self, payload: dict, signal: dict, target_key: str, state: dict,
        event_origin: str | None, live_event_received_at: str | None,
    ) -> dict:
        """Fail-closed gate for creating a live dummy entry."""
        now = _now()
        if payload.get("event") != "supertrend_signal":
            return {"eligible": False, "reason": "NOT_A_LIVE_SIGNAL_EVENT"}
        if event_origin not in {"live_websocket", "local_upstox"} or not live_event_received_at:
            return {"eligible": False, "reason": "MISSING_LIVE_SIGNAL_PROVENANCE"}
        frame_time = _dt(live_event_received_at)
        if frame_time is None:
            return {"eligible": False, "reason": "INVALID_LIVE_FRAME_RECEIVE_TIME"}
        frame_age = (now - frame_time).total_seconds()
        if frame_age < -2 or frame_age > int(getattr(self._config, "live_frame_max_age_seconds", 5)):
            return {"eligible": False, "reason": "LIVE_FRAME_RECEIVE_TIME_STALE"}
        signal_time = signal.get("dt")
        if not isinstance(signal_time, datetime) or signal_time.date() != now.date():
            return {"eligible": False, "reason": "CONFIRMATION_NOT_FROM_TODAY"}
        signal_age = (now - signal_time).total_seconds()
        max_signal_age = int(getattr(self._config, "live_signal_max_age_seconds", 60))
        if event_origin == "local_upstox":
            # Upstox timestamps identify the candle OPEN; its close is confirmed
            # one minute later. Keep a bounded allowance for that candle boundary.
            max_signal_age = max(max_signal_age, 120)
        if signal_age < -2 or signal_age > max_signal_age:
            return {"eligible": False, "reason": "CONFIRMATION_TIME_STALE_OR_FUTURE"}
        trade_date = str(state.get("trade_date_ist") or "").strip()[:10]
        if trade_date and trade_date != now.date().isoformat():
            return {"eligible": False, "reason": "ISOLATION_NOT_FROM_CURRENT_TRADING_DATE"}
        quote = self._live_feed.latest(target_key)
        quote_time = _dt((quote or {}).get("received_at"))
        if quote_time is None or quote_time.date() != now.date():
            return {"eligible": False, "reason": "NO_CURRENT_SESSION_LIVE_QUOTE"}
        quote_age = (now - quote_time).total_seconds()
        if quote_age < -2 or quote_age > int(getattr(self._config, "live_quote_max_age_seconds", 10)):
            return {"eligible": False, "reason": "LIVE_QUOTE_STALE"}
        entry = _market_ltp((quote or {}).get("data") or {})
        if entry is None or entry <= 0:
            return {"eligible": False, "reason": "LIVE_QUOTE_HAS_NO_VALID_LTP"}
        stop_loss_percent = float(getattr(self._config, "dummy_order_stop_loss_percent", 30))
        target_points = self._target_points(entry)
        raw_stop_loss = round(entry * (1 - stop_loss_percent / 100), 2)
        trade_date = str(state.get("trade_date_ist") or _now().date().isoformat())[:10]
        stop_loss, stop_loss_pivot = self._pivot_adjusted_stop_loss(
            target_key, trade_date, entry, raw_stop_loss
        )
        target_price = round(entry + target_points, 2)
        if not (0 < stop_loss < entry < target_price):
            return {"eligible": False, "reason": "INVALID_LIVE_ENTRY_OR_RISK_LEVELS"}
        return {
            "eligible": True, "reason": None, "entry": entry,
            "quote_received_at": quote_time.isoformat(),
            "quote_age_seconds": round(quote_age, 3),
            "signal_age_seconds": round(signal_age, 3),
            "frame_received_at": frame_time.isoformat(),
            "stop_loss_price": stop_loss, "raw_stop_loss_price": raw_stop_loss,
            "stop_loss_pivot": stop_loss_pivot,
            "target_points": target_points, "target_price": target_price,
        }

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

            date_key = self._date_key(payload, isolation_id)
            mongo_state = None
            if self._mongo_store is not None and hasattr(self._mongo_store, "load_isolation"):
                try:
                    mongo_state = await asyncio.to_thread(
                        self._mongo_store.load_isolation, date_key, isolation_id
                    )
                except Exception:
                    logger.exception("Mongo dummy-order restore failed date=%s iso=%s",
                                     date_key, isolation_id)

            if old is None or old.get("legs") != legs:
                state = {
                    "schema_version": 2, "isolation_id": isolation_id,
                    "trade_date_ist": payload.get("trade_date_ist") or self._date_key(payload, isolation_id),
                    "isolation_time": iso_time,
                    "legs": dict(legs), "trading_symbol": self._symbol(payload),
                    "isolated_instrument": instrument_context,
                    "status": "ACTIVE", "orders": {"supertrend": None},
                    "created_at": _now().isoformat(), "updated_at": _now().isoformat(),
                }
            else:
                state = old
                state["status"] = "ACTIVE"
                state["trading_symbol"] = self._symbol(payload) or state.get("trading_symbol")
                state["isolated_instrument"] = instrument_context
                state["updated_at"] = _now().isoformat()

            # Merge MongoDB order records into the local working state.
            # Local state remains preferred when it has a newer/different order;
            # Mongo fills missing state and completed snapshots that survived a
            # failed local write or an ephemeral local filesystem.
            if isinstance(mongo_state, dict):
                orders = state.setdefault("orders", {})
                local_current = orders.get("supertrend")
                mongo_current = mongo_state.get("current_order")
                if isinstance(mongo_current, dict):
                    if (
                        not isinstance(local_current, dict)
                        or str(local_current.get("status") or "").upper() == "DISABLED"
                    ):
                        orders["supertrend"] = dict(mongo_current)
                    else:
                        local_id = str(local_current.get("order_id") or "")
                        mongo_id = str(mongo_current.get("order_id") or "")
                        if local_id and local_id == mongo_id:
                            local_status = str(local_current.get("status") or "").upper()
                            mongo_status = str(mongo_current.get("status") or "").upper()
                            if local_status == "DUMMY_PLACED" and mongo_status.startswith("COMPLETED"):
                                orders["supertrend"] = dict(mongo_current)
                merged_history = {}
                for item in list(state.get("order_history") or []) + list(mongo_state.get("order_history") or []):
                    if not isinstance(item, dict):
                        continue
                    identity = str(item.get("order_id") or item.get("started_at_ist")
                                   or item.get("entry_time_ist") or "")
                    if identity:
                        merged_history[identity] = dict(item)
                current = orders.get("supertrend")
                current_id = str((current or {}).get("order_id") or
                                 (current or {}).get("started_at_ist") or
                                 (current or {}).get("entry_time_ist") or "")
                if current_id:
                    merged_history.pop(current_id, None)
                state["order_history"] = sorted(
                    merged_history.values(),
                    key=lambda item: str(item.get("started_at_ist") or item.get("entry_time_ist") or ""),
                )[-500:]
                state["supertrend_order_count"] = max(
                    self._supertrend_order_count(state),
                    int(mongo_state.get("order_count") or 0),
                )
                if not state.get("isolated_instrument") and mongo_state.get("isolated_instrument"):
                    state["isolated_instrument"] = mongo_state["isolated_instrument"]
                if isinstance(current, dict):
                    logger.info(
                        "Mongo dummy-order restored iso=%s order_id=%s status=%s history=%d",
                        isolation_id, current.get("order_id"), current.get("status"),
                        len(state["order_history"]),
                    )

            # SR remains a live feed/state stream, but it no longer creates orders.
            # Remove legacy SR order slots/history from local active state safely.
            orders = state.setdefault("orders", {})
            orders.pop("sr", None)
            state["order_history"] = [
                item for item in (state.get("order_history") or [])
                if isinstance(item, dict)
                and "SUPERTREND" in str(item.get("indicator") or item.get("strategy") or "").upper()
            ]
            if self._indicator_enabled():
                if isinstance(orders.get("supertrend"), dict) and orders["supertrend"].get("status") == "DISABLED":
                    orders["supertrend"] = None
            else:
                orders["supertrend"] = {
                    "indicator": "SUPERTREND",
                    "status": "DISABLED",
                    "reason": "orders disabled due to flag false",
                    "placed_at": None,
                    "real_order_submitted": False,
                }
            state["supertrend_order_count"] = self._supertrend_order_count(state)
            state["supertrend_order_limit"] = MAX_SUPERTREND_ORDERS_PER_ISOLATION

            await asyncio.to_thread(self._store.save, isolation_id, state)
            date_key = self._date_key(payload, isolation_id)
            await self._persist_mongo_isolation(date_key, isolation_id, instrument_context)
            disabled = (state.get("orders") or {}).get("supertrend")
            if isinstance(disabled, dict) and disabled.get("status") == "DISABLED":
                await self._persist_mongo_order(
                    date_key, isolation_id, "supertrend", disabled, instrument_context
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
            await asyncio.to_thread(
                self._refresh_pivot_levels,
                list(legs.values()) + [getattr(self._config, "nifty_instrument_key", "")],
                date_key,
            )

    async def prepare_supertrend_recovery(self, isolation_id: str, cutoff: datetime,
                                          recovery_end: datetime) -> None:
        async with self._lock(isolation_id):
            state = await asyncio.to_thread(self._store.load, isolation_id)
            if not state or state.get("status") != "ACTIVE":
                return
            current = state.get("supertrend_startup_recovery") or {}
            open_order = (state.get("orders") or {}).get("supertrend")
            has_open_order = isinstance(open_order, dict) and open_order.get("status") == "DUMMY_PLACED"
            if current.get("status") in {"COMPLETED", "CAUGHT_UP", "SKIPPED_EXISTING_ORDER"} and not has_open_order:
                return
            state["supertrend_startup_recovery"] = {
                "status": "PENDING",
                "recovery_window_start": cutoff.isoformat(),
                "recovery_window_end": recovery_end.isoformat(),
                "message": "Replaying persisted open positions and missed isolation-specific candles before live processing.",
                "resuming_open_order": has_open_order,
                "updated_at": _now().isoformat(),
            }
            state["updated_at"] = _now().isoformat()
            await asyncio.to_thread(self._store.save, isolation_id, state)

    async def get_isolation_time(self, isolation_id: str) -> datetime | None:
        """Return the original persisted isolation timestamp (IST-aware)."""
        state = await asyncio.to_thread(self._store.load, isolation_id)
        return _dt(state.get("isolation_time")) if isinstance(state, dict) else None

    async def mark_closed(self, isolation_id: str) -> None:
        state = await asyncio.to_thread(self._store.load, isolation_id)
        if state:
            state["status"], state["updated_at"] = "CLOSED", _now().isoformat()
            await asyncio.to_thread(self._store.save, isolation_id, state)

    async def on_supertrend_frame(
        self, payload: dict, instrument_key: str, leg_role: str, isolation_id: str,
        *, event_origin: str | None = None,
        live_event_received_at: str | None = None,
    ) -> None:
        if not self._indicator_enabled() or payload.get("event") not in {"snapshot", "supertrend_signal"}:
            return
        if leg_role != "isolated":
            return
        try:
            # Snapshots are history/recovery inputs only and can never fall
            # through into live order creation, whatever the recovery status.
            if payload.get("event") == "snapshot":
                # Local Upstox HistoryV3 recovery is authoritative for live
                # isolations. Remote snapshots are persisted for UI/diagnostics,
                # but do not start a second differently-windowed replay.
                return
            await self._process(
                payload, instrument_key, leg_role, isolation_id,
                event_origin=event_origin,
                live_event_received_at=live_event_received_at,
            )
        except Exception:
            logger.exception("Dummy Supertrend entry failed for %s", isolation_id)


    async def _process(
        self, payload: dict, source_key: str, source_role: str, isolation_id: str,
        *, event_origin: str | None = None,
        live_event_received_at: str | None = None,
    ) -> None:
        indicator = "supertrend"
        async with self._lock(isolation_id):
            state = await asyncio.to_thread(self._store.load, isolation_id)
            if not state or state.get("status") != "ACTIVE":
                return
            if not self._indicator_enabled():
                return
            recovery = state.get("supertrend_startup_recovery") or {}
            if indicator == "supertrend" and recovery.get("status") in {"PENDING", "WAITING_FOR_HISTORY"}:
                return
            existing_order = (state.get("orders") or {}).get(indicator)
            # Supertrend orders may be reversed by a later opposite confirmed
            # signal while the current order is open; completed entries may be
            # followed by another entry until the per-isolation cap is reached.
            if existing_order and existing_order.get("status") != "DISABLED":
                existing_status = str(existing_order.get("status") or "").upper()
                if not (
                    existing_status in {"DUMMY_PLACED", "DUMMY_SIGNAL_RECORDED_NO_QUOTE"}
                    or existing_status.startswith("COMPLETED")
                ):
                    return
            meta = self._meta.get(isolation_id) or {
                "legs": state.get("legs") or {},
                "cutoff": _dt(state.get("isolation_time")) or _now(),
                "instrument_context": state.get("isolated_instrument") or {},
                "instrument_meta": {},
                "date_key": self._date_key(state, isolation_id),
            }
            candidates = _st_candidates(payload)
            # Durable idempotency: remote WS and local Upstox polling can find
            # the same flip. Never process one confirmed candle twice.
            persisted_orders = list(state.get("order_history") or [])
            current_order = (state.get("orders") or {}).get(indicator)
            if isinstance(current_order, dict):
                persisted_orders.append(current_order)
            processed_times = {
                str((item.get("trigger") or {}).get("confirmed_time"))
                for item in persisted_orders if isinstance(item, dict)
                and (item.get("trigger") or {}).get("confirmed_time")
            }
            processed_times.update(
                str(value) for value in (state.get("supertrend_processed_signal_times") or [])
                if value
            )
            eligible = [x for x in candidates
                        if x["dt"] > meta["cutoff"] and x["time"] not in processed_times]
            if not eligible:
                return
            signal = min(eligible, key=lambda x: x["dt"])
            if not self._signal_in_entry_session(signal):
                logger.info("Supertrend signal before 09:15 IST ignored iso=%s signal_time=%s",
                            isolation_id, signal["time"])
                return

            order_count = self._supertrend_order_count(state)
            order_limit = self._resolve_supertrend_entry_limit(state, signal)
            signal_clock = signal["dt"].timetz().replace(tzinfo=None)
            entry_window_mode = state.get("supertrend_entry_window_mode") or (
                "LATE_SINGLE" if order_limit == 1 else "EARLY_TWO"
            )
            if order_count == 0 and signal_clock >= SUPERTREND_ENTRY_CUTOFF:
                entry_window_mode = "LATE_SINGLE"
                order_limit = 1
            # Early-session entries are allowed only before 09:40. A late
            # first signal (09:40 onward) gets exactly one entry.
            if entry_window_mode == "EARLY_TWO" and signal_clock >= SUPERTREND_ENTRY_CUTOFF:
                logger.info("Supertrend signal at/after 09:40 ignored iso=%s signal_time=%s; existing order remains active",
                            isolation_id, signal["time"])
                return
            if entry_window_mode == "LATE_SINGLE":
                order_limit = 1
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
            await asyncio.to_thread(
                self._refresh_pivot_levels,
                list((meta.get("legs") or {}).values())
                + [getattr(self._config, "nifty_instrument_key", "")],
                str(state.get("trade_date_ist") or self._date_key(state, isolation_id)),
            )
            eligibility = self._live_entry_eligibility(
                payload, signal, target_key, state, event_origin, live_event_received_at
            )
            if not eligibility.get("eligible"):
                logger.info(
                    "Supertrend live execution rejected iso=%s reason=%s signal_time=%s",
                    isolation_id, eligibility.get("reason"), signal.get("time"),
                )
                return
            # Enforce the entry cap before considering another entry. A rejected
            # later signal must never close or modify an existing position.
            if order_count >= order_limit:
                if (
                    isinstance(existing_order, dict)
                    and str(existing_order.get("status") or "").upper().startswith("COMPLETED")
                ):
                    self._archive_supertrend_order(state, existing_order)
                state["supertrend_order_count"] = order_count
                state["supertrend_order_limit"] = order_limit
                state["supertrend_entry_window_mode"] = entry_window_mode
                state["updated_at"] = _now().isoformat()
                await asyncio.to_thread(self._store.save, isolation_id, state)
                logger.info("Supertrend entry limit reached iso=%s entries=%d limit=%d signal_time=%s",
                            isolation_id, order_count, order_limit, signal["time"])
                return

            # An open first order must finish naturally. A later opposite signal
            # cannot prematurely close it or create a replacement entry.
            if isinstance(existing_order, dict) and existing_order.get("status") == "DUMMY_PLACED":
                logger.info(
                    "Supertrend second signal ignored while first order remains open iso=%s order_id=%s",
                    isolation_id, existing_order.get("order_id"),
                )
                await self._record_second_entry_decision(
                    state, isolation_id, existing_order, signal,
                    "FIRST_ORDER_STILL_OPEN",
                )
                return

            second_entry_context = None
            # The second entry is allowed only after a confirmed first-order loss.
            # A target touch (including a partial target exit) consumes the
            # second-entry opportunity permanently.
            if order_count >= 1:
                all_orders = list(state.get("order_history") or [])
                if isinstance(existing_order, dict):
                    all_orders.append(existing_order)
                unique_orders = {}
                for item in all_orders:
                    if isinstance(item, dict):
                        identity = str(item.get("order_id") or item.get("started_at_ist") or "")
                        if identity:
                            unique_orders[identity] = item
                first_order = next(
                    (item for item in unique_orders.values()
                     if int(item.get("order_sequence") or 0) == 1),
                    None,
                )
                if first_order is None and unique_orders:
                    first_order = min(
                        unique_orders.values(),
                        key=lambda item: str(item.get("started_at_ist") or item.get("entry_time_ist") or ""),
                    )
                first_status = str((first_order or {}).get("status") or "").upper()
                first_reason = str((first_order or {}).get("exit_reason") or "").upper()
                first_pnl = None
                try:
                    if (first_order or {}).get("pnl_net") is not None:
                        first_pnl = float(first_order["pnl_net"])
                except (TypeError, ValueError):
                    first_pnl = None
                target_reached = bool(
                    (first_order or {}).get("first_target_reached")
                    or (first_order or {}).get("partial_exit_done")
                    or first_reason == "TARGET"
                )
                confirmed_loss = (
                    first_status.startswith("COMPLETED")
                    and not target_reached
                    and (first_reason == "STOP_LOSS" or (first_pnl is not None and first_pnl < 0))
                )
                if target_reached:
                    logger.info(
                        "Supertrend second entry blocked: first order reached target iso=%s order_id=%s",
                        isolation_id, (first_order or {}).get("order_id"),
                    )
                    await self._record_second_entry_decision(
                        state, isolation_id, first_order, signal,
                        "FIRST_ORDER_TARGET_REACHED",
                    )
                    return
                if not confirmed_loss:
                    logger.info(
                        "Supertrend second entry blocked: first order outcome is not a confirmed loss iso=%s status=%s reason=%s",
                        isolation_id, first_status, first_reason,
                    )
                    await self._record_second_entry_decision(
                        state, isolation_id, first_order, signal,
                        "FIRST_ORDER_OUTCOME_UNCONFIRMED",
                    )
                    return
                second_entry_context = {
                    "status": "ALLOWED_AFTER_FIRST_ORDER_LOSS",
                    "first_order_id": (first_order or {}).get("order_id"),
                    "first_order_exit_reason": first_reason,
                    "first_order_pnl": first_pnl,
                    "signal_time": signal["time"],
                    "decided_at_ist": _now().isoformat(),
                }

            # Completed orders can be followed by another confirmed signal,
            # but the per-isolation entry cap remains authoritative.
            if isinstance(existing_order, dict) and str(existing_order.get("status") or "").startswith("COMPLETED"):
                self._archive_supertrend_order(state, existing_order)

            order_count = self._supertrend_order_count(state)
            if order_count >= order_limit:
                state["supertrend_order_count"] = order_count
                state["supertrend_order_limit"] = order_limit
                state["supertrend_entry_window_mode"] = entry_window_mode
                state["updated_at"] = _now().isoformat()
                await asyncio.to_thread(self._store.save, isolation_id, state)
                logger.info("Supertrend entry limit reached iso=%s entries=%d limit=%d",
                            isolation_id, order_count, MAX_SUPERTREND_ORDERS_PER_ISOLATION)
                return

            # Entry comes only from a fresh current-session live-feed LTP.
            # Signal payload prices and persisted stale quotes are never fallbacks.
            entry = eligibility["entry"]
            quote_received_at = eligibility["quote_received_at"]
            now_ist = _now()
            target_meta = (meta.get("instrument_meta") or {}).get(role) or {}
            stop_loss_percent = float(getattr(self._config, "dummy_order_stop_loss_percent", 30))
            target_points = eligibility["target_points"]
            order = {
                "order_id": f"DUMMY-{indicator.upper()}-{isolation_id}-{signal['dt'].strftime('%H%M%S')}",
                "isolation_id": isolation_id,
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
                # Keep verified instrument metadata separate from the display/PnL fallback.
                "instrument_lot_size": (
                    int(target_meta["lot_size"])
                    if target_meta.get("lot_size") not in (None, "", 0, "0")
                    else None
                ),
                "quantity_units": int(self._config.dummy_order_quantity) * int(target_meta.get("lot_size") or 1),
                "original_quantity": int(self._config.dummy_order_quantity),
                "original_quantity_units": int(self._config.dummy_order_quantity) * int(target_meta.get("lot_size") or 1),
                "remaining_quantity_lots": int(self._config.dummy_order_quantity),
                "remaining_quantity_units": int(self._config.dummy_order_quantity) * int(target_meta.get("lot_size") or 1),
                "partial_exit_done": False,
                "first_target_reached": False,
                "realized_pnl_gross": 0.0,
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
                "stop_loss_price": eligibility["stop_loss_price"],
                "raw_stop_loss_price": eligibility.get("raw_stop_loss_price"),
                "stop_loss_pivot": eligibility.get("stop_loss_pivot"),
                "target_points": target_points,
                "target_price": eligibility["target_price"],
                "target_rule": (
                    "entry > upper threshold" if entry is not None and entry > float(getattr(self._config, "dummy_target_upper_threshold", 200))
                    else "lower threshold <= entry <= upper threshold" if entry is not None and entry >= float(getattr(self._config, "dummy_target_lower_threshold", 100))
                    else "entry < lower threshold" if entry is not None else None
                ),
                "price_source": "fresh_live_market_feed",
                "quote_received_at": quote_received_at,
                "quote_age_seconds": eligibility["quote_age_seconds"],
                "live_execution_gate": {
                    "event_origin": event_origin,
                    "frame_received_at": eligibility["frame_received_at"],
                    "signal_age_seconds": eligibility["signal_age_seconds"],
                    "quote_age_seconds": eligibility["quote_age_seconds"],
                },
                "trigger": trigger, "placed_at": now_ist.isoformat(),
                "second_entry_decision": second_entry_context,
                "previous_order_id": (second_entry_context or {}).get("first_order_id"),
                "order_sequence": order_count + 1,
                "max_orders_per_isolation": order_limit,
                "status": "DUMMY_PLACED",
                "broker_order_id": None, "real_order_submitted": False,
            }
            state.setdefault("orders", {})[indicator] = order
            state["supertrend_order_count"] = order_count + 1
            state["supertrend_order_limit"] = order_limit
            state["supertrend_entry_window_mode"] = entry_window_mode
            state["updated_at"] = now_ist.isoformat()
            await asyncio.to_thread(self._store.save, isolation_id, state)
            await self._persist_mongo_order(
                meta.get("date_key") or self._date_key(state, isolation_id),
                isolation_id, indicator, order, meta.get("instrument_context") or {}
            )
            self._index_open_orders(isolation_id, state)
            logger.info("Dummy %s entry saved iso=%s target=%s trigger=%s",
                        indicator, isolation_id, target_key, trigger.get("confirmed_time"))
            # OrderFlow is intentionally dummy-only: no broker or GTT call is made.
            await self._notify_entry(state, order)            await self._notify_entry(state, order)



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
                if self._apply_partial_target(order, stamp, target):
                    # OHLC cannot reveal whether the post-target stop traded
                    # before or after target. Use the conservative outcome.
                    if low <= float(order["stop_loss_price"]):
                        return True, {
                            "reason": "STOP_LOSS",
                            "price": float(order["stop_loss_price"]),
                            "time": stamp,
                        }, stamp
                    stop = float(order["stop_loss_price"])
                    continue
                return True, {"reason": "TARGET", "price": target, "time": stamp}, stamp
        return False, None, last_stamp

    def _recovery_order(self, isolation_id: str, signal: dict, role: str,
                        key: str, candle: dict, meta: dict) -> dict:
        entry = float(candle.get("open") or candle.get("close"))
        entry_time = self._candle_dt(candle) or signal["dt"]
        target_points = self._target_points(entry)
        stop_percent = float(getattr(self._config, "dummy_order_stop_loss_percent", 30))
        raw_stop = round(entry * (1 - stop_percent / 100), 2)
        trade_date = str(meta.get("date_key") or isolation_id.split("__", 1)[0])[:10]
        stop_loss, stop_loss_pivot = self._pivot_adjusted_stop_loss(key, trade_date, entry, raw_stop)
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
            "original_quantity": quantity, "original_quantity_units": quantity * lot_size,
            "remaining_quantity_lots": quantity, "remaining_quantity_units": quantity * lot_size,
            "partial_exit_done": False, "first_target_reached": False,
            "realized_pnl_gross": 0.0,
            "entry_price": entry, "entry_time_ist": entry_time.isoformat(),
            "started_at_ist": entry_time.isoformat(), "high_price": entry,
            "high_time_ist": entry_time.isoformat(), "low_price": entry,
            "low_time_ist": entry_time.isoformat(), "stop_loss_percent": stop_percent,
            "raw_stop_loss_price": raw_stop, "stop_loss_price": stop_loss,
            "stop_loss_pivot": stop_loss_pivot,
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
        points, gross, net = self._total_order_pnl(order, price)
        order.update({
            "status": ("COMPLETED_TARGET" if reason == "TARGET" else
                       "COMPLETED_STOP_LOSS" if reason == "STOP_LOSS" else
                       "COMPLETED_SIGNAL_REVERSAL"),
            "exit_reason": reason, "exit_price": round(float(price), 2),
            "completed_at_ist": stamp.isoformat(),
            "duration_seconds": round((stamp - started).total_seconds(), 3) if started else None,
            "pnl_points": points, "pnl_gross": gross, "estimated_costs": 0.0,
            "pnl_net": net, "pnl_currency": "INR",
            "pnl_cost_model": "gross_only_costs_not_configured",
        })

    async def _recover_supertrend_history(
        self, isolation_id: str, payload: dict, *,
        candles_by_role: dict | None = None,
        recovery_start: datetime | None = None,
        recovery_end: datetime | None = None,
        recovery_as_of: datetime | None = None,
        recovery_source: str = "startup_history_replay",
    ) -> None:
        if self._candle_store is None and not candles_by_role:
            return
        async with self._lock(isolation_id):
            state = await asyncio.to_thread(self._store.load, isolation_id)
            if not state or state.get("status") != "ACTIVE":
                return
            previous_recovery = state.get("supertrend_startup_recovery") or {}
            existing_open = (state.get("orders") or {}).get("supertrend")
            has_open_order = (
                isinstance(existing_open, dict)
                and existing_open.get("status") == "DUMMY_PLACED"
            )
            if (
                previous_recovery.get("status") in {"COMPLETED", "CAUGHT_UP", "SKIPPED_EXISTING_ORDER"}
                and not has_open_order
            ):
                return
            trade_date = state.get("trade_date_ist") or isolation_id.split("__", 1)[0]
            cutoff = _dt(state.get("isolation_time")) or self._startup_at
            window_start = _dt(recovery_start) if recovery_start else cutoff
            window_end = _dt(recovery_end) if recovery_end else self._startup_at
            as_of = _dt(recovery_as_of) if recovery_as_of else self._startup_at
            signals = sorted((item for item in _st_candidates(payload)
                              if window_start < item["dt"] <= window_end
                              and self._signal_in_entry_session(item)),
                             key=lambda item: item["dt"])
            # Never replay signals already represented by a persisted entry.
            persisted_orders = list(state.get("order_history") or [])
            current_persisted = (state.get("orders") or {}).get("supertrend")
            if isinstance(current_persisted, dict):
                persisted_orders.append(current_persisted)
            processed_signal_times = {
                str((item.get("trigger") or {}).get("confirmed_time"))
                for item in persisted_orders if isinstance(item, dict)
                and (item.get("trigger") or {}).get("confirmed_time")
            }
            processed_signal_times.update(
                str(value) for value in (state.get("supertrend_processed_signal_times") or [])
                if value
            )
            signals = [item for item in signals if item["time"] not in processed_signal_times]
            persisted_count = self._supertrend_order_count(state)
            if persisted_count == 0 and signals:
                entry_window_mode = (
                    "LATE_SINGLE" if signals[0]["dt"].timetz().replace(tzinfo=None) >= SUPERTREND_ENTRY_CUTOFF
                    else "EARLY_TWO"
                )
            else:
                resolved_limit = self._resolve_supertrend_entry_limit(state)
                entry_window_mode = state.get("supertrend_entry_window_mode") or (
                    "LATE_SINGLE" if resolved_limit == 1 else "EARLY_TWO"
                )
            order_limit = 1 if entry_window_mode == "LATE_SINGLE" else 2
            if entry_window_mode == "EARLY_TWO":
                signals = [item for item in signals
                           if item["dt"].timetz().replace(tzinfo=None) < SUPERTREND_ENTRY_CUTOFF]
            elif signals:
                signals = signals[:1]
            state["supertrend_entry_window_mode"] = entry_window_mode
            state["supertrend_order_limit"] = order_limit
            persisted_order = (state.get("orders") or {}).get("supertrend")
            persisted_count = self._supertrend_order_count(state)
            if persisted_count >= order_limit and not (
                isinstance(persisted_order, dict) and persisted_order.get("status") == "DUMMY_PLACED"
            ):
                restored_orders, completed_orders = self._startup_recovery_order_counts(persisted_orders)
                state["supertrend_order_count"] = persisted_count
                state["supertrend_order_limit"] = order_limit
                state["supertrend_entry_window_mode"] = entry_window_mode
                state["supertrend_startup_recovery"] = {
                    "status": "COMPLETED", "started_at_ist": self._startup_at.isoformat(),
                    "completed_at_ist": _now().isoformat(),
                    "signals_found": len(signals),
                    "orders_restored": restored_orders,
                    "orders_replayed": restored_orders,
                    "orders_completed": completed_orders,
                    "order_limit": order_limit,
                    "order_limit_reached": True,
                    "message": "The per-isolation Supertrend entry limit was already reached; historical replay was skipped.",
                }
                await asyncio.to_thread(self._store.save, isolation_id, state)
                self._index_open_orders(isolation_id, state)
                return
            meta = self._meta.get(isolation_id) or {
                "legs": state.get("legs") or {}, "instrument_context": state.get("isolated_instrument") or {},
                "instrument_meta": {}, "date_key": trade_date,
            }
            keys = {role: meta["legs"].get(role) for role in ("isolated", "opposite")}
            if candles_by_role is not None:
                candles = {role: list(candles_by_role.get(role) or [])
                           for role in ("isolated", "opposite")}
            else:
                candles = {role: self._candle_store.read_day(key, trade_date) if key else []
                           for role, key in keys.items()}
            candles_by_key = {
                key: candles.get(role) or []
                for role, key in keys.items() if key
            }
            await asyncio.to_thread(
                self._refresh_pivot_levels,
                list(keys.values()) + [getattr(self._config, "nifty_instrument_key", "")],
                trade_date,
                candles_by_key,
            )
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


            history = list(state.get("order_history") or [])
            # A completed order can still occupy the current slot after restart.
            # Include it in the replay ledger so second-entry eligibility can
            # inspect its actual target/stop outcome.
            if isinstance(persisted_order, dict) and str(persisted_order.get("status") or "").upper().startswith("COMPLETED"):
                persisted_id = str(persisted_order.get("order_id") or "")
                if not any(str(item.get("order_id") or "") == persisted_id for item in history if isinstance(item, dict)):
                    history.append(dict(persisted_order))
            initial_history_count = len(history)
            order_count = self._supertrend_order_count(state)
            active, active_role, active_candles, last_processed = None, None, None, None
            replayed, limit_reached = 0, False

            # Resume a persisted open dummy position instead of declaring
            # recovery complete. The application may have been offline while
            # its stop-loss/target was touched; replay candles since its last
            # safely knowable entry boundary before accepting newer signals.
            if isinstance(persisted_order, dict) and persisted_order.get("status") == "DUMMY_PLACED":
                active = persisted_order
                active_role = str(active.get("target_leg_role") or "").lower()
                if active_role not in {"isolated", "opposite"}:
                    target_key = active.get("target_instrument_key") or active.get("instrument_key")
                    active_role = next((role for role, key in keys.items() if key == target_key), None)
                if active_role not in {"isolated", "opposite"} or not candles.get(active_role):
                    state["supertrend_startup_recovery"] = {
                        "status": "WAITING_FOR_HISTORY",
                        "started_at_ist": as_of.isoformat(),
                        "message": "Cannot resume the persisted open dummy order without its target-leg candle history.",
                        "missing_candle_roles": [active_role] if active_role else ["unknown"],
                    }
                    state["updated_at"] = _now().isoformat()
                    await asyncio.to_thread(self._store.save, isolation_id, state)
                    return
                active_candles = candles[active_role]
                started = _dt(active.get("started_at_ist") or active.get("entry_time_ist"))
                if started is None:
                    state["supertrend_startup_recovery"] = {
                        "status": "WAITING_FOR_HISTORY",
                        "started_at_ist": as_of.isoformat(),
                        "message": "Persisted open dummy order has no valid entry timestamp.",
                    }
                    await asyncio.to_thread(self._store.save, isolation_id, state)
                    return
                if active.get("price_source") == "persisted_one_minute_ohlc":
                    last_processed = started
                else:
                    # A live LTP entry can occur part-way through a one-minute
                    # bar. Skip that partial bar because OHLC cannot tell
                    # whether its high/low happened before or after the fill.
                    last_processed = (
                        started if started.second == 0 and started.microsecond == 0
                        else started.replace(second=0, microsecond=0) + timedelta(minutes=1)
                    )

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
                    # A second signal never reverses an open position. Replay
                    # through the signal candle and wait for the actual outcome.
                    signal_candle_end = signal["dt"] + timedelta(minutes=1)
                    hit, result, scanned_through = self._simulate_candles(
                        active, active_candles or [],
                        last_processed or _dt(active["started_at_ist"]),
                        signal_candle_end)
                    if hit:
                        active["exit_reason"], active["exit_price"] = result["reason"], result["price"]
                        active["completed_at_ist"] = result["time"].isoformat()
                        await persist_closed(active)
                        closed_reason = result["reason"]
                        closed_target_reached = bool(active.get("first_target_reached"))
                        active = active_role = active_candles = last_processed = None
                        if closed_reason == "TARGET" or closed_target_reached:
                            limit_reached = True
                            break
                        if closed_reason != "STOP_LOSS":
                            limit_reached = True
                            break
                    else:
                        if scanned_through is not None:
                            last_processed = scanned_through + timedelta(minutes=1)
                        continue
                if order_count >= order_limit:
                    limit_reached = True
                    break
                recovery_second_entry_context = None
                if order_count >= 1:
                    prior_first = next(
                        (item for item in history if int(item.get("order_sequence") or 0) == 1),
                        history[0] if history else None,
                    )
                    prior_reason = str((prior_first or {}).get("exit_reason") or "").upper()
                    prior_pnl = (prior_first or {}).get("pnl_net")
                    try:
                        prior_pnl = float(prior_pnl) if prior_pnl is not None else None
                    except (TypeError, ValueError):
                        prior_pnl = None
                    prior_target = bool(
                        (prior_first or {}).get("first_target_reached")
                        or (prior_first or {}).get("partial_exit_done")
                        or prior_reason == "TARGET"
                    )
                    prior_loss = (
                        str((prior_first or {}).get("status") or "").upper().startswith("COMPLETED")
                        and not prior_target
                        and (prior_reason == "STOP_LOSS" or (prior_pnl is not None and prior_pnl < 0))
                    )
                    if prior_target or not prior_loss:
                        limit_reached = True
                        state["supertrend_second_entry_decision"] = {
                            "status": (
                                "BLOCKED_FIRST_ORDER_PROFIT"
                                if prior_target else "BLOCKED_FIRST_ORDER_OUTCOME_UNKNOWN"
                            ),
                            "first_order_id": (prior_first or {}).get("order_id"),
                            "signal_time": signal["time"],
                            "decided_at_ist": _now().isoformat(),
                        }
                        processed = list(state.get("supertrend_processed_signal_times") or [])
                        if signal["time"] not in processed:
                            processed.append(signal["time"])
                        state["supertrend_processed_signal_times"] = processed[-500:]
                        continue
                    recovery_second_entry_context = {
                        "status": "ALLOWED_AFTER_FIRST_ORDER_LOSS",
                        "first_order_id": (prior_first or {}).get("order_id"),
                        "first_order_exit_reason": prior_reason,
                        "first_order_pnl": prior_pnl,
                        "signal_time": signal["time"],
                        "decided_at_ist": _now().isoformat(),
                    }
                entry_candle = next((bar for bar in role_candles
                                     if self._candle_dt(bar) is not None and
                                     self._candle_dt(bar) > signal["dt"] and
                                     self._candle_dt(bar) <= as_of), None)
                if entry_candle is None:
                    state["supertrend_startup_recovery"] = {
                        "status": "WAITING_FOR_HISTORY", "started_at_ist": self._startup_at.isoformat(),
                        "message": f"No post-confirmation entry candle is available for signal at {signal['time']}.",
                        "unpriced_signal_time": signal["time"],
                    }
                    await asyncio.to_thread(self._store.save, isolation_id, state)
                    return
                active = self._recovery_order(isolation_id, signal, role, key, entry_candle, meta)
                if recovery_second_entry_context:
                    active["second_entry_decision"] = recovery_second_entry_context
                    state["supertrend_second_entry_decision"] = recovery_second_entry_context
                order_count += 1
                active["order_sequence"] = order_count
                active["max_orders_per_isolation"] = order_limit
                active_role, active_candles = role, role_candles
                last_processed = self._candle_dt(entry_candle)
                replayed += 1

            if active is not None:
                hit, result, _ = self._simulate_candles(
                    active, active_candles or [], last_processed or _dt(active["started_at_ist"]),
                    as_of + timedelta(seconds=1))
                if hit:
                    active["exit_reason"], active["exit_price"] = result["reason"], result["price"]
                    active["completed_at_ist"] = result["time"].isoformat()
                    await persist_closed(active)
                    active = None

            if active is not None:
                state.setdefault("orders", {})["supertrend"] = active
                await self._persist_mongo_order(trade_date, isolation_id, "supertrend",
                                                active, meta.get("instrument_context") or {})
            elif history:
                state.setdefault("orders", {})["supertrend"] = history[-1]
            state["order_history"] = history[-500:]
            state["supertrend_order_count"] = order_count
            state["supertrend_order_limit"] = order_limit
            state["supertrend_entry_window_mode"] = entry_window_mode
            # Report cumulative persisted recovery state, not only work done
            # during this process start. Include the current order slot because
            # a completed order may not also be present in order_history.
            summary_orders = list(state.get("order_history") or [])
            current_summary_order = (state.get("orders") or {}).get("supertrend")
            if isinstance(current_summary_order, dict):
                summary_orders.append(current_summary_order)
            restored_orders, _ = self._startup_recovery_order_counts(persisted_orders)
            replayed_orders, completed_replayed_orders = self._startup_recovery_order_counts(summary_orders)
            state["supertrend_startup_recovery"] = {
                "status": "CAUGHT_UP" if active is not None else "COMPLETED",
                "started_at_ist": self._startup_at.isoformat(),
                "completed_at_ist": _now().isoformat(), "signals_found": len(signals),
                "orders_restored": restored_orders,
                "orders_replayed": replayed_orders,
                "orders_completed": completed_replayed_orders,
                "orders_replayed_this_run": replayed,
                "orders_completed_this_run": max(0, len(history) - initial_history_count),
                "order_limit": order_limit,
                "order_limit_reached": limit_reached or order_count >= order_limit,
                "method": "one_minute_ohlc_stop_first_if_both_levels_touch",
                "source": recovery_source,
                "recovery_window_start": window_start.isoformat(),
                "recovery_window_end": window_end.isoformat(),
                "recovery_as_of": as_of.isoformat(),
            }
            state["updated_at"] = _now().isoformat()
            await asyncio.to_thread(self._store.save, isolation_id, state)
            self._index_open_orders(isolation_id, state)
            logger.info("Supertrend startup recovery complete iso=%s signals=%d replayed=%d completed=%d limit_reached=%s",
                        isolation_id, len(signals), replayed_orders, completed_replayed_orders,
                        limit_reached or order_count >= order_limit)
            await self._notify_recovery(state)

    async def on_market_tick(self, instrument_key: str, data: dict,
                             received_at: str | None = None) -> None:
        price = _market_ltp(data)
        if price is None or price <= 0:
            return
        tick_time = _dt(received_at) or _now()
        if self._pivot_service is not None and self._candle_store is not None:
            minute_key = tick_time.replace(second=0, microsecond=0).isoformat()
            if self._pivot_refresh_last_minute.get(instrument_key) != minute_key:
                self._pivot_refresh_last_minute[instrument_key] = minute_key
                await asyncio.to_thread(
                    self._refresh_pivot_levels,
                    [instrument_key, getattr(self._config, "nifty_instrument_key", "")],
                    tick_time.date().isoformat(),
                )
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
            # A duplicate delivery of the exact tick that booked the first lot
            # must not immediately book the runner at its still-active target.
            partial = order.get("partial_exit") if isinstance(order.get("partial_exit"), dict) else {}
            partial_stamp = _dt(partial.get("exit_time_ist"))
            if order.get("partial_exit_done") and partial_stamp and tick_time <= partial_stamp:
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
            partial_exit = False
            stop, target = order.get("stop_loss_price"), order.get("target_price")
            if stop is not None and price <= float(stop):
                reason = "STOP_LOSS"
            elif target is not None and price >= float(target):
                if self._apply_partial_target(order, tick_time, float(target)):
                    partial_exit = True
                else:
                    reason = "TARGET"
            if reason:
                started = _dt(order.get("started_at_ist") or order.get("entry_time_ist"))
                order.update({
                    "status": "COMPLETED_STOP_LOSS" if reason == "STOP_LOSS" else "COMPLETED_TARGET",
                    "exit_reason": reason, "exit_price": price,
                    "completed_at_ist": stamp,
                    "duration_seconds": round((tick_time - started).total_seconds(), 3) if started else None,
                })
                pnl_points, pnl_gross, pnl_net = self._total_order_pnl(order, price)
                order.update({"pnl_points": pnl_points, "pnl_gross": pnl_gross,
                              "estimated_costs": 0.0, "pnl_net": pnl_net,
                              "pnl_currency": "INR", "pnl_cost_model": "gross_only_costs_not_configured"})
                changed = True
            if partial_exit:
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
            elif partial_exit:
                logger.info(
                    "Dummy %s partial target exit iso=%s order=%s remaining_lots=%s new_sl=%s",
                    indicator, isolation_id, order.get("order_id"),
                    order.get("quantity"), order.get("stop_loss_price"),
                )
                await self._notify_partial_exit(state, order)

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

    async def _record_second_entry_decision(
        self, state: dict, isolation_id: str, order: dict | None,
        signal: dict, reason: str,
    ) -> None:
        status = {
            "FIRST_ORDER_STILL_OPEN": "BLOCKED_FIRST_ORDER_STILL_OPEN",
            "FIRST_ORDER_TARGET_REACHED": "BLOCKED_FIRST_ORDER_PROFIT",
            "FIRST_ORDER_OUTCOME_UNCONFIRMED": "BLOCKED_FIRST_ORDER_OUTCOME_UNKNOWN",
        }.get(reason, "BLOCKED")
        decision = {
            "status": status,
            "reason": reason,
            "first_order_id": (order or {}).get("order_id"),
            "first_order_exit_reason": (order or {}).get("exit_reason"),
            "signal_time": signal.get("time"),
            "decided_at_ist": _now().isoformat(),
        }
        state["supertrend_second_entry_decision"] = decision
        processed = list(state.get("supertrend_processed_signal_times") or [])
        signal_time = str(signal.get("time") or "")
        if signal_time and signal_time not in processed:
            processed.append(signal_time)
        state["supertrend_processed_signal_times"] = processed[-500:]
        if isinstance(order, dict):
            order["second_entry_decision"] = decision
        state["updated_at"] = _now().isoformat()
        await asyncio.to_thread(self._store.save, isolation_id, state)
        if isinstance(order, dict):
            await self._persist_mongo_order_update(
                self._date_key(state, isolation_id), isolation_id, "supertrend",
                order, state.get("isolated_instrument") or {},
                completed=str(order.get("status") or "").upper().startswith("COMPLETED"),
            )
        if self._telegram is not None:
            try:
                await self._send_formatted_telegram(
                    format_second_entry_blocked_notification(
                        state, order, signal, reason
                    ),
                    "Second entry blocked",
                    "WARNING",
                )
            except Exception:
                logger.exception("Second-entry decision Telegram notification failed")

    async def _notify_partial_exit(self, state: dict, order: dict) -> None:
        if self._telegram is None:
            return
        try:
            await self._send_formatted_telegram(
                format_order_partial_exit_notification(state, order),
                "Simulated partial target",
                "SUCCESS",
            )
        except Exception:
            logger.exception("Dummy order partial-exit Telegram notification failed")

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
        isolation_id = str(state.get("isolation_id") or "")
        if isolation_id in self._startup_backfill_isolation_ids:
            logger.info(
                "Per-isolation recovery Telegram suppressed; included in startup digest iso=%s",
                isolation_id,
            )
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
        result["supertrend_orders_enabled"] = self._indicator_enabled()
        result["supertrend_order_limit"] = MAX_SUPERTREND_ORDERS_PER_ISOLATION
        states = await asyncio.to_thread(self._store.list_all)
        result["startup_recovery_waiting_count"] = sum(
            (x.get("supertrend_startup_recovery") or {}).get("status") == "WAITING_FOR_HISTORY"
            for x in states)
        return result
