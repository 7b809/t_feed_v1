"""Per-underlying state containers for the generic strategy engine.

The registry deliberately keeps each index's discovery and selection state
separate. Components can migrate incrementally from the legacy NIFTY cache
without creating per-index copies of strategy logic.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from core import config


@dataclass
class StrategyContext:
    underlying: str
    index_instrument_key: str
    display_name: str
    enabled: bool
    option_universe: dict[str, dict] = field(default_factory=dict)
    opening_range: dict[str, Any] = field(default_factory=dict)
    ema_state: dict[str, dict] = field(default_factory=dict)
    ema_events: list[dict] = field(default_factory=list)
    touch_state: dict[str, Any] = field(default_factory=dict)
    candidates: dict[str, dict] = field(default_factory=dict)
    selected_instrument: dict[str, Any] = field(default_factory=dict)
    alert_state: dict[str, Any] = field(default_factory=dict)

    def set_option_universe(self, contracts: list[dict]) -> None:
        """Associate each loaded contract with this context's index."""
        if not self.enabled:
            self.option_universe.clear()
            return
        self.option_universe = {}
        for raw in contracts:
            if not isinstance(raw, dict) or not raw.get("instrument_key"):
                continue
            contract = deepcopy(raw)
            contract["underlying"] = self.underlying
            contract["underlying_instrument_key"] = self.index_instrument_key
            self.option_universe[str(contract["instrument_key"])] = contract

    def snapshot(self) -> dict:
        return {
            "underlying": self.underlying,
            "index_instrument_key": self.index_instrument_key,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "option_universe": deepcopy(self.option_universe),
            "opening_range": deepcopy(self.opening_range),
            "ema_state": deepcopy(self.ema_state),
            "ema_events": deepcopy(self.ema_events),
            "touch_state": deepcopy(self.touch_state),
            "candidates": deepcopy(self.candidates),
            "selected_instrument": deepcopy(self.selected_instrument),
            "alert_state": deepcopy(self.alert_state),
        }


_lock = RLock()
_contexts: dict[str, StrategyContext] = {}


def _configured_contexts() -> dict[str, StrategyContext]:
    return {
        name: StrategyContext(
            underlying=name,
            index_instrument_key=str(settings["instrument_key"]),
            display_name=str(settings["display_name"]),
            enabled=bool(settings["enabled"]),
        )
        for name, settings in config.STRATEGY_UNDERLYINGS.items()
    }


def get_strategy_contexts(*, active_only: bool = True) -> dict[str, StrategyContext]:
    """Return registered contexts; only configured enabled indexes by default."""
    with _lock:
        if not _contexts:
            _contexts.update(_configured_contexts())
        contexts = dict(_contexts)
        return {
            key: value for key, value in contexts.items()
            if value.enabled or not active_only
        }


def get_strategy_context(underlying: str) -> StrategyContext | None:
    return get_strategy_contexts(active_only=False).get(str(underlying or "").upper())


def find_context_for_instrument(instrument_key: str) -> StrategyContext | None:
    key = str(instrument_key or "")
    for context in get_strategy_contexts().values():
        if key == context.index_instrument_key or key in context.option_universe:
            return context
    return None


def get_active_strategy_summary() -> list[dict]:
    return [
        {"underlying": context.underlying, "instrument_key": context.index_instrument_key,
         "display_name": context.display_name, "enabled": context.enabled}
        for context in get_strategy_contexts().values()
    ]


__all__ = [
    "StrategyContext", "find_context_for_instrument", "get_active_strategy_summary",
    "get_strategy_context", "get_strategy_contexts",
]
