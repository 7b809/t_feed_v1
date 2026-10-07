"""Novag7 per-instrument live feed manager and normalized tick dispatch."""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import websockets

from core import config
from core.logger import get_logger
from services.ema_engine import internal_ema_engine
from services.opening_range_service import (
    flush_pending_touch_alerts,
    get_opening_range_status,
    process_live_tick_for_opening_range,
)
from services.option_service import get_feed_by_instrument_key, get_subscribed_instrument_keys, options_cache
from ws_feed.broadcaster import broadcaster

logger = get_logger(__file__)


@dataclass
class InstrumentConnection:
    instrument_key: str
    task: asyncio.Task | None = None
    websocket: Any = None
    connected: bool = False
    reconnect_count: int = 0
    last_received_at: str | None = None
    last_error: str | None = None


def normalize_novag7_message(instrument_key: str, message: Any) -> dict | None:
    """Convert Novag7 payload variants to the internal broadcaster feed shape."""
    try:
        payload = message if isinstance(message, dict) else json.loads(message)
        if not isinstance(payload, dict):
            return None
        if isinstance(payload.get("data"), dict) and ("feed" in payload["data"] or "instrument_key" in payload["data"]):
            payload = payload["data"]
        if payload.get("type") in {"connected", "subscribed", "ping", "pong", "heartbeat"}:
            return None
        source = payload.get("feed") if isinstance(payload.get("feed"), dict) else payload
        full = source.get("fullFeed") if isinstance(source.get("fullFeed"), dict) else source
        ff = full.get("ff") if isinstance(full.get("ff"), dict) else full
        market = ff.get("marketFF") or source.get("marketFF") or {}
        index = ff.get("indexFF") or source.get("indexFF") or {}
        is_market = isinstance(market, dict) and bool(market)
        source_ff = market if is_market else (index if isinstance(index, dict) else {})
        ltpc = source.get("ltpc") or source_ff.get("ltpc") or {}
        if not isinstance(ltpc, dict):
            ltpc = {}
        normalized = dict(source_ff)
        normalized["ltpc"] = ltpc
        for name in ("atp", "vtt", "oi", "iv", "uc", "lc", "optionGreeks", "marketLevel", "marketOHLC", "optionOHLC"):
            if name in source:
                normalized[name] = source[name]
        if "vtt" not in normalized:
            normalized["vtt"] = source_ff.get("volume", source.get("volume"))
        if "oi" not in normalized:
            normalized["oi"] = source_ff.get("open_interest", source.get("open_interest"))
        if not normalized.get("marketOHLC") and not normalized.get("optionOHLC"):
            ohlc = source_ff.get("ohlc") or source.get("ohlc")
            if isinstance(ohlc, dict):
                normalized["marketOHLC"] = {"ohlc": [dict(ohlc, interval=ohlc.get("interval", "1d"))]}
        greeks = source.get("optionGreeks") or source.get("option_greeks") or source.get("greeks")
        if isinstance(greeks, dict):
            normalized["optionGreeks"] = greeks
            normalized.setdefault("iv", greeks.get("iv", greeks.get("implied_volatility")))
        feed = dict(source)
        feed["ltpc"] = ltpc
        feed["marketFF" if is_market else "indexFF"] = normalized
        canonical = {"instrument_key": str(payload.get("instrument_key") or instrument_key), "feed": feed, "raw": payload}
        return {"raw_feed": {"fullFeed": {"ff": feed}}, **canonical}
    except (ValueError, TypeError, json.JSONDecodeError):
        logger.exception("Novag7 tick parsing failed instrument=%s", instrument_key)
        return None


@dataclass
class Novag7FeedManager:
    connections: dict[str, InstrumentConnection] = field(default_factory=dict)
    is_running: bool = False
    loop: asyncio.AbstractEventLoop | None = None
    reconcile_task: asyncio.Task | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    message_count: int = 0
    feed_count: int = 0
    broadcast_success_count: int = 0
    broadcast_failed_count: int = 0
    contract_match_count: int = 0
    contract_miss_count: int = 0
    opening_range_processed_count: int = 0
    opening_range_touch_count: int = 0
    opening_range_failed_count: int = 0
    opening_range_broadcast_count: int = 0
    opening_range_alert_flush_count: int = 0

    def _subscribed_keys(self) -> list[str]:
        try:
            keys = get_subscribed_instrument_keys()
        except Exception:
            logger.exception("Subscribed key helper failed; using subscription cache")
            keys = options_cache.get("subscribed_keys", [])
        normalized = {str(key).strip() for key in (keys or []) if str(key).strip()}
        main_index = str(getattr(config, "MAIN_NIFTY_SECURITY", "NSE_INDEX|Nifty 50")).strip()
        if main_index:
            normalized.add(main_index)
        return sorted(normalized)

    async def start(self) -> None:
        provider = str(getattr(config, "LIVE_FEED_PROVIDER", "novag7")).lower()
        if provider != "novag7":
            logger.error("Unsupported live feed provider configured: %s", provider)
            return
        if not bool(getattr(config, "NOVAG7_ENABLED", True)):
            logger.warning("Novag7 feed manager disabled by configuration")
            return
        if self.is_running:
            await self.reconcile_subscriptions()
            return
        self.is_running = True
        self.loop = asyncio.get_running_loop()
        self.reconcile_task = asyncio.create_task(self._reconcile_loop(), name="novag7-reconcile")
        logger.info("Novag7 feed manager started")
        await self.reconcile_subscriptions()

    async def stop(self) -> None:
        self.is_running = False
        reconcile = self.reconcile_task
        self.reconcile_task = None
        if reconcile and reconcile is not asyncio.current_task():
            reconcile.cancel()
            await asyncio.gather(reconcile, return_exceptions=True)
        async with self._lock:
            states = list(self.connections.values())
            self.connections.clear()
        for state in states:
            if state.websocket:
                try:
                    await state.websocket.close()
                except Exception:
                    logger.debug("Novag7 socket close failed instrument=%s", state.instrument_key, exc_info=True)
            if state.task and state.task is not asyncio.current_task():
                state.task.cancel()
        await asyncio.gather(*(state.task for state in states if state.task), return_exceptions=True)
        self.loop = None
        logger.info("Novag7 feed manager stopped; instrument connections closed=%s", len(states))

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def subscribe(self, instrument_key: str) -> None:
        key = str(instrument_key or "").strip()
        if not key or not self.is_running:
            return
        async with self._lock:
            if key in self.connections:
                return
            state = InstrumentConnection(instrument_key=key)
            self.connections[key] = state
            state.task = asyncio.create_task(self._instrument_loop(state), name="novag7-" + key)
        logger.info("Novag7 instrument subscribed instrument=%s", key)

    async def unsubscribe(self, instrument_key: str) -> None:
        key = str(instrument_key or "").strip()
        async with self._lock:
            state = self.connections.pop(key, None)
        if state is None:
            return
        if state.websocket:
            try:
                await state.websocket.close()
            except Exception:
                logger.debug("Novag7 socket close failed instrument=%s", key, exc_info=True)
        if state.task and state.task is not asyncio.current_task():
            state.task.cancel()
            await asyncio.gather(state.task, return_exceptions=True)
        logger.info("Novag7 instrument unsubscribed instrument=%s", key)

    async def reconcile_subscriptions(self) -> None:
        if not self.is_running:
            return
        desired = set(self._subscribed_keys())
        async with self._lock:
            current = set(self.connections)
        for key in current - desired:
            await self.unsubscribe(key)
        for key in sorted(desired - current):
            await self.subscribe(key)

    async def _reconcile_loop(self) -> None:
        interval = max(1.0, float(getattr(config, "NOVAG7_RECONCILE_INTERVAL", 5)))
        while self.is_running:
            try:
                await self.reconcile_subscriptions()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Novag7 subscription reconciliation failed")
            await asyncio.sleep(interval)

    async def _instrument_loop(self, state: InstrumentConnection) -> None:
        base = str(getattr(config, "NOVAG7_WS_BASE_URL", "wss://feed.novag7.in/ws/market")).rstrip("?")
        url = base + "?" + urlencode({"instrument_key": state.instrument_key})
        initial = max(1.0, float(getattr(config, "NOVAG7_RECONNECT_INITIAL_DELAY", 5)))
        maximum = max(initial, float(getattr(config, "NOVAG7_RECONNECT_MAX_DELAY", 30)))
        delay = initial
        while self.is_running and self.connections.get(state.instrument_key) is state:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=float(getattr(config, "NOVAG7_PING_INTERVAL", 20)),
                    ping_timeout=float(getattr(config, "NOVAG7_PING_TIMEOUT", 20)),
                    close_timeout=float(getattr(config, "NOVAG7_CLOSE_TIMEOUT", 10)),
                    max_size=None,
                ) as websocket:
                    state.websocket = websocket
                    state.connected = True
                    state.last_error = None
                    delay = initial
                    logger.info("Novag7 WebSocket connected instrument=%s", state.instrument_key)
                    async for message in websocket:
                        self.message_count += 1
                        state.last_received_at = datetime.now(timezone.utc).isoformat()
                        await self._dispatch_message(state.instrument_key, message)
                if self.is_running and self.connections.get(state.instrument_key) is state:
                    logger.warning("Novag7 WebSocket disconnected instrument=%s", state.instrument_key)
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                state.last_error = type(ex).__name__ + ": " + str(ex)
                logger.warning("Novag7 WebSocket error instrument=%s error=%s", state.instrument_key, state.last_error)
            finally:
                state.connected = False
                state.websocket = None
            if self.is_running and self.connections.get(state.instrument_key) is state:
                state.reconnect_count += 1
                logger.info("Novag7 reconnecting instrument=%s delay=%s", state.instrument_key, delay)
                await asyncio.sleep(delay)
                delay = min(maximum, delay * 2)

    async def _dispatch_message(self, instrument_key: str, message: Any) -> None:
        tick = normalize_novag7_message(instrument_key, message)
        if tick is None:
            return
        self.feed_count += 1
        contract = get_feed_by_instrument_key(instrument_key)
        self.contract_match_count += int(bool(contract))
        self.contract_miss_count += int(not contract)
        try:
            events = []
            if bool(getattr(config, "OPENING_RANGE_TOUCH_ALERT_ENABLED", True)):
                events = process_live_tick_for_opening_range(instrument_key, tick, contract) or []
                self.opening_range_processed_count += 1
                self.opening_range_touch_count += len(events)
            if broadcaster.get_active_connections_count() > 0:
                for event in events:
                    await broadcaster.broadcast_opening_range(event)
                    self.opening_range_broadcast_count += 1
        except Exception:
            self.opening_range_failed_count += 1
            logger.exception("Opening Range processing failed instrument=%s", instrument_key)
        if broadcaster.get_active_connections_count() > 0:
            try:
                await broadcaster.broadcast_tick(instrument_key, tick, contract)
                self.broadcast_success_count += 1
            except Exception:
                self.broadcast_failed_count += 1
                logger.exception("Tick broadcast failed instrument=%s", instrument_key)
        try:
            if bool(getattr(config, "OPENING_RANGE_TOUCH_ALERT_ENABLED", True)):
                self.opening_range_alert_flush_count += int(bool(flush_pending_touch_alerts(force=False, source="live_tick")))
        except Exception:
            logger.exception("Opening Range alert flush failed")

    def get_connected_instruments(self) -> list[str]:
        return sorted(key for key, state in self.connections.items() if state.connected)

    def get_status(self) -> dict:
        try:
            opening_range = get_opening_range_status()
        except Exception as ex:
            opening_range = {"status": "error", "error": type(ex).__name__ + ": " + str(ex)}
        ema_status = internal_ema_engine.get_status()
        return {
            "provider": "novag7", "is_running": self.is_running,
            "has_task": self.reconcile_task is not None,
            "subscribed_instruments": sorted(self.connections),
            "connected_instruments": self.get_connected_instruments(),
            "connection_count": len(self.connections), "connected_count": len(self.get_connected_instruments()),
            "connections": {key: {"connected": state.connected, "reconnect_count": state.reconnect_count,
                                  "last_received_at": state.last_received_at, "last_error": state.last_error}
                            for key, state in self.connections.items()},
            "message_count": self.message_count, "feed_count": self.feed_count,
            "broadcast_success_count": self.broadcast_success_count,
            "broadcast_failed_count": self.broadcast_failed_count,
            "contract_match_count": self.contract_match_count, "contract_miss_count": self.contract_miss_count,
            "live_ema_calculation_mode": "internal_completed_candle",
            "live_ema_calculation_mode_flag": True,
            "live_ema_calculation_mode_description": "Internal EMA engine processes completed one-minute candles",
            "live_ema_processed_count": ema_status.get("candles_processed", 0),
            "live_ema_cross_count": ema_status.get("crossovers_received", 0),
            "live_ema_failed_count": int(bool(ema_status.get("last_error"))),
            "ema_opening_range_enriched_count": 0,
            "ema_opening_range_enrichment_failed_count": 0,
            "isolated_ema_alert_processed_count": 0,
            "isolated_ema_alert_accepted_count": 0,
            "isolated_ema_alert_failed_count": 0,
            "selected_or_ema_alert_processed_count": 0,
            "selected_or_ema_alert_sent_count": 0,
            "selected_or_ema_alert_failed_count": 0,
            "selected_or_ema_alert_flow": "isolated_instrument_only",
            "telegram_enabled": bool(getattr(config, "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED", True)),
            "algo_app_enabled": bool(getattr(config, "ALGO_APP_ENABLED", False)),
            "algo_app_url_configured": bool(getattr(config, "ALGO_APP_URL", "")),
            "budget_range_enabled": bool(getattr(config, "EMA_ALERT_BUDGET_RANGE_ENABLED", True)),
            "budget_range_min_price": getattr(config, "EMA_ALERT_BUDGET_MIN_PRICE", 20.0),
            "budget_range_max_price": getattr(config, "EMA_ALERT_BUDGET_MAX_PRICE", 30.0),
            "budget_range_max_instruments": getattr(config, "EMA_ALERT_BUDGET_MAX_INSTRUMENTS", 2),
            "live_ema_status": ema_status,
            "opening_range_processed_count": self.opening_range_processed_count,
            "opening_range_touch_count": self.opening_range_touch_count,
            "opening_range_failed_count": self.opening_range_failed_count,
            "opening_range_broadcast_count": self.opening_range_broadcast_count,
            "opening_range_alert_flush_count": self.opening_range_alert_flush_count,
            "opening_range_status": opening_range,
        }


novag7_feed_manager = Novag7FeedManager()
