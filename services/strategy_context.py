"""Date-scoped runtime state for each configured underlying strategy."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
from threading import RLock
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core import config

MAX_CONTEXT_EVENTS = 2000


def market_now() -> datetime:
    try:
        zone = ZoneInfo(config.MARKET_TIMEZONE)
    except (ZoneInfoNotFoundError, AttributeError):
        zone = ZoneInfo("Asia/Kolkata")
    return datetime.now(zone)


def market_date(value: Any = None) -> str:
    if isinstance(value, datetime):
        try:
            zone = ZoneInfo(config.MARKET_TIMEZONE)
        except (ZoneInfoNotFoundError, AttributeError):
            zone = ZoneInfo("Asia/Kolkata")
        if value.tzinfo is None:
            value = value.replace(tzinfo=zone)
        return value.astimezone(zone).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value:
        try:
            return date.fromisoformat(str(value)[:10]).isoformat()
        except ValueError:
            pass
    return market_now().date().isoformat()


@dataclass
class StrategyContext:
    underlying: str
    index_instrument_key: str
    display_name: str
    enabled: bool
    option_config: dict[str, Any] = field(default_factory=dict)
    trading_date: str = field(default_factory=market_date)
    option_universe: dict[str, dict] = field(default_factory=dict)
    opening_range: dict[str, Any] = field(default_factory=dict)
    ema_state: dict[str, dict] = field(default_factory=dict)
    ema_events: list[dict] = field(default_factory=list)
    touch_state: dict[str, Any] = field(default_factory=dict)
    touch_events: list[dict] = field(default_factory=list)
    candidates: dict[str, dict] = field(default_factory=dict)
    selected_instrument: dict[str, Any] = field(default_factory=dict)
    isolation_state: dict[str, Any] = field(default_factory=dict)
    alert_state: dict[str, Any] = field(default_factory=dict)
    runtime_metadata: dict[str, Any] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock, repr=False, compare=False)
    _restored_date: str | None = field(default=None, repr=False, compare=False)

    def set_option_universe(self, contracts: list[dict]) -> None:
        with self._lock:
            self.option_universe = {}
            if not self.enabled:
                return
            for raw in contracts:
                if not isinstance(raw, dict) or not raw.get("instrument_key"):
                    continue
                contract = deepcopy(raw)
                contract["underlying"] = self.underlying
                contract["underlying_instrument_key"] = self.index_instrument_key
                self.option_universe[str(contract["instrument_key"])] = contract

    def ensure_trading_date(self, value: Any = None) -> bool:
        """Reset intraday strategy state when market date changes."""
        new_date = market_date(value)
        with self._lock:
            if new_date == self.trading_date:
                return False
            self.trading_date = new_date
            self.option_universe = {}
            self.opening_range = {}
            self.ema_state = {}
            self.ema_events = []
            self.touch_state = {}
            self.touch_events = []
            self.candidates = {}
            self.selected_instrument = {}
            self.isolation_state = {}
            self.alert_state = {}
            self.runtime_metadata = {"created_at": market_now().isoformat()}
            self._restored_date = None
        return True

    def append_event(self, event: dict, *, kind: str) -> None:
        if not isinstance(event, dict):
            return
        item = deepcopy(event)
        item.setdefault("trading_date", self.trading_date)
        item.setdefault("underlying", self.underlying)
        item.setdefault("underlying_instrument_key", self.index_instrument_key)
        with self._lock:
            target = self.ema_events if kind == "ema" else self.touch_events
            target.append(item)
            del target[:-MAX_CONTEXT_EVENTS]

    def snapshot(self) -> dict:
        with self._lock:
            now = market_now().isoformat()
            self.runtime_metadata.setdefault("created_at", now)
            metadata = deepcopy(self.runtime_metadata)
            metadata["updated_at"] = now
            metadata.setdefault("version", 1)
            return {
                "trading_date": self.trading_date,
                "underlying": self.underlying,
                "underlying_instrument_key": self.index_instrument_key,
                "strategy_config": {
                    "enabled": self.enabled,
                    "display_name": self.display_name,
                    "option": deepcopy(self.option_config),
                },
                "option_universe": deepcopy(self.option_universe),
                "opening_range": deepcopy(self.opening_range),
                "ema": {"state": deepcopy(self.ema_state), "events": deepcopy(self.ema_events)},
                "touch_state": deepcopy(self.touch_state),
                "touch_events": deepcopy(self.touch_events),
                "candidates": deepcopy(self.candidates),
                "selected_instrument": deepcopy(self.selected_instrument),
                "isolation": deepcopy(self.isolation_state),
                "alerts": deepcopy(self.alert_state),
                "metadata": metadata,
            }

    def restore(self, document: dict) -> None:
        """Restore only the matching date/underlying document."""
        if not isinstance(document, dict):
            return
        if document.get("underlying") != self.underlying:
            return
        if market_date(document.get("trading_date")) != self.trading_date:
            return
        with self._lock:
            self.opening_range = deepcopy(document.get("opening_range") or {})
            ema = document.get("ema") or {}
            self.ema_state = deepcopy(ema.get("state") or {})
            self.ema_events = deepcopy(ema.get("events") or [])[-MAX_CONTEXT_EVENTS:]
            self.touch_state = deepcopy(document.get("touch_state") or {})
            self.touch_events = deepcopy(document.get("touch_events") or [])[-MAX_CONTEXT_EVENTS:]
            self.candidates = deepcopy(document.get("candidates") or {})
            self.selected_instrument = deepcopy(document.get("selected_instrument") or {})
            self.isolation_state = deepcopy(document.get("isolation") or {})
            self.alert_state = deepcopy(document.get("alerts") or {})
            self.runtime_metadata = deepcopy(document.get("metadata") or {})
            self._restored_date = self.trading_date


_lock = RLock()
_contexts: dict[str, StrategyContext] = {}


def _configured_contexts() -> dict[str, StrategyContext]:
    result = {}
    for name, settings in config.STRATEGY_UNDERLYINGS.items():
        result[name] = StrategyContext(
            underlying=name,
            index_instrument_key=str(settings["instrument_key"]),
            display_name=str(settings["display_name"]),
            enabled=bool(settings["enabled"]),
            option_config=deepcopy(settings.get("option", {})),
        )
    return result


def get_strategy_contexts(*, active_only: bool = True, restore: bool = True) -> dict[str, StrategyContext]:
    with _lock:
        if not _contexts:
            _contexts.update(_configured_contexts())
        contexts = dict(_contexts)
    if active_only:
        contexts = {key: value for key, value in contexts.items() if value.enabled}
    if restore:
        for context in contexts.values():
            context.ensure_trading_date()
            if context._restored_date != context.trading_date:
                try:
                    from services.strategy_state_persistence import strategy_state_repository
                    document = strategy_state_repository.load(context.trading_date, context.underlying)
                    context.restore(document or {})
                    context._restored_date = context.trading_date
                except Exception:
                    # Persistence is fail-open for runtime availability; service
                    # logs the failure and reports it through repository status.
                    context._restored_date = context.trading_date
    return contexts


def get_strategy_context(underlying: str, *, active_only: bool = False) -> StrategyContext | None:
    key = str(underlying or "").strip().upper()
    return get_strategy_contexts(active_only=active_only).get(key)


def find_context_for_instrument(instrument_key: str) -> StrategyContext | None:
    key = str(instrument_key or "").strip()
    for context in get_strategy_contexts().values():
        if key == context.index_instrument_key or key in context.option_universe:
            return context
    return None


def get_active_strategy_instrument_keys() -> list[str]:
    keys: list[str] = []
    for context in get_strategy_contexts().values():
        keys.append(context.index_instrument_key)
        keys.extend(context.option_universe)
    return list(dict.fromkeys(key for key in keys if key))


def persist_strategy_context(context: StrategyContext, event_type: str | None = None) -> bool:
    try:
        from services.strategy_state_persistence import strategy_state_repository
        return strategy_state_repository.save(context.snapshot(), event_type=event_type)
    except Exception:
        return False


def get_active_strategy_summary() -> list[dict]:
    return [
        {"underlying": context.underlying, "instrument_key": context.index_instrument_key,
         "display_name": context.display_name, "enabled": context.enabled,
         "trading_date": context.trading_date}
        for context in get_strategy_contexts().values()
    ]


__all__ = [
    "StrategyContext", "find_context_for_instrument", "get_active_strategy_instrument_keys",
    "get_active_strategy_summary", "get_strategy_context", "get_strategy_contexts",
    "market_date", "market_now", "persist_strategy_context",
]
