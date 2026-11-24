"""Feature-flagged live Supertrend GTT execution on an isolated worker."""
from __future__ import annotations
import hashlib
import html
import json
import math
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, time as dt_time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from core.config import (
    DUMMY_ORDER_QUANTITY, DUMMY_ORDER_STOP_LOSS_PERCENT,
    DUMMY_TARGET_UPPER_THRESHOLD, DUMMY_TARGET_LOWER_THRESHOLD,
    DUMMY_TARGET_POINTS_ABOVE_UPPER, DUMMY_TARGET_POINTS_MIDDLE, DUMMY_TARGET_POINTS_BELOW_LOWER,
    LIVE_GTT_EXECUTION_ENABLED, LIVE_GTT_KILL_SWITCH, LIVE_GTT_ISOLATION_LOOKBACK_SECONDS,
    LIVE_GTT_MAX_SIGNAL_AGE_SECONDS, LIVE_GTT_MAX_FRAME_AGE_SECONDS, LIVE_GTT_QUOTE_MAX_AGE_SECONDS,
    LIVE_GTT_EARLY_ENTRY_START, LIVE_GTT_EARLY_ENTRY_END, LIVE_GTT_EARLY_MAX_ENTRIES,
    LIVE_GTT_LATE_ENTRY_START, LIVE_GTT_LATE_MAX_ENTRIES, LIVE_GTT_LATE_ALLOW_REVERSAL,
    LIVE_GTT_MAX_NOTIONAL_PER_TRADE_INR, LIVE_GTT_MAX_RISK_PER_TRADE_INR,
    LIVE_GTT_ORDER_PRODUCT, LIVE_GTT_REVERSAL_CLOSE_TIMEOUT_SECONDS,
    LIVE_GTT_MONITOR_INTERVAL_SECONDS, LIVE_GTT_STATE_COLLECTION, LIVE_GTT_STATE_DIR,
)
from core.logger import get_logger
from core.mongo_client import get_mongo_db
from services.gtt_execution_audit_service import GTTExecutionAuditService, build_action_prompt
from services.isolation_telegram_service import send_html, send_group_html

logger = get_logger(__file__)
IST = ZoneInfo("Asia/Kolkata")
_WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-gtt-worker")
_LOCKS: dict[str, threading.RLock] = {}
_LOCK_GUARD = threading.Lock()
_AUDIT = GTTExecutionAuditService()
_STOP = threading.Event()
_MONITOR: threading.Thread | None = None


def _lock(key):
    with _LOCK_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _now():
    return datetime.now(IST)


def _dt(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        stamp = float(value)
        if not math.isfinite(stamp):
            return None
        if abs(stamp) >= 100_000_000_000:
            stamp /= 1000.0
        try:
            return datetime.fromtimestamp(stamp, tz=IST)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except (TypeError, ValueError, OverflowError):
            try:
                stamp = float(raw)
                if abs(stamp) >= 100_000_000_000:
                    stamp /= 1000.0
                return datetime.fromtimestamp(stamp, tz=IST)
            except (TypeError, ValueError, OverflowError, OSError):
                return None
    return parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed.astimezone(IST)


def _num(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _tick(value, tick):
    if tick <= 0:
        raise ValueError("Tick size must be positive")
    n = (Decimal(str(value)) / Decimal(str(tick))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(n * Decimal(str(tick)))


def _target_points(entry):
    if entry > DUMMY_TARGET_UPPER_THRESHOLD:
        return DUMMY_TARGET_POINTS_ABOVE_UPPER
    if entry >= DUMMY_TARGET_LOWER_THRESHOLD:
        return DUMMY_TARGET_POINTS_MIDDLE
    return DUMMY_TARGET_POINTS_BELOW_LOWER


def _find_value(payload, keys):
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return value
        for value in payload.values():
            found = _find_value(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_value(value, keys)
            if found not in (None, ""):
                return found
    return None


def _direction(payload):
    value = _find_value(payload, (
        "signal", "direction", "trend", "side", "signal_type",
        "signal_direction", "supertrend_direction", "action",
    ))
    if isinstance(value, dict):
        value = value.get("direction") or value.get("trend") or value.get("type") or value.get("value")
    text = str(value or "").upper().replace("-", "_").replace(" ", "_")
    if any(x in text for x in ("BULL", "BUY", "UPTREND", "UP", "LONG")):
        return "BULLISH"
    if any(x in text for x in ("BEAR", "SELL", "DOWNTREND", "DOWN", "SHORT")):
        return "BEARISH"
    return None


def _signal_time(payload, received):
    value = _find_value(payload, (
        "signal_time", "signal_timestamp", "signal_generated_at", "generated_at",
        "candle_timestamp", "bar_time", "timestamp", "time", "created_at", "datetime", "ts",
    ))
    return _dt(value) or _dt(received)


def _pivot_levels(key, day):
    from services.option_chain_history import _find_contract_folder
    folder = _find_contract_folder(key, day)
    if folder is None:
        raise ValueError(f"Contract pivot folder not found for {key}")
    def read(name):
        path = folder / name
        if not path.is_file():
            return {}
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc.get("levels") if isinstance(doc.get("levels"), dict) else {}
    return (read("pivot_levels_1m.json") or read("pivot_levels.json"), read("pivot_levels_15m.json"))


def _adjust_stop(entry, raw, levels):
    supports = [(name, v) for name in ("S1", "S2", "S3")
                if (v := _num(levels.get(name))) is not None and 0 < v < entry]
    eligible = [(name, v) for name, v in supports if v <= raw]
    if eligible:
        name, stop = max(eligible, key=lambda x: x[1])
    else:
        s3 = _num(levels.get("S3"))
        name, stop = ("S3_FLOOR", s3) if s3 is not None and 0 < s3 < entry else ("PERCENTAGE", raw)
    return stop, {"raw_percentage_stop_loss": raw, "final_stop_loss": stop,
                  "selected_level": name, "pivot_levels": levels}


def _leg(record, direction):
    if direction == "BULLISH":
        return {k: record.get(k) for k in ("instrument_key", "trading_symbol", "lot_size", "tick_size", "option_type")}
    opposite = record.get("opposite_leg")
    if not isinstance(opposite, dict):
        raise ValueError("Opposite option contract is missing")
    return opposite


def _iso_id(record):
    return f"{record.get('trade_date_ist') or _now().date().isoformat()}__{record.get('instrument_key') or 'unknown'}"


def _signal_key(key, direction, stamp):
    return hashlib.sha256(f"{key}|{direction}|{stamp.isoformat()}".encode()).hexdigest()


class _StateStore:
    def __init__(self):
        self.root = Path(LIVE_GTT_STATE_DIR).expanduser()

    def _path(self, key):
        return self.root / (re.sub(r"[^A-Za-z0-9_.-]+", "_", key)[:160] + ".json")

    def load(self, key):
        try:
            doc = get_mongo_db()[LIVE_GTT_STATE_COLLECTION].find_one({"_id": key})
            if doc:
                doc.pop("_id", None)
                return doc
        except Exception:
            logger.exception("Mongo GTT state load failed; checking local state | isolation=%s", key)
        path = self._path(key)
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.exception("Local GTT state load failed | path=%s", path)
        return {"isolation_id": key, "status": "ACTIVE", "entry_count": 0, "entries": [], "processed_signal_keys": []}

    def save(self, state, require_mongo=True):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(state["isolation_id"])
        fd, temp = tempfile.mkstemp(prefix="gtt-state-", suffix=".tmp", dir=str(self.root))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        try:
            get_mongo_db()[LIVE_GTT_STATE_COLLECTION].replace_one(
                {"_id": state["isolation_id"]}, {"_id": state["isolation_id"], **state}, upsert=True
            )
        except Exception:
            logger.exception("Mongo GTT state write failed | isolation=%s", state.get("isolation_id"))
            if require_mongo:
                raise


_STORE = _StateStore()


def _prompt_audit(action, isolation_id, context, request, response, status):
    prompt = build_action_prompt(action, context)
    event = _AUDIT.append(action, isolation_id, prompt, request, response, status)
    # Detailed prompt/JSON response goes to normal chat only.
    send_private_audit(event)
    return event


def send_private_audit(event):
    def clip(s, n=3000):
        return s if len(s) <= n else s[:n-30] + "\n...[truncated; full copy is in audit storage]"
    prompt = html.escape(clip(event.get("prompt", "")))
    request = html.escape(clip(json.dumps(event.get("request", {}), ensure_ascii=False, indent=2, default=str), 1000))
    response = html.escape(clip(json.dumps(event.get("response", {}), ensure_ascii=False, indent=2, default=str)))
    msg = (
        f"<b>LIVE GTT · {html.escape(str(event.get('action')))}</b>\n"
        f"Time: <code>{html.escape(str(event.get('timestamp_ist')))}</code>\n"
        f"Isolation: <code>{html.escape(str(event.get('isolation_id')))}</code>\n\n"
        f"<b>Prompt / execution contract</b>\n<pre>{prompt}</pre>\n"
        f"<b>Request</b>\n<pre>{request}</pre>\n"
        f"<b>JSON response</b>\n<pre>{response}</pre>\n"
        f"<i>Deterministic Python strategy; not an LLM-generated trade decision.</i>"
    )
    try:
        send_html(msg)
    except Exception:
        logger.exception("Private GTT audit Telegram send failed")


def _group(status, plan, reason=None, ids=None):
    # Only the primary lifecycle milestones are sent to the official group.
    allowed = {"GTT_ENTRY_SUBMITTED", "POSITION_CLOSED", "REVERSAL_ENTRY_SUBMITTED",
               "TARGET1_EXIT_CONFIRMED", "RUNNER_STOP_MODIFIED", "POSITION_COMPLETED"}
    if status not in allowed:
        return
    fields = [
        "<b>LIVE GTT EXECUTION</b>",
        f"Status: <b>{html.escape(status.replace('_', ' '))}</b>",
    ]
    for key, label in (("trading_symbol", "Symbol"), ("instrument_key", "Instrument"),
                       ("signal_direction", "Signal"), ("entry_price", "Entry LTP"),
                       ("stop_loss_price", "Initial SL"), ("target1_price", "Target 1"),
                       ("runner_target_price", "Runner R3"), ("quantity_lots", "Lots"),
                       ("quantity_units", "Units")):
        if plan.get(key) not in (None, ""):
            fields.append(f"{label}: <code>{html.escape(str(plan[key]))}</code>")
    if ids:
        fields.append("GTT IDs: <code>" + html.escape(", ".join(ids)) + "</code>")
    if reason:
        fields.append("Reason: " + html.escape(reason))
    fields.append("<i>GTT acceptance is not proof of exchange fill; fills are separately reconciled.</i>")
    try:
        send_group_html("\n".join(fields))
    except Exception:
        logger.exception("GTT group Telegram send failed")


def _extract_id(value, names=("gtt_order_id", "gtt_id", "order_id", "id")):
    if isinstance(value, dict):
        for name in names:
            if value.get(name):
                return str(value[name])
        for child in value.values():
            found = _extract_id(child, names) if isinstance(child, (dict, list)) else None
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _extract_id(child, names) if isinstance(child, (dict, list)) else None
            if found:
                return found
    return None


def _position_qty(client, key):
    rows = [x for x in client.get_positions()
            if str(x.get("instrument_token") or x.get("instrument_key") or "") == key]
    if len(rows) > 1:
        raise RuntimeError("Multiple position rows for one instrument; manual reconciliation required")
    if not rows:
        return 0
    return int(rows[0].get("quantity", rows[0].get("net_quantity", 0)) or 0)


def _plan(client, leg, day):
    key = str(leg.get("instrument_key") or "")
    lot = int(leg.get("lot_size") or 0)
    tick = _num(leg.get("tick_size"))
    if not key or lot <= 0 or tick is None or tick <= 0:
        raise ValueError("Instrument key, verified lot size and tick size are mandatory")
    started = _now()
    quote = client.get_ltp(key)
    received = _now()
    age = (received - started).total_seconds()
    if age > LIVE_GTT_QUOTE_MAX_AGE_SECONDS:
        raise ValueError(f"Quote request exceeded freshness limit ({age:.3f}s)")
    entry = _tick(float(quote["last_price"]), tick)
    one, fifteen = _pivot_levels(key, day)
    r3 = _num(fifteen.get("R3"))
    if r3 is None or r3 <= entry:
        raise ValueError(f"15-minute R3 must be valid and above entry (entry={entry}, R3={r3})")
    points = _target_points(entry)
    target1 = _tick(entry + points, tick)
    raw_stop = entry * (1 - DUMMY_ORDER_STOP_LOSS_PERCENT / 100)
    stop, stop_meta = _adjust_stop(entry, raw_stop, one)
    stop = _tick(stop, tick)
    runner = _tick(r3, tick)
    lots = int(DUMMY_ORDER_QUANTITY)
    units = lots * lot
    notional = entry * units
    risk = (entry - stop) * units
    if LIVE_GTT_MAX_NOTIONAL_PER_TRADE_INR <= 0 or LIVE_GTT_MAX_RISK_PER_TRADE_INR <= 0:
        raise ValueError("Positive LIVE_GTT_MAX_NOTIONAL_PER_TRADE_INR and LIVE_GTT_MAX_RISK_PER_TRADE_INR are required")
    if notional > LIVE_GTT_MAX_NOTIONAL_PER_TRADE_INR:
        raise ValueError(f"Order notional ₹{notional:.2f} exceeds configured cap")
    if risk > LIVE_GTT_MAX_RISK_PER_TRADE_INR:
        raise ValueError(f"Initial risk ₹{risk:.2f} exceeds configured cap")
    return {
        "instrument_key": key, "trading_symbol": leg.get("trading_symbol") or key,
        "entry_price": entry, "stop_loss_price": stop, "raw_stop_loss_price": raw_stop,
        "stop_loss_context": stop_meta, "target_points": points, "target1_price": target1,
        "runner_target_price": runner, "quantity_lots": lots, "quantity_units": units,
        "lot_size": lot, "tick_size": tick, "product": LIVE_GTT_ORDER_PRODUCT,
        "estimated_notional_inr": round(notional, 2), "estimated_initial_risk_inr": round(risk, 2),
        "quote_fetched_at_ist": received.isoformat(), "quote_age_seconds": round(age, 3),
        "legs": [
            {"name": "LOT_1_FIXED_TARGET", "quantity_units": lot, "target_price": target1},
            {"name": "LOT_2_R3_RUNNER", "quantity_units": lot, "target_price": runner},
        ],
    }


def _entry_limit(now):
    start = datetime.combine(now.date(), dt_time.fromisoformat(LIVE_GTT_EARLY_ENTRY_START), tzinfo=IST)
    end = datetime.combine(now.date(), dt_time.fromisoformat(LIVE_GTT_EARLY_ENTRY_END), tzinfo=IST)
    late = datetime.combine(now.date(), dt_time.fromisoformat(LIVE_GTT_LATE_ENTRY_START), tzinfo=IST)
    if start <= now <= end:
        return LIVE_GTT_EARLY_MAX_ENTRIES
    if now >= late:
        return LIVE_GTT_LATE_MAX_ENTRIES
    return 0


def _active_entry(state):
    for entry in reversed(state.get("entries", [])):
        if entry.get("status") in {"GTT_SUBMITTED", "OPEN", "REVERSAL_PENDING", "RECONCILIATION_REQUIRED"}:
            return entry
    return None


def _cancel_close(client, state, entry, isolation_id):
    # Request cancellation for every active bracket and confirm terminal states.
    for leg in entry.get("gtt_legs", []):
        if leg.get("terminal") or not leg.get("gtt_order_id"):
            continue
        request = {"gtt_order_id": leg["gtt_order_id"]}
        try:
            response = client.cancel_gtt(leg["gtt_order_id"])
            _prompt_audit("CANCEL_GTT", isolation_id, {"reason": "OPPOSITE_SIGNAL", **request}, request, response, "CANCEL_REQUESTED")
            leg["cancel_requested"] = True
        except Exception as exc:
            entry.update(status="RECONCILIATION_REQUIRED", last_error=f"Cancellation ambiguous: {exc}")
            _STORE.save(state)
            return False
    _STORE.save(state)
    deadline = time.monotonic() + LIVE_GTT_REVERSAL_CLOSE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        pending = []
        try:
            for leg in entry.get("gtt_legs", []):
                gid = leg.get("gtt_order_id")
                if not gid:
                    continue
                detail = client.get_gtt(gid)
                status = str(detail.get("status") or detail.get("state") or "").upper()
                if status in {"CANCELLED", "CANCELED", "COMPLETE", "COMPLETED", "EXPIRED", "REJECTED"}:
                    leg["terminal"] = True
                    leg["broker_status"] = status
                else:
                    pending.append(gid)
            _STORE.save(state)
            if not pending:
                break
        except Exception:
            logger.exception("GTT cancellation reconciliation failed | isolation=%s", isolation_id)
        time.sleep(1)
    else:
        entry.update(status="RECONCILIATION_REQUIRED", last_error="GTT cancellation not confirmed")
        _STORE.save(state)
        return False

    qty = _position_qty(client, entry["instrument_key"])
    if qty < 0:
        entry.update(status="RECONCILIATION_REQUIRED", last_error=f"Unexpected short position: {qty}")
        _STORE.save(state)
        return False
    if qty > 0:
        request = {"instrument_key": entry["instrument_key"], "quantity": qty,
                   "product": entry.get("product", LIVE_GTT_ORDER_PRODUCT), "transaction_type": "SELL", "order_type": "MARKET"}
        entry.update(status="REVERSAL_PENDING", close_intent=request)
        _STORE.save(state)
        try:
            response = client.place_market_exit(entry["instrument_key"], qty, request["product"], "gtt-reversal")
            _prompt_audit("CLOSE_POSITION", isolation_id, request, request, response, "CLOSE_ORDER_SUBMITTED")
        except Exception as exc:
            entry.update(status="RECONCILIATION_REQUIRED", last_error=f"Close outcome ambiguous: {exc}")
            _STORE.save(state)
            return False
        deadline = time.monotonic() + LIVE_GTT_REVERSAL_CLOSE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if _position_qty(client, entry["instrument_key"]) == 0:
                break
            time.sleep(1)
        else:
            entry.update(status="REVERSAL_PENDING", last_error="Position did not reconcile to zero")
            _STORE.save(state)
            return False
    entry.update(status="CLOSED_BY_OPPOSITE_SIGNAL", closed_at_ist=_now().isoformat())
    _STORE.save(state)
    _group("POSITION_CLOSED", entry, "Opposite signal; broker position reconciled to zero")
    return True


def _submit(client, state, plan, signal, isolation_id):
    entry_id = hashlib.sha256(f"{isolation_id}|{signal['signal_key']}".encode()).hexdigest()[:24]
    entry = {
        "entry_id": entry_id, "signal_key": signal["signal_key"],
        "signal_direction": signal["direction"], "signal_time_ist": signal["signal_time"].isoformat(),
        "instrument_key": plan["instrument_key"], "trading_symbol": plan["trading_symbol"],
        "product": plan["product"], "entry_price": plan["entry_price"],
        "stop_loss_price": plan["stop_loss_price"], "target1_price": plan["target1_price"],
        "runner_target_price": plan["runner_target_price"], "quantity_lots": plan["quantity_lots"],
        "quantity_units": plan["quantity_units"], "lot_size": plan["lot_size"],
        "gtt_legs": [], "status": "ENTRY_INTENT_PERSISTED", "created_at_ist": _now().isoformat(),
    }
    state.setdefault("entries", []).append(entry)
    state["entry_count"] = int(state.get("entry_count", 0)) + 1
    state.setdefault("processed_signal_keys", []).append(signal["signal_key"])
    state["processed_signal_keys"] = state["processed_signal_keys"][-500:]
    _STORE.save(state)
    for leg in plan["legs"]:
        request = {
            "type": "MULTIPLE", "instrument_token": plan["instrument_key"],
            "product": plan["product"], "quantity": leg["quantity_units"],
            "transaction_type": "BUY", "leg": leg["name"],
            "rules": [
                {"strategy": "ENTRY", "trigger_type": "IMMEDIATE", "trigger_price": plan["entry_price"]},
                {"strategy": "STOPLOSS", "trigger_type": "IMMEDIATE", "trigger_price": plan["stop_loss_price"]},
                {"strategy": "TARGET", "trigger_type": "IMMEDIATE", "trigger_price": leg["target_price"]},
            ],
        }
        try:
            response = client.place_gtt_bracket(
                plan["instrument_key"], leg["quantity_units"], plan["product"],
                plan["entry_price"], plan["stop_loss_price"], leg["target_price"],
            )
            gid = _extract_id(response)
            if not gid:
                raise RuntimeError("GTT response has no readable GTT ID; reconciliation required")
            entry["gtt_legs"].append({
                "name": leg["name"], "gtt_order_id": gid,
                "quantity_units": leg["quantity_units"], "target_price": leg["target_price"],
                "terminal": False, "status": "GTT_SUBMITTED",
            })
            _STORE.save(state)
            _prompt_audit("PLACE_GTT", isolation_id, {"plan": plan, "leg": leg, "signal": signal},
                          request, response, "GTT_SUBMITTED")
        except Exception as exc:
            entry.update(status="RECONCILIATION_REQUIRED", last_error=f"GTT write failed or ambiguous: {exc}")
            _STORE.save(state)
            _prompt_audit("PLACE_GTT_FAILED", isolation_id, {"plan": plan, "leg": leg, "signal": signal},
                          request, {"error": str(exc), "retry": False}, "RECONCILIATION_REQUIRED")
            return entry
    entry.update(status="GTT_SUBMITTED", submitted_at_ist=_now().isoformat())
    _STORE.save(state)
    _group("GTT_ENTRY_SUBMITTED", plan, ids=[x["gtt_order_id"] for x in entry["gtt_legs"]])
    return entry


def _process(record, payload, received_at, role):
    if not LIVE_GTT_EXECUTION_ENABLED or LIVE_GTT_KILL_SWITCH:
        return
    if role != "isolated" or payload.get("event") != "supertrend_signal":
        return
    direction = _direction(payload)
    stamp = _signal_time(payload, received_at)
    received = _dt(received_at)
    isolation = _dt(record.get("isolation_touch_time") or record.get("isolation_created_at"))
    now = _now()
    iso_id = _iso_id(record)
    source_key = str(record.get("instrument_key") or "")
    if not direction or not stamp or not received or not isolation or not source_key:
        return
    if str(record.get("trade_date_ist") or "") != now.date().isoformat():
        return
    if stamp < isolation - timedelta(seconds=LIVE_GTT_ISOLATION_LOOKBACK_SECONDS):
        return
    signal_age = (now - stamp).total_seconds()
    frame_age = (now - received).total_seconds()
    if signal_age < -2 or signal_age > LIVE_GTT_MAX_SIGNAL_AGE_SECONDS:
        return
    if frame_age < -2 or frame_age > LIVE_GTT_MAX_FRAME_AGE_SECONDS:
        return
    key = _signal_key(source_key, direction, stamp)
    with _lock(iso_id):
        state = _STORE.load(iso_id)
        state.setdefault("isolation_id", iso_id)
        state.setdefault("trade_date_ist", now.date().isoformat())
        state.setdefault("entries", [])
        state.setdefault("processed_signal_keys", [])
        if key in state["processed_signal_keys"]:
            return
        try:
            from clients.upstox_gtt_client import UpstoxGTTClient
            client = UpstoxGTTClient()
            current = _active_entry(state)
            if current:
                if current.get("signal_direction") == direction:
                    state["processed_signal_keys"].append(key)
                    _STORE.save(state)
                    return
                late = now.time().replace(tzinfo=None) >= dt_time.fromisoformat(LIVE_GTT_LATE_ENTRY_START)
                if late and not LIVE_GTT_LATE_ALLOW_REVERSAL:
                    if _cancel_close(client, state, current, iso_id):
                        state["processed_signal_keys"].append(key)
                        _STORE.save(state)
                    return
                if not _cancel_close(client, state, current, iso_id):
                    return
            limit = _entry_limit(now)
            if limit <= 0 or int(state.get("entry_count", 0)) >= limit:
                state["processed_signal_keys"].append(key)
                _STORE.save(state)
                return
            leg = _leg(record, direction)
            plan = _plan(client, leg, now.date().isoformat())
            # Never combine this strategy with an unrelated/manual position.
            if _position_qty(client, plan["instrument_key"]) != 0:
                raise RuntimeError("A pre-existing position exists for the target instrument")
            signal = {"direction": direction, "signal_time": stamp, "signal_key": key,
                      "received_at_ist": received.isoformat(), "source_instrument_key": source_key}
            _submit(client, state, plan, signal, iso_id)
        except Exception as exc:
            logger.exception("Live GTT signal rejected/failed | isolation=%s", iso_id)
            prompt = build_action_prompt("EXECUTION_BLOCKED", {
                "isolation_id": iso_id, "signal_direction": direction,
                "signal_time_ist": stamp.isoformat(), "reason": str(exc),
            })
            event = _AUDIT.append("EXECUTION_BLOCKED", iso_id, prompt, {}, {"accepted": False, "reason": str(exc)}, "BLOCKED")
            send_private_audit(event)


def _status(value):
    return str(value or "").strip().upper().replace(" ", "_")


def _linked_ids(detail):
    result = []
    for key in ("order_ids", "order_id", "orders", "triggered_orders"):
        raw = detail.get(key)
        if isinstance(raw, str):
            result.append(raw)
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, str):
                    result.append(item)
                elif isinstance(item, dict) and (item.get("order_id") or item.get("id")):
                    result.append(str(item.get("order_id") or item.get("id")))
    return list(dict.fromkeys(result))


def _rule_state(detail, strategy):
    rules = detail.get("rules") or []
    matches = [r for r in rules if isinstance(r, dict) and _status(r.get("strategy")) == strategy]
    if len(matches) != 1:
        return ""
    return _status(matches[0].get("status") or matches[0].get("state"))


def _filled_order(client, detail):
    for order_id in _linked_ids(detail):
        try:
            order = client.get_order_details(order_id)
        except Exception:
            continue
        if _status(order.get("status") or order.get("order_status")) in {"COMPLETE", "COMPLETED", "FILLED"}:
            return order
    return None


def _monitor_once():
    if not LIVE_GTT_EXECUTION_ENABLED or LIVE_GTT_KILL_SWITCH:
        return
    try:
        docs = list(get_mongo_db()[LIVE_GTT_STATE_COLLECTION].find({"status": {"$ne": "CLOSED"}}))
    except Exception:
        logger.exception("Live GTT monitor could not read Mongo state")
        return
    if not docs:
        return
    from clients.upstox_gtt_client import UpstoxGTTClient
    client = UpstoxGTTClient()
    for state in docs:
        state.pop("_id", None)
        iso_id = state.get("isolation_id")
        with _lock(iso_id):
            changed = False
            for entry in state.get("entries", []):
                if entry.get("status") not in {"GTT_SUBMITTED", "OPEN", "RECONCILIATION_REQUIRED"}:
                    continue
                for leg in entry.get("gtt_legs", []):
                    if leg.get("terminal") or not leg.get("gtt_order_id"):
                        continue
                    try:
                        detail = client.get_gtt(leg["gtt_order_id"])
                        broker_status = _status(detail.get("status") or detail.get("state"))
                        if broker_status:
                            leg["broker_status"] = broker_status
                        target_state = _rule_state(detail, "TARGET")
                        stop_state = _rule_state(detail, "STOPLOSS")
                        triggered_exit = (
                            target_state in {"TRIGGERED", "EXECUTED", "COMPLETE", "COMPLETED", "FILLED"}
                            or stop_state in {"TRIGGERED", "EXECUTED", "COMPLETE", "COMPLETED", "FILLED"}
                        )
                        # A linked ENTRY order can be FILLED while the GTT is still
                        # protecting the position. Never interpret that as a target exit.
                        order = _filled_order(client, detail) if triggered_exit else None
                        if order:
                            exit_kind = "TARGET" if target_state in {"TRIGGERED", "EXECUTED", "COMPLETE", "COMPLETED", "FILLED"} else "STOPLOSS"
                            leg.update(terminal=True, status=f"{exit_kind}_FILLED", filled_order=order,
                                       exit_kind=exit_kind, filled_at_ist=_now().isoformat())
                            changed = True
                            if leg.get("name") == "LOT_1_FIXED_TARGET" and exit_kind == "TARGET":
                                entry["target1_exit_confirmed"] = True
                                _group("TARGET1_EXIT_CONFIRMED", entry, reason="Broker target rule and linked exit order confirmed")
                                runner = next((x for x in entry["gtt_legs"] if x.get("name") == "LOT_2_R3_RUNNER"), None)
                                if runner and not runner.get("runner_stop_adjusted"):
                                    midpoint = entry["entry_price"] + (entry["target1_price"] - entry["entry_price"]) / 2
                                    response = client.modify_gtt_stop(runner["gtt_order_id"], midpoint)
                                    request = {"gtt_order_id": runner["gtt_order_id"], "new_stop_loss": midpoint,
                                               "preserve_all_other_rules": True}
                                    _prompt_audit("MODIFY_RUNNER_STOP", iso_id,
                                                  {"entry": entry, "reason": "LOT_1_TARGET_FILLED"},
                                                  request, response, "RUNNER_STOP_MODIFIED")
                                    runner.update(runner_stop_adjusted=True, stop_loss_price=midpoint)
                                    _group("RUNNER_STOP_MODIFIED", entry, reason=f"Runner SL moved to {midpoint}")
                        elif broker_status in {"CANCELLED", "CANCELED", "EXPIRED", "REJECTED"}:
                            leg.update(terminal=True, broker_status=broker_status)
                            changed = True
                        elif broker_status in {"COMPLETE", "COMPLETED"} and not triggered_exit:
                            # GTT is terminal but the exit leg cannot be identified.
                            # Keep it visible for manual reconciliation; do not call it a fill.
                            entry.update(status="RECONCILIATION_REQUIRED",
                                         last_error="GTT completed without an identifiable target/stop exit state")
                            changed = True
                    except Exception:
                        logger.exception("GTT reconciliation failed | isolation=%s gtt=%s", iso_id, leg.get("gtt_order_id"))
                        entry.update(status="RECONCILIATION_REQUIRED", last_error="Broker GTT state is unreadable")
                        changed = True
                if entry.get("gtt_legs") and all(x.get("terminal") for x in entry["gtt_legs"]):
                    entry.update(status="CLOSED", closed_at_ist=_now().isoformat())
                    changed = True
                    _group("POSITION_COMPLETED", entry, reason="All GTT legs reached terminal states")
                elif entry.get("status") == "GTT_SUBMITTED":
                    entry["status"] = "OPEN"
                    changed = True
            if changed:
                try:
                    _STORE.save(state, require_mongo=False)
                except Exception:
                    logger.exception("Could not persist monitor update | isolation=%s", iso_id)


def _monitor_loop():
    while not _STOP.wait(max(1, LIVE_GTT_MONITOR_INTERVAL_SECONDS)):
        try:
            _monitor_once()
        except Exception:
            logger.exception("Unexpected live GTT monitor error")


def start_live_gtt_execution_service():
    global _MONITOR
    if not LIVE_GTT_EXECUTION_ENABLED:
        logger.info("Live GTT execution is disabled; monitor remains off")
        return {"enabled": False, "state": "disabled"}
    if LIVE_GTT_KILL_SWITCH:
        logger.warning("Live GTT kill switch is active")
        return {"enabled": True, "state": "kill_switch"}
    if _MONITOR and _MONITOR.is_alive():
        return {"enabled": True, "state": "running"}
    _STOP.clear()
    _MONITOR = threading.Thread(target=_monitor_loop, name="live-gtt-monitor", daemon=True)
    _MONITOR.start()
    return {"enabled": True, "state": "running"}


def stop_live_gtt_execution_service():
    _STOP.set()
    if _MONITOR and _MONITOR.is_alive():
        _MONITOR.join(timeout=5)
    logger.info("Live GTT monitor stop requested")


def dispatch_live_supertrend_signal(record, payload, received_at=None, role="isolated"):
    """Feed-safe non-blocking dispatcher; no broker call runs on the socket loop."""
    if not LIVE_GTT_EXECUTION_ENABLED or LIVE_GTT_KILL_SWITCH:
        return False
    if role != "isolated" or not isinstance(record, dict) or not isinstance(payload, dict):
        return False
    if payload.get("event") != "supertrend_signal":
        return False
    try:
        start_live_gtt_execution_service()
        _WORKER.submit(_process, dict(record), dict(payload), received_at or _now().isoformat(), role)
        return True
    except Exception:
        logger.exception("Could not enqueue live GTT signal")
        return False
