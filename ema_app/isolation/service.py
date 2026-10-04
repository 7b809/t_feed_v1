"""
Isolation orchestrator.

Responsibilities
----------------
  - Subscribe to the base engine's candle + cross listeners
  - On each finalized candle:
        * compute / cache the option's opening range from its first candle
        * detect touches against that OR
        * add touches to the scope's candidate pool
        * re-evaluate the day's winner for that scope
  - On each EMA cross:
        * if it belongs to the scope's current isolated instrument,
          build the payload, persist the alert, broadcast it, place
          orders (per config), persist each placed order, and stamp the
          delivery block
  - Persist isolation state to disk continuously

Manual overrides
----------------
External callers (HTTP router, Telegram bot) can force-select a
specific contract via `manual_select(...)`. Manual selections:

  * are sticky — `_reevaluate_isolation` leaves them alone until
    `manual_clear(...)` is called,
  * carry `manual_override=True`, `manual_actor`, `manual_reason`,
  * still fire alerts and place orders on the winner's crosses, same
    as algorithmic selections,
  * survive restart via the persisted state file.

Scope
-----
  `isolation_config.scope_global` controls how candidates are pooled:

    false -> per-index isolation
    true  -> global isolation

Manual selection respects the active scope: under per-index, the target
bucket is the instrument's own underlying; under global, the target
bucket is `GLOBAL_SCOPE_KEY`.

The service is idempotent and safe to call from the SDK thread.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from ema_app.isolation.alert_storage import isolated_alert_storage
from ema_app.isolation.config import isolation_config
from ema_app.isolation.manual_ops import (
    format_isolated_summary,
    normalize_underlying,
    resolve_from_metadata,
)
from ema_app.isolation.order_service import place_orders_for_isolated_ema_alert
from ema_app.isolation.order_storage import isolated_order_storage
from ema_app.isolation.payload import build_payload
from ema_app.isolation.selector import (
    choose_best_candidate,
    get_level_priority,
    is_better_than_current,
)
from ema_app.isolation.state import (
    GLOBAL_SCOPE_KEY,
    IsolatedInstrument,
    TouchEvent,
    format_ist,
    isolation_store,
    now_ist,
)
from ema_app.isolation.touch_detector import detect_touches
from ema_app.opening_range import compute_opening_range_levels

logger = get_logger(__name__)


def _scope_key_for(index_name: str) -> str:
    """Return the state-bucket key under the active scope."""
    if isolation_config.scope_global:
        return GLOBAL_SCOPE_KEY
    return str(index_name or "").strip().upper()


class IsolationService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._running = False
        self._base_service = None

        self._or_levels: Dict[str, Dict[str, float]] = {}
        self._or_candle_time: Dict[str, int] = {}
        self._last_close: Dict[str, float] = {}

        self._metadata: Dict[str, Dict[str, Any]] = {}
        self._index_ltp: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self, base_service) -> None:
        if not isolation_config.enabled:
            logger.info("Isolation layer disabled by config")
            return

        with self._lock:
            if self._running:
                return
            self._base_service = base_service
            self._running = True

        isolation_store.ensure_session()
        isolation_store.load_all()

        try:
            base_service.register_candle_listener(self.on_candle)
            logger.info("Isolation: candle listener registered")
        except Exception as exc:
            logger.warning("Isolation: cannot register candle listener: %s", exc)

        logger.info(
            "Isolation service started | scope=%s",
            "global" if isolation_config.scope_global else "per_index",
        )

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
        try:
            isolation_store.save_all()
        except Exception as exc:
            logger.warning("Isolation: save on stop failed: %s", exc)
        logger.info("Isolation service stopped")

    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register_instrument_metadata(self, items) -> int:
        count = 0
        with self._lock:
            for item in items:
                if not isinstance(item, dict):
                    continue
                key = item.get("instrument_key")
                if not key:
                    continue

                underlying = str(
                    item.get("underlying_symbol")
                    or item.get("underlying")
                    or item.get("index")
                    or ""
                ).upper()

                option_type = item.get("option_type") or item.get("instrument_type")
                if option_type:
                    option_type = str(option_type).upper()

                self._metadata[key] = {
                    "instrument_key": key,
                    "underlying": underlying,
                    "strike": item.get("strike_price"),
                    "option_type": option_type,
                    "expiry": item.get("expiry"),
                    "trading_symbol": item.get("trading_symbol") or item.get("short_name"),
                    "lot_size": item.get("lot_size"),
                }
                count += 1
        logger.info("Isolation: registered metadata for %d instruments", count)
        return count

    def update_index_ltp(self, index_name: str, ltp: float) -> None:
        if not index_name or ltp is None:
            return
        try:
            self._index_ltp[str(index_name).upper()] = float(ltp)
        except (TypeError, ValueError):
            pass

    # ------------------------------------------------------------------
    # MANUAL OPERATIONS (public)
    # ------------------------------------------------------------------
    def manual_lookup(
        self,
        *,
        instrument_key: Optional[str] = None,
        underlying: Optional[str] = None,
        strike: Optional[float] = None,
        option_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Resolve a target without mutating state. Used by the HTTP
        `/manual/lookup` endpoint and by the Telegram flow to validate
        user input before executing.

        Returns:
            {
              "success": bool,
              "resolved": dict | None,
              "candidates": [...],
              "error": str | None,
              "error_code": "not_found" | "ambiguous" | "invalid" | None,
            }
        """
        with self._lock:
            metadata_snapshot = dict(self._metadata)

        res = resolve_from_metadata(
            metadata_snapshot,
            instrument_key=instrument_key,
            underlying=underlying,
            strike=strike,
            option_type=option_type,
        )

        return {
            "success": res.ok,
            "resolved": res.resolved,
            "candidates": res.candidates,
            "error": res.error,
            "error_code": res.error_code,
        }

    def manual_select(
        self,
        *,
        instrument_key: Optional[str] = None,
        underlying: Optional[str] = None,
        strike: Optional[float] = None,
        option_type: Optional[str] = None,
        reason: str = "manual_override",
        actor: str = "unknown",
    ) -> Dict[str, Any]:
        """
        Force a specific contract to be the isolated instrument.

        The target bucket is derived from the active scope:
            per-index -> instrument's own underlying
            global    -> GLOBAL_SCOPE_KEY

        Any existing winner in the target bucket is snapshotted into
        `selection_history` before being replaced.

        Returns a structured result dict.
        """
        if not isolation_config.enabled:
            return {
                "success": False,
                "error": "Isolation layer is disabled (EMA_ISOLATION_ENABLED=false).",
                "bucket_key": None,
                "isolated": None,
            }

        lookup = self.manual_lookup(
            instrument_key=instrument_key,
            underlying=underlying,
            strike=strike,
            option_type=option_type,
        )
        if not lookup["success"]:
            return {
                "success": False,
                "error": lookup["error"],
                "error_code": lookup["error_code"],
                "bucket_key": None,
                "isolated": None,
            }

        meta = lookup["resolved"]
        target_underlying = normalize_underlying(meta.get("underlying"))
        if not target_underlying:
            return {
                "success": False,
                "error": "Resolved contract has no underlying index.",
                "error_code": "invalid",
                "bucket_key": None,
                "isolated": None,
            }

        bucket_key = _scope_key_for(target_underlying)

        with self._lock:
            state = isolation_store.get_state(bucket_key)
            previous_key = (
                state.isolated.instrument_key if state.isolated is not None else None
            )

            # Build the manual IsolatedInstrument from cached metadata.
            isolated = IsolatedInstrument(
                instrument_key=meta["instrument_key"],
                underlying=target_underlying,
                strike=meta.get("strike"),
                option_type=meta.get("option_type"),
                expiry=meta.get("expiry"),
                trading_symbol=meta.get("trading_symbol"),
                lot_size=meta.get("lot_size"),

                # Manual picks don't come from an OR touch; use neutral values
                selected_level="MANUAL",
                level_value=0.0,
                trigger_field="manual",
                trigger_price=0.0,
                touch_time=None,
                reference_average=self._reference_average_for(target_underlying),

                selection_reason=f"manual_override: {reason}",
                selection_priority=999,   # higher than any real level
                selected_at=int(now_ist().timestamp()),
                source_touch=None,

                manual_override=True,
                manual_actor=actor,
                manual_reason=reason,
            )

            isolation_store.set_isolated(bucket_key, isolated, reason="manual_select")

        try:
            isolation_store.save_all()
        except Exception:
            pass

        logger.info(
            "Isolation: manual select | scope=%s | bucket=%s | key=%s | actor=%s "
            "| previous=%s | reason=%s",
            "global" if isolation_config.scope_global else "per_index",
            bucket_key,
            isolated.instrument_key,
            actor,
            previous_key,
            reason,
        )

        return {
            "success": True,
            "bucket_key": bucket_key,
            "scope": "global" if isolation_config.scope_global else "per_index",
            "isolated": isolated.to_dict(),
            "previous_instrument_key": previous_key,
            "summary": format_isolated_summary(isolated),
            "error": None,
        }

    def manual_clear(self, bucket_key: str) -> Dict[str, Any]:
        """
        Clear the winner for `bucket_key`.

        Special values:
            "__GLOBAL__"  -> the global bucket (when scope=global)
            "*"           -> every bucket currently in memory
        """
        if not isolation_config.enabled:
            return {
                "success": False,
                "error": "Isolation layer is disabled.",
                "cleared": [],
            }

        key = str(bucket_key or "").strip().upper()

        cleared: List[Dict[str, Any]] = []

        with self._lock:
            if key == "*":
                target_keys = list(isolation_store.all_states().keys())
            else:
                target_keys = [key]

            for k in target_keys:
                state = isolation_store.get_state(k)
                previous = state.isolated
                state.isolated = None
                if previous is not None:
                    state.selection_history.append(
                        {
                            "at": now_ist().isoformat(),
                            "reason": "manual_clear",
                            "instrument_key": None,
                            "level": None,
                            "previous_instrument_key": previous.instrument_key,
                            "manual_override": False,
                            "actor": "unknown",
                        }
                    )
                cleared.append(
                    {
                        "bucket_key": k,
                        "previous_instrument_key": (
                            previous.instrument_key if previous else None
                        ),
                        "was_manual": bool(previous.manual_override) if previous else False,
                    }
                )

        try:
            isolation_store.save_all()
        except Exception:
            pass

        logger.info(
            "Isolation: manual clear | keys=%s | cleared=%d",
            target_keys, len(cleared),
        )

        return {
            "success": True,
            "cleared": cleared,
            "error": None,
        }

    # ------------------------------------------------------------------
    # Candle hook
    # ------------------------------------------------------------------
    def on_candle(self, candle: Dict[str, Any]) -> None:
        if not self._running:
            return

        key = candle.get("instrument_key")
        if not key:
            return

        underlying = str(candle.get("underlying") or "").upper()
        ts = int(candle.get("time") or 0)
        close = float(candle.get("close") or 0.0)

        with self._lock:
            if key not in self._or_levels:
                levels = compute_opening_range_levels(candle)
                if levels:
                    self._or_levels[key] = levels
                    self._or_candle_time[key] = ts
                    logger.debug(
                        "Isolation: OR set | key=%s | r3=%.2f s3=%.2f",
                        key, levels.get("r3", 0), levels.get("s3", 0),
                    )

            prev_close = self._last_close.get(key)
            levels = self._or_levels.get(key)
            self._last_close[key] = close

        if not levels or prev_close is None:
            return

        touches = detect_touches(candle, prev_close, levels)
        if not touches:
            return

        meta = self._metadata.get(key) or {}
        index_name = str(underlying or meta.get("underlying") or "").upper()
        if not index_name:
            return

        scope_key = _scope_key_for(index_name)
        index_ltp = self._index_ltp.get(index_name)

        for t in touches:
            strike = meta.get("strike")
            distance = None
            if index_ltp is not None and strike is not None:
                try:
                    distance = abs(float(strike) - float(index_ltp))
                except (TypeError, ValueError):
                    distance = None

            event = TouchEvent(
                instrument_key=key,
                underlying=index_name,
                strike=strike,
                option_type=meta.get("option_type"),
                expiry=meta.get("expiry"),
                trading_symbol=meta.get("trading_symbol"),
                level=t["level"],
                level_value=float(t["level_value"]),
                trigger_field=t["trigger_field"],
                trigger_price=float(t["trigger_price"]),
                touch_time=ts,
                source="live",
                main_index_ltp=index_ltp,
                distance_from_index=distance,
            )
            isolation_store.add_candidate(scope_key, event)
            logger.info(
                "Isolation: touch | scope=%s | index=%s | key=%s | level=%s | price=%.2f",
                scope_key, index_name, key, t["level"], float(t["trigger_price"]),
            )

        self._reevaluate_isolation(scope_key)

    # ------------------------------------------------------------------
    # Cross hook
    # ------------------------------------------------------------------
    def on_cross(self, cross_record: Dict[str, Any]) -> None:
        if not self._running:
            return

        key = cross_record.get("instrument_key")
        if not key:
            return

        underlying = str(
            cross_record.get("underlying")
            or (self._metadata.get(key) or {}).get("underlying")
            or ""
        ).upper()
        if not underlying:
            return

        scope_key = _scope_key_for(underlying)

        state = isolation_store.get_state(scope_key)
        isolated = state.isolated
        if isolated is None or isolated.instrument_key != key:
            return

        winner_index = str(isolated.underlying or underlying).upper()

        index_ltp = self._index_ltp.get(winner_index)
        budget = self._budget_instruments(cross_record, winner_index)
        nearest = self._nearest_instruments(cross_record, winner_index)

        payload = build_payload(
            state=state,
            isolated=isolated,
            cross_record=cross_record,
            index_ltp=index_ltp,
            budget_instruments=budget,
            nearest_instruments=nearest,
        )

        direction = (
            payload.get("duplicate_control") or {}
        ).get("direction") or "unknown"
        with self._lock:
            isolated.cross_count += 1
            isolated.last_cross_time = int(cross_record.get("cross_time") or 0)
            isolated.last_cross_direction = direction
            isolated.suggested_order_side = (
                payload.get("order_suggestion") or {}
            ).get("suggested_order_side")
            state.cross_count += 1
            state.last_cross_time = isolated.last_cross_time

        listener = getattr(self, "_broadcast_listener", None)
        if callable(listener):
            try:
                listener(payload)
            except Exception as exc:
                logger.warning("Isolation: broadcast failed: %s", exc)

        alert_key: Optional[str] = None
        try:
            alert_result = isolated_alert_storage.save_alert(
                payload=payload,
                isolated=isolated,
            )
            alert_key = alert_result.get("alert_key")
            if not alert_result.get("success"):
                logger.warning(
                    "Isolation: alert persist failed | err=%s | event_id=%s",
                    alert_result.get("error"),
                    payload.get("event_id"),
                )
        except Exception as exc:
            logger.warning("Isolation: alert persist raised: %s", exc)

        placement_result = place_orders_for_isolated_ema_alert(payload)

        delivery = payload.setdefault("delivery", {})
        delivery["orders"] = {
            "enabled": bool(isolation_config.orders_enabled),
            "attempted": placement_result.get("mode") != "skipped",
            "success": bool(placement_result.get("success")),
            "orders_placed": len(placement_result.get("orders") or []),
            "mode": placement_result.get("mode"),
            "skipped_reason": placement_result.get("skipped_reason"),
        }

        for order in placement_result.get("orders") or []:
            try:
                isolated_order_storage.save_order(
                    payload=payload,
                    placement_result=placement_result,
                    order=order,
                    alert_key=alert_key,
                )
            except Exception as exc:
                logger.warning("Isolation: order persist failed: %s", exc)

        try:
            isolation_store.save_all()
        except Exception:
            pass

        logger.info(
            "Isolation alert | scope=%s | index=%s | key=%s | dir=%s "
            "| manual=%s | mode=%s | orders=%d | alert_key=%s",
            scope_key,
            winner_index,
            key,
            direction,
            isolated.manual_override,
            placement_result.get("mode"),
            len(placement_result.get("orders") or []),
            alert_key,
        )

    def register_broadcast_listener(self, fn) -> None:
        self._broadcast_listener = fn

    # ------------------------------------------------------------------
    # Selector (unchanged algorithm; adds sticky-manual guard)
    # ------------------------------------------------------------------
    def _reevaluate_isolation(self, scope_key: str) -> None:
        """
        Re-evaluate the winner inside `scope_key`.

        Manual overrides are sticky: if the current winner has
        `manual_override=True`, we return immediately and let the
        algorithm run only after an explicit `manual_clear(...)`.
        """
        state = isolation_store.get_state(scope_key)

        # ---- Sticky manual guard -------------------------------------
        with self._lock:
            if state.isolated is not None and state.isolated.manual_override:
                return
            candidates = list(state.candidates)

        if not candidates:
            return

        best = choose_best_candidate(candidates)
        if best is None:
            return

        current_touch: Optional[TouchEvent] = None
        if state.isolated is not None and state.isolated.source_touch:
            try:
                current_touch = TouchEvent.from_dict(state.isolated.source_touch)
            except Exception:
                current_touch = None

        if not is_better_than_current(best, current_touch):
            return

        meta = self._metadata.get(best.instrument_key) or {}
        winner_index = str(best.underlying or meta.get("underlying") or "").upper()
        reference_average = self._reference_average_for(winner_index)

        isolated = IsolatedInstrument(
            instrument_key=best.instrument_key,
            underlying=best.underlying or meta.get("underlying") or "",
            strike=best.strike or meta.get("strike"),
            option_type=best.option_type or meta.get("option_type"),
            expiry=best.expiry or meta.get("expiry"),
            trading_symbol=best.trading_symbol or meta.get("trading_symbol"),
            lot_size=meta.get("lot_size"),
            selected_level=best.level,
            level_value=best.level_value,
            trigger_field=best.trigger_field,
            trigger_price=best.trigger_price,
            touch_time=best.touch_time,
            reference_average=reference_average,
            selection_reason=(
                f"level={best.level} priority={get_level_priority(best.level)} "
                f"time={format_ist(best.touch_time)}"
            ),
            selection_priority=get_level_priority(best.level),
            selected_at=int(now_ist().timestamp()),
            source_touch=best.to_dict(),
            manual_override=False,
        )

        isolation_store.set_isolated(
            scope_key,
            isolated,
            reason="better_candidate" if current_touch else "initial_selection",
        )
        isolation_store.save_all()

    # ------------------------------------------------------------------
    # Index helpers
    # ------------------------------------------------------------------
    def _reference_average_for(self, index_name: str) -> Optional[float]:
        target = str(index_name or "").strip().upper()
        if not target or target == GLOBAL_SCOPE_KEY:
            return None

        with self._lock:
            values = []
            for key, levels in self._or_levels.items():
                meta = self._metadata.get(key) or {}
                if str(meta.get("underlying") or "").upper() != target:
                    continue
                avg = levels.get("A")
                if avg is not None:
                    values.append(float(avg))
            if not values:
                return None
            return sum(values) / len(values)

    def _budget_instruments(
        self, cross_record: Dict[str, Any], underlying: str
    ) -> list:
        suggested_side = (cross_record.get("suggested_order_side") or "").upper()
        if suggested_side not in ("CE", "PE"):
            return []

        target = str(underlying or "").strip().upper()
        if not target or target == GLOBAL_SCOPE_KEY:
            return []

        pool = []
        with self._lock:
            for key, meta in self._metadata.items():
                if str(meta.get("underlying") or "").upper() != target:
                    continue
                if str(meta.get("option_type") or "").upper() != suggested_side:
                    continue
                ltp = cross_record.get(f"ltp_{key}")
                if ltp is None:
                    continue
                try:
                    ltp_f = float(ltp)
                except (TypeError, ValueError):
                    continue
                if 20.0 <= ltp_f <= 30.0:
                    pool.append({**meta, "ltp": ltp_f})

        pool.sort(key=lambda x: abs(float(x["ltp"]) - 25.0))
        return pool[:2]

    def _nearest_instruments(
        self, cross_record: Dict[str, Any], underlying: str
    ) -> list:
        return cross_record.get("nearest_instruments") or []

    # ------------------------------------------------------------------
    # Debug/inspection helpers
    # ------------------------------------------------------------------
    def metadata_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Return a shallow copy of the tracked instrument metadata."""
        with self._lock:
            return dict(self._metadata)


isolation_service = IsolationService()


__all__ = ["IsolationService", "isolation_service"]