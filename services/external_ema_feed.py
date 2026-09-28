"""External EMA WebSocket client for the selected isolated instrument."""

import asyncio
import json
from pathlib import Path
from threading import Lock
from urllib.parse import quote

import websockets

from core import config
from core.logger import get_logger
from services.opening_range.ema_alerts import get_selected_or_instrument_key
from services.option_service import get_contract_info_by_instrument_key
from services.opening_range_service import (
    get_opening_range_levels_for_ema_event,
    process_selected_or_ema_cross_alert,
)
from ws_feed.broadcaster import broadcaster

logger = get_logger(__file__)


class ExternalEMAFeed:
    def __init__(self):
        self.base_url = getattr(
            config, "EXTERNAL_EMA_WEBSOCKET_URL", "wss://tfeed.up.railway.app/ws/ema"
        ).rstrip("/")
        self.output_file = Path(
            getattr(config, "EXTERNAL_EMA_OUTPUT_FILE", "data/external_ema_feed.jsonl")
        )
        self.reconnect_seconds = max(
            1, int(getattr(config, "EXTERNAL_EMA_RECONNECT_SECONDS", 5))
        )
        self.task = None
        self.is_running = False
        self.connected = False
        self.instrument_key = None
        self.messages_received = 0
        self.crossovers_received = 0
        self.last_error = None
        self._state_lock = Lock()
        self.instrument_states = {}

    async def start(self):
        if self.is_running:
            return
        self.is_running = True
        self.task = asyncio.create_task(self._run(), name="external-ema-feed")
        logger.info("External EMA feed started: %s", self.base_url)

    async def stop(self):
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            finally:
                self.task = None
        self.connected = False
        self.instrument_key = None

    def get_status(self):
        return {
            "enabled": True,
            "is_running": self.is_running,
            "connected": self.connected,
            "instrument_key": self.instrument_key,
            "messages_received": self.messages_received,
            "crossovers_received": self.crossovers_received,
            "output_file": str(self.output_file),
            "last_error": self.last_error,
        }

    def get_instrument_state(self, instrument_key):
        with self._state_lock:
            state = self.instrument_states.get(str(instrument_key or ""))
            if not isinstance(state, dict):
                return None
            result = dict(state)
        result.setdefault("previous_ema_fast", result.get("ema_9"))
        result.setdefault("previous_ema_slow", result.get("ema_21"))
        result.setdefault("fast_period", 9)
        result.setdefault("slow_period", 21)
        return result

    async def _run(self):
        while self.is_running:
            selected_key = get_selected_or_instrument_key()
            if not selected_key:
                self.connected = False
                self.instrument_key = None
                await asyncio.sleep(self.reconnect_seconds)
                continue
            url = f"{self.base_url}/{quote(selected_key, safe='')}"
            try:
                logger.info("Connecting external EMA feed for %s", selected_key)
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20, close_timeout=5
                ) as socket:
                    self.connected = True
                    self.instrument_key = selected_key
                    self.last_error = None
                    while self.is_running and get_selected_or_instrument_key() == selected_key:
                        try:
                            raw_message = await asyncio.wait_for(socket.recv(), timeout=2)
                        except asyncio.TimeoutError:
                            continue
                        try:
                            payload = json.loads(raw_message)
                        except (TypeError, json.JSONDecodeError):
                            logger.warning("Ignoring invalid external EMA message")
                            continue
                        if isinstance(payload, dict):
                            await self._handle_message(payload, selected_key)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("External EMA feed disconnected for %s: %s", selected_key, self.last_error)
            finally:
                self.connected = False
                self.instrument_key = None
            if self.is_running:
                await asyncio.sleep(self.reconnect_seconds)

    def _save_message(self, payload):
        try:
            self.output_file.parent.mkdir(parents=True, exist_ok=True)
            with self.output_file.open("a", encoding="utf-8") as output:
                output.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except OSError:
            logger.exception("Could not persist external EMA message to %s", self.output_file)

    async def _handle_message(self, payload, selected_key):
        self.messages_received += 1
        self._save_message(payload)
        if payload.get("event") == "initial_state":
            instruments = payload.get("instruments") or []
            with self._state_lock:
                for item in instruments:
                    if not isinstance(item, dict):
                        continue
                    key = item.get("instrument_key")
                    feed_state = item.get("state") or {}
                    if key and isinstance(feed_state, dict):
                        self.instrument_states[str(key)] = {
                            **feed_state,
                            "instrument_key": key,
                            "symbol": item.get("symbol"),
                            "contract": item.get("contract") or {},
                        }
            return
        if payload.get("event_type") != "ema.crossover":
            return

        raw_event = payload.get("event")
        if not isinstance(raw_event, dict):
            return
        event = dict(raw_event)
        instrument_key = payload.get("instrument_key") or selected_key
        with self._state_lock:
            cached_state = dict(self.instrument_states.get(instrument_key, {}))
            cached_state.update({
                "ema_9": event.get("ema_9", cached_state.get("ema_9")),
                "ema_21": event.get("ema_21", cached_state.get("ema_21")),
                "ema_difference": event.get("ema_difference", cached_state.get("ema_difference")),
                "last_processed_timestamp": event.get("timestamp", cached_state.get("last_processed_timestamp")),
            })
            cached_state["previous_ema_fast"] = cached_state.get("ema_9")
            cached_state["previous_ema_slow"] = cached_state.get("ema_21")
            self.instrument_states[instrument_key] = cached_state
        event.update({
            "type": "live_ema_cross",
            "event_type": "ema.crossover",
            "source": "external_ema_feed",
            "instrument_key": instrument_key,
            "symbol": payload.get("symbol"),
            "trading_symbol": payload.get("trading_symbol") or payload.get("symbol"),
            "sequence": payload.get("sequence"),
            "published_at": payload.get("published_at"),
            "ema_calculation_mode": payload.get("mode", "live"),
            "cross_type": event.get("cross_type") or (
                "bullish" if event.get("is_bullish_cross") else "bearish"
            ),
            "timestamp": event.get("timestamp") or payload.get("published_at"),
        })
        contract = get_contract_info_by_instrument_key(event["instrument_key"])
        if contract:
            event["contract_info"] = contract
            event.setdefault("strike_price", contract.get("strike_price"))
            event.setdefault("option_type", contract.get("instrument_type"))
            event.setdefault("underlying", contract.get("underlying_symbol"))
        try:
            levels = get_opening_range_levels_for_ema_event(event["instrument_key"])
            if isinstance(levels, dict):
                event.update(levels)
        except Exception:
            logger.exception("Opening Range enrichment failed for external EMA event")

        self.crossovers_received += 1
        try:
            process_selected_or_ema_cross_alert(event)
        except Exception:
            logger.exception("External EMA alert processing failed")
        try:
            await broadcaster.broadcast_ema_cross(event)
        except Exception:
            logger.exception("External EMA crossover broadcast failed")


external_ema_feed = ExternalEMAFeed()
