# app/services/isolation_subscriber.py
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from ..config import Settings
from .market_session import is_market_session_open
from .isolation_store import IsolationStore
from .sr_state_store import SrStateStore
from .sr_ws_client_service import SrWsClientService
from .supertrend_state_store import SupertrendStateStore
from .supertrend_ws_client_service import SupertrendWsClientService
from .live_feed_state_store import LiveFeedStateStore
from .live_feed_ws_client_service import LiveFeedWsClientService
from .dummy_order_engine import DummyOrderEngine
from .local_supertrend_service import LocalSupertrendService

logger = logging.getLogger(__name__)



def _instrument_label(instrument: Any, fallback: str) -> str:
    """Prefer a compact strike/type label, then provider symbol metadata."""
    if not isinstance(instrument, dict):
        return fallback
    strike = instrument.get("strike")
    option_type = instrument.get("option_type") or instrument.get("optionType")
    if strike not in (None, "") and option_type:
        try:
            strike_text = str(int(float(strike)))
        except (TypeError, ValueError):
            strike_text = str(strike).strip()
        return f"{strike_text}_{str(option_type).strip().lower()}"
    for field in ("trading_symbol", "tradingsymbol", "symbol", "instrument_name"):
        value = instrument.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return fallback


class IsolationSubscriber:
    """Orchestrates the two WS clients (isolated + opposite) per isolation."""

    def __init__(
        self,
        config: Settings,
        isolation_store: IsolationStore,
        state_store: SrStateStore,
        supertrend_state_store: SupertrendStateStore,
        live_feed_state_store: LiveFeedStateStore,
        dummy_order_engine: DummyOrderEngine | None = None,
        live_feed_candle_store=None,
    ) -> None:
        self._config = config
        self._isolation_store = isolation_store
        self._state_store = state_store
        self._supertrend_state_store = supertrend_state_store
        self._live_feed_client = LiveFeedWsClientService(
            config, live_feed_state_store, order_engine=dummy_order_engine,
            candle_store=live_feed_candle_store,
        )
        self._dummy_order_engine = dummy_order_engine
        self._local_supertrend_service = (
            LocalSupertrendService(config, dummy_order_engine)
            if dummy_order_engine is not None and config.supertrend_enabled else None
        )

        self._clients: Dict[str, SrWsClientService] = {}
        self._supertrend_clients: Dict[str, SupertrendWsClientService] = {}
        self._isolation_legs: Dict[str, Dict[str, str]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ lookups
    def get_client(self, instrument_key: str) -> Optional[SrWsClientService]:
        return self._clients.get(instrument_key)

    def list_clients(self) -> List[SrWsClientService]:
        return list(self._clients.values())

    def get_supertrend_client(self, instrument_key: str) -> Optional[SupertrendWsClientService]:
        return self._supertrend_clients.get(instrument_key)

    def list_supertrend_clients(self) -> List[SupertrendWsClientService]:
        return list(self._supertrend_clients.values())

    def get_live_feed_client(self) -> LiveFeedWsClientService:
        return self._live_feed_client

    def list_isolation_ids(self) -> List[str]:
        return list(self._isolation_legs.keys())

    # ------------------------------------------------------------ subscribe
    async def subscribe_isolation(
        self,
        payload: Dict[str, Any],
        isolation_id: str,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """Start (or refresh) the two WS clients for an isolation.

        * ``overwrite=False``: idempotent — existing clients are reused.
        * ``overwrite=True``: existing clients for this isolation are torn
          down and ``sr_state`` for both legs is purged before restarting.
        """
        status = payload.get("status")

        async with self._lock:
            # -------------------------------------------------- overwrite path
            if overwrite:
                logger.info(
                    "overwrite=true for %s — tearing down existing clients",
                    isolation_id,
                )
                await self._teardown_isolation(isolation_id)

                # Purge any sr_state left behind by the old subscription
                legs = self._legs_from_payload(payload)
                keys = [k for k in legs.values() if k]
                if keys:
                    await asyncio.to_thread(
                        self._state_store.delete_for_instrument_keys, keys
                    )
                    await asyncio.to_thread(
                        self._supertrend_state_store.delete_for_instrument_keys, keys
                    )

            # -------------------------------------------------- non-ACTIVE
            if status != "ACTIVE":
                if not overwrite:
                    await self._teardown_isolation(isolation_id)
                if self._dummy_order_engine is not None:
                    await self._dummy_order_engine.mark_closed(isolation_id)
                try:
                    self._isolation_store.set_status(isolation_id, "closed", [])
                except Exception:
                    logger.exception("set_status(closed) failed")
                return {
                    "subscribed": [],
                    "rejected_legs": [],
                    "web_socket_sessions": {},
                }

            iso_key = payload["instrument_key"]
            opp_key = (payload.get("opposite_leg") or {}).get("instrument_key")
            if not opp_key:
                return {
                    "subscribed": [],
                    "rejected_legs": ["opposite"],
                    "web_socket_sessions": {},
                }

            new_legs = {"isolated": iso_key, "opposite": opp_key}
            if self._dummy_order_engine is not None:
                await self._dummy_order_engine.register_isolation(
                    isolation_id, payload, new_legs, overwrite=overwrite
                )

            # If the same isolation previously had different legs → tear down
            old_legs = self._isolation_legs.get(isolation_id) or {}
            for role in ("isolated", "opposite"):
                old_key = old_legs.get(role)
                new_key = new_legs[role]
                if old_key and old_key != new_key:
                    existing = self._clients.get(old_key)
                    if existing is not None:
                        logger.warning(
                            "leg changed for %s: %s %s → %s; stopping old client",
                            isolation_id,
                            role,
                            old_key,
                            new_key,
                        )
                        await existing.stop()
                        self._clients.pop(old_key, None)
                    old_supertrend = self._supertrend_clients.pop(old_key, None)
                    if old_supertrend is not None:
                        await old_supertrend.stop()

            subscribed: List[str] = []
            sessions: Dict[str, Dict[str, str]] = {}

            for role, key in new_legs.items():
                existing = self._clients.get(key)
                if existing is not None:
                    sessions[key] = {"state": "already_running"}
                    subscribed.append(role)
                    continue

                client = SrWsClientService(
                    config=self._config,
                    state_store=self._state_store,
                    isolation_id=isolation_id,
                    instrument_key=key,
                    leg_role=role,
                )
                self._clients[key] = client
                await client.start()
                sessions[key] = {"state": "connecting"}
                subscribed.append(role)

            # Start an independent Supertrend connection for both legs.
            # SR remains subscribed and persisted independently; only Supertrend
            # frames are connected to the simulated-order engine.
            if self._config.supertrend_enabled:
                for role, key in new_legs.items():
                    existing_st = self._supertrend_clients.get(key)
                    if existing_st is not None:
                        sessions.setdefault(key, {})["supertrend_state"] = "already_running"
                        continue
                    st_client = SupertrendWsClientService(
                        config=self._config,
                        state_store=self._supertrend_state_store,
                        isolation_id=isolation_id,
                        instrument_key=key,
                        leg_role=role,
                        order_engine=self._dummy_order_engine,
                    )
                    self._supertrend_clients[key] = st_client
                    await st_client.start()
                    sessions.setdefault(key, {})["supertrend_state"] = "connecting"
            else:
                logger.info("Supertrend WS disabled; skipping both legs for %s", isolation_id)

            self._isolation_legs[isolation_id] = new_legs

            # After-hours startup: replay today's persisted Supertrend signals
            # against the saved one-minute candle files. No socket is opened and
            # no synthetic fill is generated when the candle history is missing.
            if (
                not is_market_session_open(self._config)
                and self._config.supertrend_enabled
                and self._dummy_order_engine is not None
            ):
                try:
                    history = await asyncio.to_thread(
                        self._supertrend_state_store.get_latest, iso_key, "isolated"
                    )
                    today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
                    if isinstance(history, dict) and history.get("today_ist_date") == today:
                        await self._dummy_order_engine._recover_supertrend_history(
                            isolation_id, {"event": "snapshot", "signals": history.get("signals") or []}
                        )
                        logger.info("After-hours Supertrend replay checked isolation=%s signals=%d",
                                    isolation_id, len(history.get("signals") or []))
                    else:
                        logger.info("After-hours Supertrend replay skipped isolation=%s reason=no today's persisted signal snapshot",
                                    isolation_id)
                except Exception:
                    logger.exception("After-hours Supertrend replay failed for %s", isolation_id)

            # Register both option legs with the shared local market-feed
            # client. It adds NIFTY 50 once and de-duplicates shared keys.
            if self._config.live_feed_enabled:
                try:
                    await self._live_feed_client.register_isolation(
                        isolation_id,
                        new_legs,
                        instrument_labels={
                            "isolated": _instrument_label(payload, "isolated"),
                            "opposite": _instrument_label(
                                payload.get("opposite_leg") or {}, "opposite"
                            ),
                        },
                    )
                    for key in new_legs.values():
                        sessions.setdefault(key, {})["live_feed_state"] = "registered"
                except Exception:
                    logger.exception("Live-feed registration failed for %s", isolation_id)

            if self._local_supertrend_service is not None:
                try:
                    raw_time = next((payload.get(k) for k in (
                        "isolation_time", "isolated_at", "isolation_timestamp",
                        "created_at", "received_at", "timestamp"
                    ) if payload.get(k)), None)
                    parsed_time = datetime.fromisoformat(
                        str(raw_time).replace("Z", "+00:00")
                    ) if raw_time else datetime.now(ZoneInfo("Asia/Kolkata"))
                    if parsed_time.tzinfo is None:
                        parsed_time = parsed_time.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
                    await self._local_supertrend_service.start_isolation(
                        isolation_id, new_legs, parsed_time
                    )
                except Exception:
                    logger.exception("Local Upstox Supertrend registration failed for %s", isolation_id)

            try:
                self._isolation_store.set_status(
                    isolation_id, "subscribing", subscribed
                )
            except Exception:
                logger.exception("set_status(subscribing) failed")

            return {
                "subscribed": subscribed,
                "rejected_legs": [],
                "web_socket_sessions": sessions,
            }

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _legs_from_payload(payload: Dict[str, Any]) -> Dict[str, str]:
        iso_key = payload.get("instrument_key") or ""
        opp_key = (payload.get("opposite_leg") or {}).get("instrument_key") or ""
        return {"isolated": iso_key, "opposite": opp_key}

    async def _teardown_isolation(self, isolation_id: str) -> None:
        legs = self._isolation_legs.pop(isolation_id, None)
        if not legs:
            return
        await self._live_feed_client.unregister_isolation(isolation_id)
        if self._local_supertrend_service is not None:
            await self._local_supertrend_service.stop_isolation(isolation_id)
        for key in legs.values():
            client = self._clients.pop(key, None)
            if client is not None:
                await client.stop()
            st_client = self._supertrend_clients.pop(key, None)
            if st_client is not None:
                await st_client.stop()

    async def stop_all(self) -> None:
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
            supertrend_clients = list(self._supertrend_clients.values())
            self._supertrend_clients.clear()
            self._isolation_legs.clear()
        await self._live_feed_client.stop()
        if self._local_supertrend_service is not None:
            await self._local_supertrend_service.stop()
        results = await asyncio.gather(
            *(c.stop() for c in clients + supertrend_clients), return_exceptions=True
        )
        for r in results:
            if isinstance(r, Exception):
                logger.warning("stop_all client error: %s", r)

    # ------------------------------------------------------------ watchdog
    async def restart_dead_clients(self) -> None:
        await self._live_feed_client.ensure_running()
        for client in list(self._clients.values()) + list(self._supertrend_clients.values()):
            state = client.status()["ws_state"]
            if client.is_running():
                continue
            if state == "failed":
                logger.warning(
                    "watchdog: %s leg=%s still failed (no retry)",
                    client.instrument_key,
                    client.leg_role,
                )
                continue
            logger.warning(
                "watchdog: restarting dead client %s leg=%s",
                client.instrument_key,
                client.leg_role,
            )
            try:
                await client.start()
            except Exception:
                logger.exception(
                    "watchdog restart failed for %s", client.instrument_key
                )