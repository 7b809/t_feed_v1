"""Thread-safe, instrument-keyed cache of today's live Supertrend WebSocket snapshots."""
from __future__ import annotations
import copy
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
_CONDITION = threading.Condition(threading.RLock())
_CACHE: dict[tuple[str, str], dict] = {}

def _now():
    return datetime.now(IST)

def _body(payload):
    nested = payload.get("data")
    return nested if isinstance(nested, dict) else payload

def _signal_key(item):
    return (str(item.get("signal") or "").strip().lower(),
            str(item.get("confirmed_time") or ""),
            str(item.get("confirmed_time_price") or ""))

def _normalise_signal(item):
    if not isinstance(item, dict):
        return None
    if str(item.get("signal") or "").strip().lower() not in {"buy", "sell"} or not item.get("confirmed_time"):
        return None
    return copy.deepcopy(item)

def update_supertrend_snapshot_cache(instrument_key, payload, received_at_ist=None):
    """Merge a snapshot, confirmed signal, or heartbeat into today's cache."""
    key = str(instrument_key or "").strip()
    if not key or not isinstance(payload, dict):
        return False
    body = _body(payload)
    event = str(payload.get("event") or payload.get("type") or body.get("event") or "").lower()
    if event not in {"snapshot", "supertrend_signal", "signal", "heartbeat"}:
        return False
    if (body.get("data_scope") or payload.get("data_scope")) != "today_only":
        return False
    if str(body.get("instrument_key") or payload.get("instrument_key") or key) != key:
        return False
    today = str(body.get("today_ist_date") or payload.get("today_ist_date") or _now().date().isoformat())
    if today != _now().date().isoformat():
        return False
    now_text = received_at_ist or _now().isoformat()
    with _CONDITION:
        state = _CACHE.setdefault((today, key), {
            "instrument_key": key, "today_ist_date": today, "snapshot": None,
            "latest": None, "last_heartbeat": None, "signals": [], "updated_at_ist": now_text,
        })
        if event == "snapshot":
            state["snapshot"] = copy.deepcopy(payload)
            signals = _body(payload).get("signals")
            if isinstance(signals, list):
                merged = {_signal_key(item): item for item in state["signals"] if isinstance(item, dict)}
                for item in signals:
                    normalized = _normalise_signal(item)
                    if normalized is not None:
                        merged[_signal_key(normalized)] = normalized
                state["signals"] = sorted(merged.values(), key=lambda item: str(item.get("confirmed_time") or ""))
            latest = _body(payload).get("latest") or _body(payload).get("state")
            if latest is not None:
                state["latest"] = copy.deepcopy(latest)
        elif event in {"supertrend_signal", "signal"}:
            data = copy.deepcopy(body)
            normalized = _normalise_signal(data)
            if normalized is not None:
                existing = {_signal_key(item) for item in state["signals"] if isinstance(item, dict)}
                if _signal_key(normalized) not in existing:
                    state["signals"].append(normalized)
                    state["signals"].sort(key=lambda item: str(item.get("confirmed_time") or ""))
            state["latest"] = data
        else:
            state["last_heartbeat"] = copy.deepcopy(payload)
        state["updated_at_ist"] = now_text
        _CONDITION.notify_all()
    return True

def get_supertrend_snapshot(instrument_key, trade_date_ist=None):
    """Return an evaluator-compatible backfill object for this exact instrument."""
    key = str(instrument_key or "").strip()
    day = str(trade_date_ist or _now().date().isoformat())
    with _CONDITION:
        state = _CACHE.get((day, key))
        if not state or not isinstance(state.get("snapshot"), dict):
            return {"status": "snapshot_unavailable", "is_backfill": True,
                    "source": "supertrend_websocket_snapshot", "instrument_key": key,
                    "trade_date_ist": day, "signal_count": 0, "data": None}
        snapshot = copy.deepcopy(state["snapshot"])
        body = copy.deepcopy(_body(snapshot))
        document = {
            **body, "indicator": str(body.get("indicator") or snapshot.get("indicator") or "supertrend"),
            "instrument_key": key, "today_ist_date": day, "data_scope": "today_only",
            "signals": copy.deepcopy(state["signals"]), "signal_count": len(state["signals"]),
            "snapshot": snapshot, "latest": copy.deepcopy(state.get("latest")),
            "last_heartbeat": copy.deepcopy(state.get("last_heartbeat")),
            "snapshot_updated_at_ist": state.get("updated_at_ist"),
        }
        if str(document.get("indicator") or "").lower() != "supertrend":
            return {"status": "invalid_snapshot", "is_backfill": True,
                    "source": "supertrend_websocket_snapshot", "instrument_key": key,
                    "trade_date_ist": day, "signal_count": 0, "data": None}
        return {"status": "loaded", "is_backfill": True,
                "source": "supertrend_websocket_snapshot", "instrument_key": key,
                "trade_date_ist": day, "loaded_at_ist": _now().isoformat(),
                "signal_count": len(state["signals"]), "data": document}

def wait_for_supertrend_snapshot(instrument_key, trade_date_ist=None, timeout_seconds=15.0):
    """Wait for a snapshot from this instrument only; never substitute another key."""
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    result = get_supertrend_snapshot(instrument_key, trade_date_ist)
    while result.get("status") != "loaded":
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return result
        with _CONDITION:
            _CONDITION.wait(timeout=remaining)
        result = get_supertrend_snapshot(instrument_key, trade_date_ist)
    return result

def clear_supertrend_snapshot_cache():
    """Clear process-local cache; intended for deterministic tests."""
    with _CONDITION:
        _CACHE.clear()
        _CONDITION.notify_all()
