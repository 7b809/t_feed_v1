"""
Daily state for the isolation layer.

Per enabled index we keep:
  - the opening-range levels for the day (derived from the index's first candle)
  - the candidate touch events collected so far
  - the currently isolated instrument (winner) + its metadata
  - a small audit trail of selection history

State is thread-safe (RLock) and persisted to
    data/runtime/isolation/<BUCKET>.json

The <BUCKET> is either an index name (per-index scope) or the sentinel
`GLOBAL_SCOPE_KEY` (global scope). So a mid-day restart rehydrates the
winner and the candidate pool under the bucket that was active when
they were written.

A winner may be either algorithmically selected (from the candidate
pool) or manually selected (via the /isolate API or Telegram command).
Manual selections are sticky: `_reevaluate_isolation` leaves them alone
until explicitly cleared.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logger import get_logger

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RUNTIME_DIR = PROJECT_ROOT / "data" / "runtime"
ISOLATION_DIR = RUNTIME_DIR / "isolation"

# ---------------------------------------------------------------------------
# Scope sentinel
# ---------------------------------------------------------------------------
# Used as the state bucket key when `isolation_config.scope_global` is true.
# Chosen so it can never collide with a real index name, which is always
# uppercase alphanumeric (NIFTY, SENSEX, BANKNIFTY, ...).
GLOBAL_SCOPE_KEY = "__GLOBAL__"


def now_ist() -> datetime:
    return datetime.now(IST)


def today_str() -> str:
    return now_ist().strftime("%Y-%m-%d")


def format_ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class TouchEvent:
    """A single touch of an opening-range level by an option's candle."""
    instrument_key: str
    underlying: str
    strike: Optional[float]
    option_type: Optional[str]
    expiry: Optional[str]
    trading_symbol: Optional[str]

    level: str                    # r1 / r2 / r3 / s1 / s2 / s3
    level_value: float
    trigger_field: str            # "high" | "low"
    trigger_price: float
    touch_time: int               # unix seconds
    source: str                   # "live" | "backfill"
    main_index_ltp: Optional[float] = None
    distance_from_index: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instrument_key": self.instrument_key,
            "underlying": self.underlying,
            "strike": self.strike,
            "option_type": self.option_type,
            "expiry": self.expiry,
            "trading_symbol": self.trading_symbol,
            "level": self.level,
            "level_value": self.level_value,
            "trigger_field": self.trigger_field,
            "trigger_price": self.trigger_price,
            "touch_time": self.touch_time,
            "touch_time_iso": format_ist(self.touch_time),
            "source": self.source,
            "main_index_ltp": self.main_index_ltp,
            "distance_from_index": self.distance_from_index,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TouchEvent":
        return cls(
            instrument_key=data["instrument_key"],
            underlying=data.get("underlying") or "",
            strike=data.get("strike"),
            option_type=data.get("option_type"),
            expiry=data.get("expiry"),
            trading_symbol=data.get("trading_symbol"),
            level=data["level"],
            level_value=float(data["level_value"]),
            trigger_field=data.get("trigger_field") or "high",
            trigger_price=float(data.get("trigger_price") or 0.0),
            touch_time=int(data["touch_time"]),
            source=data.get("source") or "live",
            main_index_ltp=data.get("main_index_ltp"),
            distance_from_index=data.get("distance_from_index"),
        )


@dataclass
class IsolatedInstrument:
    """The day's winner for a given index (or for the global pool)."""
    instrument_key: str
    underlying: str
    strike: Optional[float]
    option_type: Optional[str]
    expiry: Optional[str]
    trading_symbol: Optional[str]
    lot_size: Optional[int] = None

    selected_level: str = ""
    level_value: float = 0.0
    trigger_field: str = ""
    trigger_price: float = 0.0
    touch_time: Optional[int] = None
    reference_average: Optional[float] = None
    selection_reason: str = ""
    selection_priority: int = 0
    selected_at: Optional[int] = None
    source_touch: Optional[Dict[str, Any]] = None

    cross_count: int = 0
    last_cross_time: Optional[int] = None
    last_cross_direction: Optional[str] = None
    suggested_order_side: Optional[str] = None

    # NEW: distinguishes a manual override (from /isolate) from an
    # algorithmic selection. Manual overrides are sticky — the selector
    # leaves them alone until explicitly cleared.
    manual_override: bool = False
    manual_actor: Optional[str] = None
    manual_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instrument_key": self.instrument_key,
            "underlying": self.underlying,
            "strike": self.strike,
            "option_type": self.option_type,
            "expiry": self.expiry,
            "trading_symbol": self.trading_symbol,
            "lot_size": self.lot_size,
            "selected_level": self.selected_level,
            "level_value": self.level_value,
            "trigger_field": self.trigger_field,
            "trigger_price": self.trigger_price,
            "touch_time": self.touch_time,
            "touch_time_iso": format_ist(self.touch_time) if self.touch_time else None,
            "reference_average": self.reference_average,
            "selection_reason": self.selection_reason,
            "selection_priority": self.selection_priority,
            "selected_at": self.selected_at,
            "selected_at_iso": format_ist(self.selected_at) if self.selected_at else None,
            "cross_count": self.cross_count,
            "last_cross_time": self.last_cross_time,
            "last_cross_direction": self.last_cross_direction,
            "suggested_order_side": self.suggested_order_side,
            "source_touch": self.source_touch,
            "manual_override": self.manual_override,
            "manual_actor": self.manual_actor,
            "manual_reason": self.manual_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IsolatedInstrument":
        return cls(
            instrument_key=data["instrument_key"],
            underlying=data.get("underlying") or "",
            strike=data.get("strike"),
            option_type=data.get("option_type"),
            expiry=data.get("expiry"),
            trading_symbol=data.get("trading_symbol"),
            lot_size=data.get("lot_size"),
            selected_level=data.get("selected_level") or "",
            level_value=float(data.get("level_value") or 0.0),
            trigger_field=data.get("trigger_field") or "",
            trigger_price=float(data.get("trigger_price") or 0.0),
            touch_time=data.get("touch_time"),
            reference_average=data.get("reference_average"),
            selection_reason=data.get("selection_reason") or "",
            selection_priority=int(data.get("selection_priority") or 0),
            selected_at=data.get("selected_at"),
            source_touch=data.get("source_touch"),
            cross_count=int(data.get("cross_count") or 0),
            last_cross_time=data.get("last_cross_time"),
            last_cross_direction=data.get("last_cross_direction"),
            suggested_order_side=data.get("suggested_order_side"),
            manual_override=bool(data.get("manual_override") or False),
            manual_actor=data.get("manual_actor"),
            manual_reason=data.get("manual_reason"),
        )


@dataclass
class IndexIsolationState:
    """Per-bucket isolation state for the day."""
    index_name: str
    opening_range: Optional[Dict[str, float]] = None
    opening_range_candle_time: Optional[int] = None
    opening_range_reference_average: Optional[float] = None

    candidates: List[TouchEvent] = field(default_factory=list)
    isolated: Optional[IsolatedInstrument] = None

    selection_history: List[Dict[str, Any]] = field(default_factory=list)
    cross_count: int = 0
    last_cross_time: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index_name": self.index_name,
            "session_date": today_str(),
            "opening_range": self.opening_range,
            "opening_range_candle_time": self.opening_range_candle_time,
            "opening_range_reference_average": self.opening_range_reference_average,
            "candidates": [c.to_dict() for c in self.candidates],
            "isolated": self.isolated.to_dict() if self.isolated else None,
            "selection_history": self.selection_history,
            "cross_count": self.cross_count,
            "last_cross_time": self.last_cross_time,
            "last_updated_iso": now_ist().isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IndexIsolationState":
        state = cls(index_name=data.get("index_name") or "")
        state.opening_range = data.get("opening_range")
        state.opening_range_candle_time = data.get("opening_range_candle_time")
        state.opening_range_reference_average = data.get(
            "opening_range_reference_average"
        )
        state.candidates = [
            TouchEvent.from_dict(c) for c in (data.get("candidates") or [])
        ]
        isolated_data = data.get("isolated")
        if isinstance(isolated_data, dict):
            state.isolated = IsolatedInstrument.from_dict(isolated_data)
        state.selection_history = list(data.get("selection_history") or [])
        state.cross_count = int(data.get("cross_count") or 0)
        state.last_cross_time = data.get("last_cross_time")
        return state


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class IsolationStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_index: Dict[str, IndexIsolationState] = {}
        self._session_date: Optional[str] = None

    def ensure_session(self) -> None:
        today = today_str()
        with self._lock:
            if self._session_date == today:
                return
            self._session_date = today
            self._by_index.clear()
            logger.info("Isolation store: new session %s", today)

    def get_state(self, index_name: str) -> IndexIsolationState:
        key = str(index_name or "").strip().upper()
        with self._lock:
            if key not in self._by_index:
                self._by_index[key] = IndexIsolationState(index_name=key)
            return self._by_index[key]

    def all_states(self) -> Dict[str, IndexIsolationState]:
        with self._lock:
            return dict(self._by_index)

    def set_opening_range(
        self,
        index_name: str,
        levels: Dict[str, float],
        candle_time: int,
        reference_average: Optional[float] = None,
    ) -> None:
        state = self.get_state(index_name)
        with self._lock:
            state.opening_range = dict(levels)
            state.opening_range_candle_time = int(candle_time)
            if reference_average is not None:
                state.opening_range_reference_average = float(reference_average)
        logger.info(
            "Isolation: opening range set | bucket=%s | candle_ts=%s",
            index_name, candle_time,
        )

    def add_candidate(self, index_name: str, touch: TouchEvent) -> None:
        state = self.get_state(index_name)
        with self._lock:
            for existing in state.candidates:
                if (
                    existing.instrument_key == touch.instrument_key
                    and existing.level == touch.level
                    and (existing.touch_time // 60) == (touch.touch_time // 60)
                ):
                    return
            state.candidates.append(touch)

    def set_isolated(
        self, index_name: str, isolated: IsolatedInstrument, reason: str
    ) -> None:
        state = self.get_state(index_name)
        with self._lock:
            previous = state.isolated
            state.isolated = isolated
            state.selection_history.append(
                {
                    "at": now_ist().isoformat(),
                    "reason": reason,
                    "instrument_key": isolated.instrument_key,
                    "level": isolated.selected_level,
                    "previous_instrument_key": previous.instrument_key if previous else None,
                    "manual_override": bool(isolated.manual_override),
                    "actor": isolated.manual_actor,
                }
            )
        logger.info(
            "Isolation: selected | bucket=%s | index=%s | key=%s | level=%s "
            "| reason=%s | manual=%s | actor=%s",
            index_name,
            isolated.underlying,
            isolated.instrument_key,
            isolated.selected_level,
            reason,
            isolated.manual_override,
            isolated.manual_actor,
        )

    def clear_isolated(self, index_name: str) -> None:
        state = self.get_state(index_name)
        with self._lock:
            state.isolated = None

    def save_all(self) -> None:
        try:
            ISOLATION_DIR.mkdir(parents=True, exist_ok=True)
            states = self.all_states()
            for name, state in states.items():
                safe = str(name).replace("/", "_").replace("\\", "_")
                path = ISOLATION_DIR / f"{safe}.json"
                with path.open("w", encoding="utf-8") as fh:
                    json.dump(state.to_dict(), fh, indent=2, default=str)
            logger.debug("Isolation store: saved %d bucket states", len(states))
        except Exception as exc:
            logger.warning("Isolation store: save failed | err=%s", exc)

    def load_all(self) -> None:
        try:
            if not ISOLATION_DIR.exists():
                return
            today = today_str()
            for path in ISOLATION_DIR.glob("*.json"):
                try:
                    with path.open("r", encoding="utf-8") as fh:
                        data = json.load(fh)
                except Exception as exc:
                    logger.warning("Isolation store: cannot read %s: %s", path, exc)
                    continue

                if str(data.get("session_date")) != today:
                    continue

                state = IndexIsolationState.from_dict(data)
                if state.index_name:
                    with self._lock:
                        self._by_index[state.index_name] = state
                    logger.info(
                        "Isolation store: rehydrated | bucket=%s | isolated=%s "
                        "| manual=%s",
                        state.index_name,
                        state.isolated.instrument_key if state.isolated else "-",
                        state.isolated.manual_override if state.isolated else False,
                    )
        except Exception as exc:
            logger.warning("Isolation store: load failed | err=%s", exc)


isolation_store = IsolationStore()