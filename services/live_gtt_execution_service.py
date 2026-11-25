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
    LIVE_GTT_LOTS_PER_ORDER, DUMMY_ORDER_STOP_LOSS_PERCENT,
    DUMMY_TARGET_UPPER_THRESHOLD, DUMMY_TARGET_LOWER_THRESHOLD,
    DUMMY_TARGET_POINTS_ABOVE_UPPER, DUMMY_TARGET_POINTS_MIDDLE, DUMMY_TARGET_POINTS_BELOW_LOWER,
    LIVE_GTT_EXECUTION_ENABLED, LIVE_GTT_KILL_SWITCH, LIVE_GTT_ISOLATION_LOOKBACK_SECONDS,
    LIVE_GTT_MAX_SIGNAL_AGE_SECONDS, LIVE_GTT_MAX_FRAME_AGE_SECONDS, LIVE_GTT_QUOTE_MAX_AGE_SECONDS,
    LIVE_GTT_EARLY_ENTRY_START, LIVE_GTT_EARLY_ENTRY_END, LIVE_GTT_EARLY_MAX_ENTRIES,
    LIVE_GTT_LATE_ENTRY_START, LIVE_GTT_LATE_MAX_ENTRIES, LIVE_GTT_LATE_ALLOW_REVERSAL,
    LIVE_GTT_MAX_NOTIONAL_PER_TRADE_INR, LIVE_GTT_MAX_RISK_PER_TRADE_INR,
    LIVE_GTT_REVERSAL_CLOSE_TIMEOUT_SECONDS,
    LIVE_GTT_MONITOR_INTERVAL_SECONDS, LIVE_GTT_STATE_COLLECTION, LIVE_GTT_STATE_DIR,
    ISOLATION_SUPERTREND_RECOVERY_ENABLED,
    ISOLATION_SUPERTREND_RECOVERY_MAX_DELAY_SECONDS,
    ISOLATION_SUPERTREND_SNAPSHOT_WAIT_SECONDS,
)
from core.logger import get_logger
from core.mongo_client import get_mongo_db
from services.gtt_execution_audit_service import GTTExecutionAuditService, build_action_prompt
from services.isolation_telegram_service import send_html, send_isolation_html, format_chart_links

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
    """Existing GTT 1 rule: choose the nearest support strictly below the percentage SL."""
    supports = [
        (name, value)
        for name in ("S1", "S2", "S3")
        if (value := _num(levels.get(name))) is not None
        and 0 < value < raw
        and value < entry
    ]
    if supports:
        name, stop = max(supports, key=lambda item: item[1])
    else:
        name, stop = "PERCENTAGE", raw
    return stop, {
        "raw_percentage_stop_loss": raw,
        "final_stop_loss": stop,
        "selected_level": name,
        "pivot_levels": levels,
        "support_adjustment_applied": name != "PERCENTAGE",
    }


def _adjust_trailing_stop(entry, raw, levels):
    """GTT 2: use the nearest support strictly below the percentage SL.

    A support above the percentage stop must not widen the permitted loss.
    If no valid support is below the percentage stop, retain the percentage SL.
    """
    supports = [
        (name, value)
        for name in ("S1", "S2", "S3")
        if (value := _num(levels.get(name))) is not None
        and 0 < value < entry
        and value < raw
    ]
    if supports:
        level_name, level_value = max(supports, key=lambda item: item[1])
        stop, selected = level_value, level_name
    else:
        level_name, level_value = None, None
        stop, selected = raw, "PERCENTAGE"
    return stop, {
        "raw_percentage_stop_loss": raw,
        "nearest_support_name": level_name,
        "nearest_support_value": level_value,
        "final_stop_loss": stop,
        "selected_level": selected,
        "pivot_levels": levels,
        "support_adjustment_applied": selected != "PERCENTAGE",
        "rule": "nearest_support_below_percentage_stop_or_percentage_fallback",
    }


def _trailing_step(entry, stop, target):
    """Fixed step from the original entry, initial SL and initial target."""
    step = (float(entry) - float(stop)) + ((float(target) - float(entry)) / 2.0)
    if not math.isfinite(step) or step <= 0:
        raise ValueError("GTT 2 trailing step must be a positive finite number")
    return step


def _trailing_stop_for_price(entry, initial_stop, step, ltp, tick):
    """Move the SL by one fixed step for every full step of favourable movement."""
    step = float(step)
    if step <= 0:
        raise ValueError("Trailing step must be positive")
    completed_steps = max(0, int(math.floor((float(ltp) - float(entry)) / step + 1e-12)))
    desired = _tick(float(initial_stop) + completed_steps * step, tick)
    return desired, completed_steps


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
    cli_meta = response.get("_algocli", {}) if isinstance(response, dict) else {}
    prompt = cli_meta.get("prompt")
    audit_contract = build_action_prompt(action, context)
    event = _AUDIT.append(
        action, isolation_id, prompt, request, response, status,
        metadata={"algocli": cli_meta} if cli_meta else {},
        prompt_origin="algocli_template" if prompt else "not_available",
        execution_stage=cli_meta.get("stage"),
        audit_contract=audit_contract,
    )
    # Detailed prompt/JSON response goes to normal chat only.
    send_private_audit(event)
    return event


def send_private_audit(event):
    def clip(s, n):
        return s if len(s) <= n else s[:n-30] + "\n...[truncated; full copy is in audit storage]"
    # Keep the combined HTML message below Telegram's message-size limit.
    prompt_text = event.get("prompt") or event.get("audit_contract") or ""
    prompt = html.escape(clip(str(prompt_text), 700))
    request = html.escape(clip(json.dumps(event.get("request", {}), ensure_ascii=False, indent=2, default=str), 600))
    response = html.escape(clip(json.dumps(event.get("response", {}), ensure_ascii=False, indent=2, default=str), 1500))
    metadata = event.get("metadata", {}).get("algocli", {})
    details = [
        f"Prompt origin: <code>{html.escape(str(event.get('prompt_origin') or 'not_available'))}</code>",
        f"Execution stage: <code>{html.escape(str(event.get('execution_stage') or 'unknown'))}</code>",
    ]
    if metadata.get("executable"):
        details.append(f"Executable: <code>{html.escape(str(metadata['executable']))}</code>")
    if metadata.get("error"):
        details.append(f"CLI error: <code>{html.escape(str(metadata['error']))}</code>")
    msg = (
        f"<b>LIVE GTT · {html.escape(str(event.get('action')))}</b>\n"
        f"Time: <code>{html.escape(str(event.get('timestamp_ist')))}</code>\n"
        f"Isolation: <code>{html.escape(str(event.get('isolation_id')))}</code>\n"
        + "\n".join(details) + "\n\n"
        f"<b>Predefined prompt / audit contract</b>\n<pre>{prompt}</pre>\n"
        f"<b>Request</b>\n<pre>{request}</pre>\n"
        f"<b>JSON response</b>\n<pre>{response}</pre>\n"
        f"<i>Deterministic Python strategy; not an LLM-generated trade decision.</i>"
    )
    try:
        send_html(msg)
    except Exception:
        logger.exception("Private GTT audit Telegram send failed")


def _group(status, plan, reason=None, ids=None, chart_context=None):
    # Only the primary lifecycle milestones are sent to the official group.
    allowed = {"GTT_ENTRY_SUBMITTED", "POSITION_CLOSED", "REVERSAL_ENTRY_SUBMITTED",
               "GTT_TARGET_EXIT_CONFIRMED", "POSITION_COMPLETED",
               "GTT_TRAIL_UPDATED", "GTT_RECONCILIATION_REQUIRED"}
    if status not in allowed:
        return
    fields = [
        "<b>LIVE GTT EXECUTION</b>",
        f"Status: <b>{html.escape(status.replace('_', ' '))}</b>",
    ]
    for key, label in (("trading_symbol", "Symbol"), ("instrument_key", "Instrument"),
                       ("signal_direction", "Signal"), ("entry_price", "Entry LTP"),
                       ("stop_loss_price", "Stop Loss"), ("target_price", "Target"),
                       ("quantity_lots", "Lots"), ("quantity_units", "Units")):
        if plan.get(key) not in (None, ""):
            fields.append(f"{label}: <code>{html.escape(str(plan[key]))}</code>")
    for leg in plan.get("gtt_legs", []):
        if not isinstance(leg, dict):
            continue
        fields.append(
            f"{html.escape(str(leg.get('name') or 'GTT'))}: "
            f"SL <code>{html.escape(str(leg.get('current_stop_loss_price') or leg.get('stop_loss_price') or '—'))}</code> · "
            f"Target <code>{html.escape(str(leg.get('target_price') or '—'))}</code> · "
            f"Lots <code>{html.escape(str(leg.get('lots') or '—'))}</code>"
        )
    if plan.get("trailing_step_points") is not None:
        fields.append(f"Trailing step: <code>{html.escape(str(plan['trailing_step_points']))} points</code>")
    if ids:
        fields.append("GTT IDs: <code>" + html.escape(", ".join(ids)) + "</code>")
    if reason:
        fields.append("Reason: " + html.escape(reason))
    fields.append("<i>GTT acceptance is not proof of exchange fill; fills are separately reconciled.</i>")
    chart_links = format_chart_links(chart_context or plan.get("chart_context") or plan)
    if chart_links:
        fields += ["", "📊 <b>CHARTS</b>", *chart_links]
    try:
        send_isolation_html("\n".join(fields))
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
    client.current_stage = "GET_POSITIONS"
    positions = client.get_positions()
    client.current_stage = "VALIDATE_POSITION"
    rows = [x for x in positions
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
    client.current_stage = "BUILD_EXECUTION_PLAN"
    received = _now()
    age = (received - started).total_seconds()
    if age > LIVE_GTT_QUOTE_MAX_AGE_SECONDS:
        raise ValueError(f"Quote request exceeded freshness limit ({age:.3f}s)")
    entry = _tick(float(quote["last_price"]), tick)
    one, fifteen = _pivot_levels(key, day)

    # GTT 1 preserves the previously deployed target and support-selection rules.
    target_points = _target_points(entry)
    target1 = _tick(entry + target_points, tick)
    raw_stop = entry * (1 - DUMMY_ORDER_STOP_LOSS_PERCENT / 100)
    stop1, stop1_meta = _adjust_stop(entry, raw_stop, one)
    stop1 = _tick(stop1, tick)

    # GTT 2 uses this contract's completed 15-minute R3 when present.
    r3 = _num(fifteen.get("R3")) if isinstance(fifteen, dict) else None
    if r3 is not None:
        target2 = _tick(r3, tick)
        if target2 <= entry:
            raise ValueError(
                f"15-minute R3 ({target2}) is not above live entry ({entry}); refusing invalid GTT 2 target"
            )
        target2_source = "15M_R3"
    else:
        # Literal configured rule: calculated target PRICE multiplied by five.
        target2 = _tick(target1 * 5, tick)
        target2_source = "CALCULATED_TARGET_PRICE_X5"

    stop2, stop2_meta = _adjust_trailing_stop(entry, raw_stop, one)
    stop2 = _tick(stop2, tick)
    trail_step = _tick(_trailing_step(entry, stop2, target2), tick)
    lots = int(LIVE_GTT_LOTS_PER_ORDER)
    if lots != 1:
        raise ValueError("LIVE_GTT_LOTS_PER_ORDER must be exactly 1 for the two-GTT strategy")
    units = lots * lot

    # Risk caps apply to the combined two-order exposure, not one GTT in isolation.
    notional = entry * units * 2
    risk = ((entry - stop1) + (entry - stop2)) * units
    if LIVE_GTT_MAX_NOTIONAL_PER_TRADE_INR <= 0 or LIVE_GTT_MAX_RISK_PER_TRADE_INR <= 0:
        raise ValueError("Positive LIVE_GTT_MAX_NOTIONAL_PER_TRADE_INR and LIVE_GTT_MAX_RISK_PER_TRADE_INR are required")
    if notional > LIVE_GTT_MAX_NOTIONAL_PER_TRADE_INR:
        raise ValueError(f"Combined two-GTT notional ₹{notional:.2f} exceeds configured cap")
    if risk > LIVE_GTT_MAX_RISK_PER_TRADE_INR:
        raise ValueError(f"Combined two-GTT initial risk ₹{risk:.2f} exceeds configured cap")

    common = {
        "instrument_key": key, "trading_symbol": leg.get("trading_symbol") or key,
        "entry_price": entry, "raw_stop_loss_price": raw_stop,
        "quantity_lots": lots, "quantity_units": units,
        "lot_size": lot, "tick_size": tick,
        "quote_fetched_at_ist": received.isoformat(), "quote_age_seconds": round(age, 3),
    }
    gtt_orders = [
        {
            **common, "name": "GTT_1_EXISTING", "strategy": "EXISTING",
            "stop_loss_price": stop1, "stop_loss_context": stop1_meta,
            "target_points": target_points, "target_price": target1,
            "trailing_enabled": False,
        },
        {
            **common, "name": "GTT_2_TRAILING", "strategy": "TRAILING",
            "stop_loss_price": stop2, "stop_loss_context": stop2_meta,
            "target_price": target2, "target_source": target2_source,
            "trailing_enabled": True, "trailing_step_points": trail_step,
            "trailing_initial_entry": entry, "trailing_initial_stop": stop2,
            "trailing_initial_target": target2,
        },
    ]
    logger.info(
        "Two-GTT plan calculated | instrument=%s entry=%.2f | GTT1 SL=%.2f target=%.2f | "
        "GTT2 SL=%.2f target=%.2f source=%s trail_step=%.2f | lots_each=%d combined_notional=%.2f combined_risk=%.2f",
        key, entry, stop1, target1, stop2, target2, target2_source, trail_step,
        lots, notional, risk,
    )
    return {
        **common, "stop_loss_price": stop1, "target_price": target1,
        "gtt_orders": gtt_orders, "trailing_step_points": trail_step,
        "gtt2_target_source": target2_source,
        "estimated_notional_inr": round(notional, 2),
        "estimated_initial_risk_inr": round(risk, 2),
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
        if entry.get("status") in {"ENTRY_INTENT_PERSISTED", "GTT_SUBMITTED", "OPEN", "REVERSAL_PENDING", "RECONCILIATION_REQUIRED"}:
            return entry
    return None


def _cancel_close(client, state, entry, isolation_id):
    # Request cancellation for the active GTT and confirm its terminal state.
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
        lot_size = int(entry.get("lot_size") or 0)
        if lot_size <= 0 or qty % lot_size:
            entry.update(
                status="RECONCILIATION_REQUIRED",
                last_error=f"Net position {qty} is not an exact multiple of known lot size {lot_size}",
            )
            _STORE.save(state)
            return False
        lots = qty // lot_size
        request = {
            "instrument_key": entry["instrument_key"],
            "lots": lots,
            "transaction_type": "SELL",
            "order_type": "MARKET",
        }
        entry.update(status="REVERSAL_PENDING", close_intent=request)
        _STORE.save(state)
        try:
            response = client.place_market_exit(entry["instrument_key"], lots)
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


def _submit(client, state, plan, signal, isolation_id, is_reversal=False, chart_context=None):
    entry_id = hashlib.sha256(f"{isolation_id}|{signal['signal_key']}".encode()).hexdigest()[:24]
    entry = {
        "entry_id": entry_id, "signal_key": signal["signal_key"],
        "signal_direction": signal["direction"], "signal_time_ist": signal["signal_time"].isoformat(),
        "trigger_signal_direction": signal.get("trigger_direction", signal["direction"]),
        "direction_source": signal.get("direction_source", "one_minute_signal"),
        "signal_source": signal.get("source", "live_supertrend"),
        "historical_recovery": signal.get("historical_recovery"),
        "instrument_key": plan["instrument_key"], "trading_symbol": plan["trading_symbol"],
        "entry_price": plan["entry_price"],
        "stop_loss_price": plan["stop_loss_price"], "target_price": plan["target_price"],
        "quantity_lots": plan["quantity_lots"], "quantity_units": plan["quantity_units"],
        "lot_size": plan["lot_size"], "gtt_legs": [],
        "combined_estimated_notional_inr": plan["estimated_notional_inr"],
        "combined_estimated_initial_risk_inr": plan["estimated_initial_risk_inr"],
        "status": "ENTRY_INTENT_PERSISTED", "created_at_ist": _now().isoformat(),
    }
    if isinstance(chart_context, dict):
        entry["chart_context"] = {
            key: chart_context.get(key)
            for key in (
                "instrument_key", "trading_symbol", "opposite_leg",
                "trigger_index", "trigger_index_canonical",
                "related_instruments", "related_instrument_keys",
            )
            if chart_context.get(key) is not None
        }
    for order in plan["gtt_orders"]:
        leg_state = {
            "name": order["name"], "strategy": order["strategy"],
            "lots": order["quantity_lots"], "quantity_units": order["quantity_units"],
            "stop_loss_price": order["stop_loss_price"], "current_stop_loss_price": order["stop_loss_price"],
            "target_price": order["target_price"], "terminal": False,
            "status": "WRITE_NOT_STARTED",
        }
        if order["trailing_enabled"]:
            leg_state.update({
                "trailing_enabled": True,
                "trailing_step_points": order["trailing_step_points"],
                "trailing_initial_entry": order["trailing_initial_entry"],
                "trailing_initial_stop": order["trailing_initial_stop"],
                "trailing_initial_target": order["trailing_initial_target"],
                "trailing_completed_steps": 0,
                "trailing_updates": [],
            })
        entry["gtt_legs"].append(leg_state)

    state.setdefault("entries", []).append(entry)
    state["entry_count"] = int(state.get("entry_count", 0)) + 1
    state.setdefault("processed_signal_keys", []).append(signal["signal_key"])
    state["processed_signal_keys"] = state["processed_signal_keys"][-500:]
    _STORE.save(state)

    # Submit each one-lot GTT independently. Persist each write intent before invoking
    # AlgoCLI so an ambiguous response can never cause an automatic duplicate.
    for order, stored in zip(plan["gtt_orders"], entry["gtt_legs"]):
        operation_id = f"{entry_id}-{order['name']}"
        request = {
            "gtt_name": order["name"],
            "instrument_key": order["instrument_key"],
            "lots": order["quantity_lots"],
            "quantity_units": order["quantity_units"],
            "transaction_type": "BUY",
            "product": "I",
            "entry_type": "IMMEDIATE_MARKET",
            "stop_loss_price": order["stop_loss_price"],
            "target_price": order["target_price"],
            "target_source": order.get("target_source", "EXISTING_PREMIUM_BAND"),
            "trailing_step_points": order.get("trailing_step_points"),
            "operation_id": operation_id,
        }
        stored.update(status="WRITE_INTENT_PERSISTED", operation_id=operation_id,
                      write_intent_at_ist=_now().isoformat())
        _STORE.save(state)
        logger.info(
            "Submitting %s | isolation=%s instrument=%s lots=%d SL=%.2f target=%.2f",
            order["name"], isolation_id, order["instrument_key"], order["quantity_lots"],
            order["stop_loss_price"], order["target_price"],
        )
        try:
            response = client.place_gtt_bracket(
                order["instrument_key"], order["quantity_lots"],
                order["stop_loss_price"], order["target_price"],
                operation_id=operation_id,
            )
            gid = _extract_id(response)
            stored.update({
                "gtt_order_id": gid,
                "submission_response": response.get("response") if isinstance(response, dict) else response,
                "status": "GTT_SUBMITTED" if gid else "RESPONSE_RECORDED_NO_GTT_ID",
                "submitted_at_ist": _now().isoformat(),
            })
            _STORE.save(state)
            _prompt_audit(
                "PLACE_GTT", isolation_id, {"plan": order, "signal": signal},
                request, response, "GTT_SUBMITTED" if gid else "RESPONSE_RECORDED_NO_GTT_ID",
            )
            if not gid:
                reason = f"{order['name']} response saved without a readable GTT ID; no retry attempted"
                stored["status"] = "RECONCILIATION_REQUIRED"
                entry.update(status="RECONCILIATION_REQUIRED", last_error=reason)
                _STORE.save(state)
                _group("GTT_RECONCILIATION_REQUIRED", entry, reason=reason, ids=[
                    x["gtt_order_id"] for x in entry["gtt_legs"] if x.get("gtt_order_id")
                ], chart_context=chart_context)
                return entry
            logger.info("%s submitted | isolation=%s gtt_id=%s", order["name"], isolation_id, gid)
        except Exception as exc:
            reason = f"{order['name']} write failed or is ambiguous: {exc}"
            stored.update(status="RECONCILIATION_REQUIRED", last_error=str(exc),
                          write_failed_at_ist=_now().isoformat())
            entry.update(status="RECONCILIATION_REQUIRED", last_error=reason)
            _STORE.save(state)
            _prompt_audit(
                "PLACE_GTT_FAILED", isolation_id, {"plan": order, "signal": signal},
                request, {"error": str(exc), "retry": False,
                          "_algocli": getattr(client, "last_execution", {})},
                "RECONCILIATION_REQUIRED",
            )
            _group("GTT_RECONCILIATION_REQUIRED", entry, reason=reason, ids=[
                x["gtt_order_id"] for x in entry["gtt_legs"] if x.get("gtt_order_id")
            ], chart_context=chart_context)
            return entry

    entry.update(status="GTT_SUBMITTED", submitted_at_ist=_now().isoformat())
    _STORE.save(state)
    _group(
        "REVERSAL_ENTRY_SUBMITTED" if is_reversal else "GTT_ENTRY_SUBMITTED",
        {**plan, "signal_direction": signal["direction"],
         "gtt_legs": entry["gtt_legs"]},
        ids=[x["gtt_order_id"] for x in entry["gtt_legs"] if x.get("gtt_order_id")],
        chart_context=chart_context,
    )
    logger.info(
        "Both independent GTTs submitted | isolation=%s entry_id=%s ids=%s",
        isolation_id, entry_id, [x.get("gtt_order_id") for x in entry["gtt_legs"]],
    )
    return entry

def _process(record, payload, received_at, role, *, historical=False, historical_evidence=None):
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
    if not historical:
        # Entry triggers must be genuinely post-isolation 1m signals. A
        # pre-isolation signal inside the legacy lookback is not an entry trigger.
        if stamp <= isolation:
            return
        signal_age = (now - stamp).total_seconds()
        frame_age = (now - received).total_seconds()
        if signal_age < -2 or signal_age > LIVE_GTT_MAX_SIGNAL_AGE_SECONDS:
            return
        if frame_age < -2 or frame_age > LIVE_GTT_MAX_FRAME_AGE_SECONDS:
            return
    else:
        # Historical signals are intentionally older than the live freshness
        # window. They are accepted only through the separate recovery worker.
        if (now - isolation).total_seconds() > ISOLATION_SUPERTREND_RECOVERY_MAX_DELAY_SECONDS:
            state = _STORE.load(iso_id)
            state.setdefault("historical_recovery", {}).update(
                execution_status="NOT_REQUIRED",
                status="expired",
                reason="Historical recovery execution window expired before dispatch",
            )
            _STORE.save(state, require_mongo=False)
            logger.info("Historical Supertrend execution skipped: isolation recovery window expired | isolation=%s", iso_id)
            return
    trigger_direction = direction
    key = _signal_key(source_key, trigger_direction, stamp)
    with _lock(iso_id):
        state = _STORE.load(iso_id)
        state.setdefault("isolation_id", iso_id)
        state.setdefault("trade_date_ist", now.date().isoformat())
        state.setdefault("entries", [])
        state.setdefault("processed_signal_keys", [])
        if key in state["processed_signal_keys"]:
            return

        # The 1m signal is only the execution trigger. Select CE/PE from the
        # latest confirmed 15m direction available at the trigger timestamp.
        # If 15m has no usable signal, use the isolation-time 1m validation;
        # if that is also unavailable, fall back to the triggering 1m signal.
        trend_context = state.get("trend_context") or {}
        trade_direction = None
        direction_source = None
        try:
            from services.supertrend_snapshot_cache import get_supertrend_snapshot
            fifteen_snapshot = get_supertrend_snapshot(
                source_key, str(record.get("trade_date_ist") or now.date().isoformat()),
                interval="15m",
            )
            latest_15m = _latest_confirmed_snapshot_direction(fifteen_snapshot, stamp)
            if latest_15m:
                trade_direction = latest_15m["direction"]
                direction_source = "live_15m_supertrend"
                trend_context["latest_15m_signal"] = latest_15m
        except Exception:
            logger.exception("Could not read 15m Supertrend direction; using stored fallback | isolation=%s", iso_id)

        if trade_direction is None and trend_context.get("initial_15m_direction") in {"BULLISH", "BEARISH"}:
            trade_direction = trend_context["initial_15m_direction"]
            direction_source = "initial_15m_supertrend"
        if trade_direction is None and trend_context.get("one_minute_direction") in {"BULLISH", "BEARISH"}:
            trade_direction = trend_context["one_minute_direction"]
            direction_source = "validated_1m_historical_fallback"
        if trade_direction is None:
            trade_direction = trigger_direction
            direction_source = "triggering_1m_signal_fallback"

        direction = trade_direction
        state["trend_context"] = trend_context
        state["trend_context"]["last_execution_decision"] = {
            "trigger_signal_direction": trigger_direction,
            "selected_trade_direction": trade_direction,
            "direction_source": direction_source,
            "trigger_signal_time_ist": stamp.isoformat(),
            "evaluated_at_ist": now.isoformat(),
        }
        try:
            from clients.algocli_client import AlgoCLIClient
            client = AlgoCLIClient()
            current = _active_entry(state)
            is_reversal = False
            if current:
                if current.get("status") in {"ENTRY_INTENT_PERSISTED", "RECONCILIATION_REQUIRED", "REVERSAL_PENDING"}:
                    # An earlier write may have reached the broker. Reconcile manually; do not
                    # submit, cancel, close, or reverse automatically from an ambiguous state.
                    if historical:
                        state.setdefault("historical_recovery", {})["execution_status"] = "RECONCILIATION_REQUIRED"
                        state["historical_recovery"]["execution_reason"] = "An existing entry is in an ambiguous state"
                        _STORE.save(state)
                    return
                if current.get("signal_direction") == direction:
                    state["processed_signal_keys"].append(key)
                    if historical:
                        state.setdefault("historical_recovery", {})["execution_status"] = "ALREADY_ACTIVE_SAME_DIRECTION"
                    _STORE.save(state)
                    return
                late = now.time().replace(tzinfo=None) >= dt_time.fromisoformat(LIVE_GTT_LATE_ENTRY_START)
                if late and not LIVE_GTT_LATE_ALLOW_REVERSAL:
                    if _cancel_close(client, state, current, iso_id):
                        state["processed_signal_keys"].append(key)
                        if historical:
                            state.setdefault("historical_recovery", {})["execution_status"] = "CLOSED_ONLY_LATE_REVERSAL_DISABLED"
                    elif historical:
                        state.setdefault("historical_recovery", {})["execution_status"] = "RECONCILIATION_REQUIRED"
                    _STORE.save(state)
                    return
                if not _cancel_close(client, state, current, iso_id):
                    if historical:
                        state.setdefault("historical_recovery", {})["execution_status"] = "RECONCILIATION_REQUIRED"
                        _STORE.save(state)
                    return
                is_reversal = True
            limit = _entry_limit(now)
            if limit <= 0 or int(state.get("entry_count", 0)) >= limit:
                state["processed_signal_keys"].append(key)
                if historical:
                    state.setdefault("historical_recovery", {})["execution_status"] = "BLOCKED_ENTRY_LIMIT"
                _STORE.save(state)
                return
            leg = _leg(record, direction)
            plan = _plan(client, leg, now.date().isoformat())
            # Never combine this strategy with an unrelated/manual position.
            client.current_stage = "CHECK_EXISTING_POSITION"
            if _position_qty(client, plan["instrument_key"]) != 0:
                raise RuntimeError("A pre-existing position exists for the target instrument")
            signal = {"direction": direction, "trigger_direction": trigger_direction,
                      "direction_source": direction_source,
                      "signal_time": stamp, "signal_key": key,
                      "received_at_ist": received.isoformat(), "source_instrument_key": source_key,
                      "source": "historical_price_recovery" if historical else "live_supertrend"}
            if historical and isinstance(historical_evidence, dict):
                signal["historical_recovery"] = historical_evidence
            submitted_entry = _submit(client, state, plan, signal, iso_id, is_reversal=is_reversal, chart_context=record)
            if historical:
                recovery = state.setdefault("historical_recovery", {})
                recovery["execution_status"] = (
                    "SUBMITTED" if submitted_entry.get("status") == "GTT_SUBMITTED"
                    else "RECONCILIATION_REQUIRED" if submitted_entry.get("status") == "RECONCILIATION_REQUIRED"
                    else "ATTEMPTED"
                )
                recovery["entry_id"] = submitted_entry.get("entry_id")
                _STORE.save(state)
        except Exception as exc:
            logger.exception("Live GTT signal rejected/failed | isolation=%s", iso_id)
            if historical:
                state.setdefault("historical_recovery", {})["execution_status"] = "BLOCKED"
                state["historical_recovery"]["execution_reason"] = str(exc)
                try:
                    _STORE.save(state, require_mongo=False)
                except Exception:
                    logger.exception("Could not persist historical recovery block state | isolation=%s", iso_id)
            client_obj = locals().get("client")
            cli_meta = getattr(client_obj, "last_execution", {}) if client_obj else {}
            stage = (
                getattr(client_obj, "current_stage", None)
                or cli_meta.get("stage")
                or "PRE_ALGOCLI"
            )
            rendered_prompt = (
                cli_meta.get("prompt")
                if cli_meta.get("stage") == stage
                else None
            )
            audit_contract = build_action_prompt("EXECUTION_BLOCKED", {
                "isolation_id": iso_id, "signal_direction": direction,
                "signal_time_ist": stamp.isoformat(), "reason": str(exc),
                "execution_stage": stage,
            })
            response = {
                "accepted": False,
                "reason": str(exc),
                "error": str(exc),
                "cli_error": (
                    cli_meta.get("error")
                    if cli_meta.get("outcome") in {"LAUNCH_FAILED", "FAILED", "AMBIGUOUS"}
                    else None
                ),
                "failed_stage": stage,
                "executable": cli_meta.get("executable"),
                "template": cli_meta.get("template"),
                "process_started": cli_meta.get("process_started", False),
            }
            event = _AUDIT.append(
                "EXECUTION_BLOCKED", iso_id, rendered_prompt,
                {
                    "instrument_key": source_key,
                    "signal_direction": direction,
                    "signal_time_ist": stamp.isoformat(),
                    "failed_stage": stage,
                    "template": cli_meta.get("template"),
                },
                response, "BLOCKED",
                metadata={"algocli": cli_meta} if cli_meta else {},
                prompt_origin="algocli_template" if rendered_prompt else "not_available",
                execution_stage=stage,
                audit_contract=audit_contract,
            )
            send_private_audit(event)




def _latest_confirmed_snapshot_direction(snapshot: dict, at_time=None) -> dict | None:
    """Return the latest confirmed buy/sell direction at or before at_time."""
    if not isinstance(snapshot, dict) or snapshot.get("status") != "loaded":
        return None
    document = snapshot.get("data")
    signals = document.get("signals") if isinstance(document, dict) else None
    if not isinstance(signals, list):
        return None
    cutoff = _dt(at_time) if at_time is not None else None
    eligible = []
    for index, item in enumerate(signals):
        if not isinstance(item, dict):
            continue
        raw_signal = str(item.get("signal") or "").strip().lower()
        if raw_signal not in {"buy", "sell"}:
            continue
        confirmed = _dt(item.get("confirmed_time"))
        if confirmed is None or (cutoff is not None and confirmed > cutoff):
            continue
        eligible.append((confirmed, index, {
            "direction": "BULLISH" if raw_signal == "buy" else "BEARISH",
            "signal": raw_signal,
            "confirmed_time_ist": confirmed.isoformat(),
            "confirmed_time_price": _num(item.get("confirmed_time_price")),
        }))
    if not eligible:
        return None
    return max(eligible, key=lambda row: (row[0], row[1]))[2]


def _process_pre_isolation_recovery(record: dict, access_token: str) -> None:
    """Build isolation-time trend context; never submit an order from history."""
    if not ISOLATION_SUPERTREND_RECOVERY_ENABLED:
        return
    if not LIVE_GTT_EXECUTION_ENABLED or LIVE_GTT_KILL_SWITCH:
        logger.info("Isolation trend context evaluated only when live GTT execution is enabled")
        return

    isolation = _dt(record.get("isolation_touch_time") or record.get("isolation_created_at"))
    source_key = str(record.get("instrument_key") or "")
    if isolation is None or not source_key:
        return
    trade_date = str(record.get("trade_date_ist") or isolation.date().isoformat())
    if trade_date != _now().date().isoformat():
        return
    iso_id = _iso_id(record)

    with _lock(iso_id):
        state = _STORE.load(iso_id)
        state.setdefault("isolation_id", iso_id)
        existing = state.get("historical_recovery") or {}
        if existing.get("execution_status") in {
            "BIAS_READY", "SUBMITTED", "BLOCKED", "NOT_REQUIRED",
            "ALREADY_ACTIVE_SAME_DIRECTION", "CLOSED_ONLY_LATE_REVERSAL_DISABLED",
            "BLOCKED_ENTRY_LIMIT", "RECONCILIATION_REQUIRED", "ATTEMPTED",
        }:
            return
        if (_now() - isolation).total_seconds() > ISOLATION_SUPERTREND_RECOVERY_MAX_DELAY_SECONDS:
            state["historical_recovery"] = {
                "status": "expired", "execution_status": "NOT_REQUIRED",
                "reason": "Isolation trend-context collection exceeded configured safety window",
                "isolation_time_ist": isolation.isoformat(),
            }
            state["trend_context"] = {
                "status": "fallback_to_first_live_1m",
                "initial_15m_direction": None,
                "one_minute_direction": None,
                "direction_source": "triggering_1m_signal_fallback",
                "isolation_time_ist": isolation.isoformat(),
            }
            _STORE.save(state)
            return
        state["historical_recovery"] = {
            "status": "evaluating", "execution_status": "EVALUATING",
            "started_at_ist": _now().isoformat(),
            "isolation_time_ist": isolation.isoformat(),
        }
        _STORE.save(state)

    one_minute_snapshot = {"status": "snapshot_unavailable"}
    fifteen_minute_snapshot = {"status": "snapshot_unavailable"}
    one_minute_evidence = {
        "status": "data_unavailable", "direction": None,
        "reason": "1m snapshot has not been loaded",
    }
    fifteen_minute_direction = None
    try:
        from core.candle_fetcher import CandleFetcher
        from services.isolation_supertrend_backfill_service import evaluate_pre_isolation_price_trend
        from services.supertrend_snapshot_cache import wait_for_supertrend_snapshot

        remaining = max(
            0.0,
            ISOLATION_SUPERTREND_RECOVERY_MAX_DELAY_SECONDS
            - (_now() - isolation).total_seconds(),
        )
        wait_each = min(float(ISOLATION_SUPERTREND_SNAPSHOT_WAIT_SECONDS), remaining)
        # Prefer 15m directional context, but always retain the existing 1m
        # historical validation as the fallback when 15m has no signal.
        fifteen_minute_snapshot = wait_for_supertrend_snapshot(
            source_key, trade_date, timeout_seconds=wait_each, interval="15m",
        )
        remaining = max(
            0.0,
            ISOLATION_SUPERTREND_RECOVERY_MAX_DELAY_SECONDS
            - (_now() - isolation).total_seconds(),
        )
        one_minute_snapshot = wait_for_supertrend_snapshot(
            source_key, trade_date,
            timeout_seconds=min(wait_each, remaining), interval="1m",
        )
        fifteen_minute_direction = _latest_confirmed_snapshot_direction(
            fifteen_minute_snapshot, isolation,
        )
        candles = CandleFetcher(access_token).fetch_intraday_candles(
            source_key, unit="minutes", interval="1",
        )
        one_minute_evidence = evaluate_pre_isolation_price_trend(
            one_minute_snapshot, isolation, candles,
        )
    except Exception as exc:
        logger.exception(
            "Isolation Supertrend trend-context collection incomplete; live 1m fallback remains available | isolation=%s",
            iso_id,
        )
        one_minute_evidence = {
            "status": "data_unavailable", "direction": None, "reason": str(exc),
            "isolation_time_ist": isolation.isoformat(),
        }
        # Still use a 15m snapshot if it was obtained before a later 1m failure.
        if fifteen_minute_direction is None:
            fifteen_minute_direction = _latest_confirmed_snapshot_direction(
                fifteen_minute_snapshot, isolation,
            )

    one_minute_direction = (
        one_minute_evidence.get("direction")
        if one_minute_evidence.get("direction") in {"BULLISH", "BEARISH"}
        else None
    )
    if fifteen_minute_direction:
        selected_direction = fifteen_minute_direction["direction"]
        direction_source = "15m_historical_supertrend"
        context_status = "15m_bias_ready"
    elif one_minute_direction:
        selected_direction = one_minute_direction
        direction_source = "validated_1m_historical_fallback"
        context_status = "1m_fallback_bias_ready"
    else:
        selected_direction = None
        direction_source = "triggering_1m_signal_fallback"
        context_status = "waiting_for_first_live_1m_direction"

    trend_context = {
        "status": context_status,
        "selected_direction": selected_direction,
        "initial_15m_direction": (
            fifteen_minute_direction.get("direction") if fifteen_minute_direction else None
        ),
        "initial_15m_signal": fifteen_minute_direction,
        "one_minute_direction": one_minute_direction,
        "one_minute_validation": one_minute_evidence,
        "direction_source": direction_source,
        "isolation_time_ist": isolation.isoformat(),
        "ready_at_ist": _now().isoformat(),
        "execution_policy": "first_post_isolation_1m_signal_triggers_entry",
    }

    with _lock(iso_id):
        state = _STORE.load(iso_id)
        current = state.get("historical_recovery") or {}
        if current.get("execution_status") == "BIAS_READY":
            return
        state["trend_context"] = trend_context
        state["historical_recovery"] = {
            **one_minute_evidence,
            "execution_status": "BIAS_READY",
            "status": context_status,
            "initial_15m_signal": fifteen_minute_direction,
            "selected_direction": selected_direction,
            "direction_source": direction_source,
            "isolation_time_ist": isolation.isoformat(),
            "evaluated_at_ist": _now().isoformat(),
            "note": "Historical evidence establishes direction only; no historical order is submitted.",
        }
        _STORE.save(state)
    logger.info(
        "Isolation trend context ready; waiting for first post-isolation 1m trigger | isolation=%s "
        "15m=%s 1m_fallback=%s selected=%s source=%s",
        iso_id,
        (fifteen_minute_direction or {}).get("direction"),
        one_minute_direction,
        selected_direction,
        direction_source,
    )

def dispatch_pre_isolation_supertrend_recovery(record: dict, access_token: str) -> bool:
    """Queue historical evaluation after isolation persistence; never block feed callbacks."""
    if not ISOLATION_SUPERTREND_RECOVERY_ENABLED:
        return False
    if not LIVE_GTT_EXECUTION_ENABLED or LIVE_GTT_KILL_SWITCH:
        return False
    if not isinstance(record, dict) or not access_token:
        return False
    try:
        start_live_gtt_execution_service()
        _WORKER.submit(_process_pre_isolation_recovery, dict(record), str(access_token))
        return True
    except Exception:
        logger.exception("Could not enqueue pre-isolation Supertrend recovery")
        return False

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



def _rule_trigger_price(detail, strategy):
    rules = detail.get("rules") or []
    matches = [r for r in rules if isinstance(r, dict) and _status(r.get("strategy")) == strategy]
    if len(matches) != 1:
        return None
    rule = matches[0]
    for key in ("trigger_price", "trigger_value", "price", "value"):
        value = _num(rule.get(key))
        if value is not None:
            return value
    return None


def _maybe_trail_gtt(client, state, entry, leg, detail, isolation_id):
    if leg.get("strategy") != "TRAILING" or not leg.get("trailing_enabled"):
        return False
    if leg.get("trail_update_pending") or leg.get("trail_reconciliation_required"):
        return False
    if _rule_state(detail, "TARGET") in {"TRIGGERED", "EXECUTED", "COMPLETE", "COMPLETED", "FILLED"}:
        return False
    if _rule_state(detail, "STOPLOSS") in {"TRIGGERED", "EXECUTED", "COMPLETE", "COMPLETED", "FILLED"}:
        return False

    started = _now()
    quote = client.get_ltp(entry["instrument_key"])
    received = _now()
    age = (received - started).total_seconds()
    if age > LIVE_GTT_QUOTE_MAX_AGE_SECONDS:
        logger.warning("Trailing quote stale; skip this monitor cycle | gtt=%s age=%.3fs",
                       leg.get("gtt_order_id"), age)
        return False
    ltp = _num(quote.get("last_price"))
    if ltp is None:
        raise ValueError("Trailing LTP response has no valid last_price")

    initial_entry = float(leg["trailing_initial_entry"])
    initial_stop = float(leg["trailing_initial_stop"])
    step = float(leg["trailing_step_points"])
    tick = float(entry.get("tick_size") or 0)
    desired_stop, completed_steps = _trailing_stop_for_price(
        initial_entry, initial_stop, step, ltp, tick
    )
    previous_steps = int(leg.get("trailing_completed_steps", 0))
    current_stop = float(leg.get("current_stop_loss_price") or leg.get("stop_loss_price"))
    if completed_steps <= previous_steps or desired_stop <= current_stop + (tick / 2):
        return False

    operation_id = f"{entry['entry_id']}-TRAIL-{completed_steps}"
    request = {
        "gtt_order_id": leg["gtt_order_id"],
        "instrument_key": entry["instrument_key"],
        "new_stop_loss_price": desired_stop,
        "previous_stop_loss_price": current_stop,
        "ltp": ltp,
        "completed_trailing_steps": completed_steps,
        "fixed_trailing_step_points": step,
        "operation_id": operation_id,
        "preserve_target_rule": True,
    }
    # Persist the write intent before changing the broker GTT. An ambiguous result
    # blocks further automated writes until a human reconciles the actual GTT.
    leg["trail_update_pending"] = {
        **request, "intent_at_ist": _now().isoformat(),
    }
    _STORE.save(state)
    logger.info(
        "GTT 2 trailing update intent | isolation=%s gtt=%s ltp=%.2f step=%.2f steps=%d old_sl=%.2f new_sl=%.2f",
        isolation_id, leg["gtt_order_id"], ltp, step, completed_steps, current_stop, desired_stop,
    )
    try:
        response = client.modify_gtt_stop_loss(
            leg["gtt_order_id"], desired_stop, operation_id=operation_id
        )
        _prompt_audit(
            "MODIFY_GTT_STOPLOSS", isolation_id, request, request, response,
            "MODIFY_RESPONSE_RECEIVED",
        )
        verified = client.get_gtt(leg["gtt_order_id"])
        actual_stop = _rule_trigger_price(verified, "STOPLOSS")
        if actual_stop is None or abs(actual_stop - desired_stop) > (tick / 2):
            raise RuntimeError(
                f"Modified GTT stop could not be verified (expected={desired_stop}, actual={actual_stop})"
            )
        leg.update(
            current_stop_loss_price=desired_stop,
            trailing_completed_steps=completed_steps,
            last_trail_update_at_ist=_now().isoformat(),
            trail_update_pending=None,
        )
        leg.setdefault("trailing_updates", []).append({
            "timestamp_ist": _now().isoformat(), "ltp": ltp,
            "previous_stop_loss_price": current_stop,
            "new_stop_loss_price": desired_stop,
            "completed_steps": completed_steps,
            "step_points": step,
            "broker_verified": True,
        })
        _STORE.save(state)
        _prompt_audit(
            "VERIFY_GTT_STOPLOSS", isolation_id, request,
            {"gtt_order_id": leg["gtt_order_id"], "expected_stop_loss": desired_stop},
            verified, "MODIFY_VERIFIED",
        )
        _group(
            "GTT_TRAIL_UPDATED", {**entry, "stop_loss_price": desired_stop,
             "target_price": leg.get("target_price"),
             "trailing_step_points": step},
            reason=f"{leg.get('name')} SL moved {current_stop:.2f} → {desired_stop:.2f} at LTP {ltp:.2f}",
            ids=[leg["gtt_order_id"]],
        )
        logger.info(
            "GTT 2 trailing stop verified | isolation=%s gtt=%s sl=%.2f steps=%d",
            isolation_id, leg["gtt_order_id"], desired_stop, completed_steps,
        )
        return True
    except Exception as exc:
        leg["trail_reconciliation_required"] = True
        leg["trail_update_error"] = str(exc)
        entry.update(status="RECONCILIATION_REQUIRED",
                     last_error=f"GTT 2 trailing modification ambiguous/unverified: {exc}")
        _STORE.save(state)
        _prompt_audit(
            "MODIFY_GTT_STOPLOSS_FAILED", isolation_id, request,
            {"error": str(exc), "retry": False,
             "_algocli": getattr(client, "last_execution", {})},
            "RECONCILIATION_REQUIRED",
        )
        _group(
            "GTT_RECONCILIATION_REQUIRED", entry,
            reason=f"GTT 2 trailing update needs manual reconciliation: {exc}",
            ids=[leg["gtt_order_id"]],
        )
        logger.exception(
            "GTT 2 trailing update requires reconciliation | isolation=%s gtt=%s",
            isolation_id, leg.get("gtt_order_id"),
        )
        return False

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
    from clients.algocli_client import AlgoCLIClient
    client = AlgoCLIClient()
    for state in docs:
        state.pop("_id", None)
        iso_id = state.get("isolation_id")
        with _lock(iso_id):
            changed = False
            for entry in state.get("entries", []):
                if entry.get("status") not in {"GTT_SUBMITTED", "OPEN"}:
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
                        if not triggered_exit and leg.get("strategy") == "TRAILING":
                            _maybe_trail_gtt(client, state, entry, leg, detail, iso_id)
                            if entry.get("status") == "RECONCILIATION_REQUIRED":
                                changed = True
                                break
                        order = _filled_order(client, detail) if triggered_exit else None
                        if order:
                            exit_kind = "TARGET" if target_state in {"TRIGGERED", "EXECUTED", "COMPLETE", "COMPLETED", "FILLED"} else "STOPLOSS"
                            leg.update(terminal=True, status=f"{exit_kind}_FILLED", filled_order=order,
                                       exit_kind=exit_kind, filled_at_ist=_now().isoformat())
                            changed = True
                            if exit_kind == "TARGET":
                                entry["target_exit_confirmed"] = True
                                _group("GTT_TARGET_EXIT_CONFIRMED", entry, reason="GTT target rule and linked exit order confirmed")
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
                    _group("POSITION_COMPLETED", entry, reason="The GTT reached a terminal state")
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
