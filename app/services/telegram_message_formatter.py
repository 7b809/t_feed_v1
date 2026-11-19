"""Compact, mobile-readable Telegram notifications for OrderFlow events."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _first_text(source: Any, *keys: str) -> str | None:
    if not isinstance(source, dict):
        return None
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            text = str(value).strip()
            if text:
                return text
    return None


def _instrument_name(source: Any, fallback_key: str | None = None) -> str:
    if not isinstance(source, dict):
        return str(fallback_key or "Unknown instrument")
    meta = source.get("meta") if isinstance(source.get("meta"), dict) else {}
    name = _first_text(
        source, "trading_symbol", "tradingsymbol", "symbol", "instrument_name",
    ) or _first_text(
        meta, "trading_symbol", "tradingsymbol", "symbol", "instrument_name",
    )
    if name:
        return name
    strike = _first_text(source, "strike", "strike_price") or _first_text(
        meta, "strike_price", "strike",
    )
    option_type = _first_text(
        source, "option_type", "optionType", "strike_type",
    ) or _first_text(meta, "strike_type", "option_type")
    if strike and option_type:
        return f"{strike} {option_type.upper()}"
    return str(fallback_key or "Unknown instrument")


def _money(value: Any) -> str:
    try:
        return f"₹{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _time(value: Any) -> str | None:
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).strip().replace("Z", "+00:00")
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed.astimezone(IST).strftime("%I:%M %p").lstrip("0") + " IST"
    except (TypeError, ValueError):
        return str(value)


def _duration(value: Any) -> str | None:
    try:
        seconds = max(0, int(float(value)))
    except (TypeError, ValueError):
        return None
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_isolation_notification(
    isolation_id: str,
    payload: dict[str, Any],
    overwrite: bool = False,
) -> str:
    """Show human symbols first; keep instrument keys as compact identifiers."""
    isolated_key = _first_text(payload, "instrument_key") or "—"
    isolated_name = _instrument_name(payload, isolated_key)
    opposite = payload.get("opposite_leg") or {}
    opposite_key = _first_text(opposite, "instrument_key")
    opposite_name = _instrument_name(opposite, opposite_key) if opposite_key else None

    lines = [
        "🔄 ISOLATION UPDATED" if overwrite else "📥 INSTRUMENT ISOLATED",
        f"📌 {isolated_name}",
        f"Key: {isolated_key}",
    ]
    if opposite_key:
        lines.extend([
            "",
            f"↔️ Opposite: {opposite_name}",
            f"Key: {opposite_key}",
        ])
    trade_date = _first_text(payload, "trade_date_ist")
    isolated_at = _time(
        _first_text(payload, "isolation_time", "isolated_at", "created_at", "received_at")
    )
    if trade_date or isolated_at:
        lines.append("")
        if trade_date:
            lines.append(f"Trade date: {trade_date} (IST)")
        if isolated_at:
            lines.append(f"Received: {isolated_at}")
    lines.append(f"ID: {isolation_id}")
    return "\n".join(lines)


def format_order_entry_notification(
    state: dict[str, Any],
    order: dict[str, Any],
) -> str:
    """Describe only the entry event that just occurred."""
    strategy = str(order.get("indicator") or order.get("strategy") or "Order").upper()
    symbol = _instrument_name(order, order.get("target_instrument_key") or order.get("instrument_key"))
    instrument_key = _first_text(order, "target_instrument_key", "instrument_key") or "—"
    status = str(order.get("status") or "").upper()
    trigger = order.get("trigger") if isinstance(order.get("trigger"), dict) else {}
    lines: list[str]

    if status == "DUMMY_PLACED":
        lines = ["🟢 SIMULATED ENTRY", f"📌 {symbol}", f"{strategy} • BUY"]
    else:
        lines = ["🟡 SIGNAL RECORDED • ENTRY WAITING", f"📌 {symbol}", strategy]

    signal = _first_text(order, "signal_direction")
    if not signal:
        signal = _first_text(trigger, "level_type")
    if signal:
        lines.append(f"Signal: {signal.replace('_', ' ').title()}")

    if status == "DUMMY_PLACED":
        entry_time = _time(order.get("started_at_ist") or order.get("entry_time_ist"))
        lines.append(f"Entry: {_money(order.get('entry_price'))}")
        quantity = order.get("quantity")
        lot_size = order.get("lot_size")
        units = order.get("quantity_units")
        if quantity is not None and lot_size not in (None, "", 0, "0"):
            quantity_text = f"Qty: {quantity} lot × {lot_size}"
            if units not in (None, ""):
                quantity_text += f" ({units} units)"
            lines.append(quantity_text)
        elif quantity is not None:
            lines.append(f"Quantity: {quantity}")
        target = order.get("target_price")
        stop = order.get("stop_loss_price")
        if target is not None or stop is not None:
            lines.append(
                f"Target: {_money(target)}  |  Stop: {_money(stop)}"
            )
        if entry_time:
            lines.append(f"Time: {entry_time}")
    else:
        lines.append("No same-session quote was available; no simulated fill was recorded.")

    level_price = trigger.get("level_price")
    if level_price is not None:
        level_detail = f"SR level: {_money(level_price)}"
        config = _first_text(trigger, "config")
        if config:
            level_detail += f" • Config {config}"
        lines.append(level_detail)

    confirmed = _time(trigger.get("confirmed_time"))
    if confirmed:
        lines.append(f"Confirmed: {confirmed}")
    lines.append(f"Key: {instrument_key}")

    source_symbol = _first_text(order, "source_trading_symbol")
    source_key = _first_text(order, "source_instrument_key")
    same_source = bool(
        (source_key and source_key == instrument_key)
        or (source_symbol and source_symbol == symbol)
    )
    if (source_symbol or source_key) and not same_source:
        source_label = source_symbol or source_key
        lines.append(f"Signal source: {source_label}")
    gtt = order.get("gtt_submission") if isinstance(order.get("gtt_submission"), dict) else {}
    if gtt.get("status") == "SUBMITTED":
        lines.append("🟢 LIVE GTT accepted by execution API")
        ids = gtt.get("gtt_order_ids") or []
        if ids:
            lines.append("GTT ID: " + ", ".join(str(item) for item in ids))
    elif gtt.get("status") in {"FAILED", "UNKNOWN"}:
        lines.append("⚠️ LIVE GTT " + str(gtt.get("status")) + ": inspect order record before retry")
    else:
        lines.append("SIMULATION ONLY • No live order sent")
    return "\n".join(lines)


def format_order_completion_notification(
    state: dict[str, Any],
    indicator: str,
    order: dict[str, Any],
) -> str:
    """Show the outcome, prices and P&L without diagnostic tick history."""
    reason = str(order.get("exit_reason") or "COMPLETED").upper()
    reason_label = {
        "TARGET": "Target reached",
        "STOP_LOSS": "Stop-loss hit",
        "SIGNAL_REVERSAL": "Signal reversed",
    }.get(reason, reason.replace("_", " ").title())
    symbol = _instrument_name(order, order.get("target_instrument_key") or order.get("instrument_key"))
    strategy = str(order.get("indicator") or indicator or "ORDER").upper()
    icon = "✅" if reason == "TARGET" else "⚠️" if reason == "STOP_LOSS" else "🔁"
    lines = [
        f"{icon} SIMULATED ORDER CLOSED",
        f"📌 {symbol}",
        f"{strategy} • {reason_label}",
        f"Entry: {_money(order.get('entry_price'))} → Exit: {_money(order.get('exit_price'))}",
    ]
    pnl = order.get("pnl_net")
    if pnl is not None:
        try:
            pnl_value = float(pnl)
            lines.append(f"P&L: {'+' if pnl_value > 0 else ''}{_money(pnl_value)}")
        except (TypeError, ValueError):
            pass
    duration = _duration(order.get("duration_seconds"))
    if duration:
        lines.append(f"Duration: {duration}")
    completed = _time(order.get("completed_at_ist"))
    if completed:
        lines.append(f"Closed: {completed}")
    instrument_key = _first_text(order, "target_instrument_key", "instrument_key")
    if instrument_key:
        lines.append(f"Key: {instrument_key}")
    gtt = order.get("gtt_submission") if isinstance(order.get("gtt_submission"), dict) else {}
    if gtt.get("status") == "SUBMITTED":
        lines.append("Linked live GTT was submitted separately; verify its broker lifecycle.")
    else:
        lines.append("SIMULATION ONLY • No live order sent")
    return "\n".join(lines)


def format_recovery_notification(state: dict[str, Any]) -> str:
    """One compact summary after startup candle replay."""
    recovery = state.get("supertrend_startup_recovery") or {}
    symbol = _instrument_name(
        state.get("isolated_instrument") or {},
        (state.get("legs") or {}).get("isolated"),
    )
    lines = [
        "♻️ STARTUP RECOVERY COMPLETE",
        f"📌 {symbol}",
        "Supertrend history reconciled",
        f"Signals found: {recovery.get('signals_found', 0)}"
        f"  •  Entries replayed: {recovery.get('orders_replayed', 0)}"
        f"  •  Closed: {recovery.get('orders_completed', 0)}",
    ]
    current = (state.get("orders") or {}).get("supertrend")
    if isinstance(current, dict) and current.get("status") == "DUMMY_PLACED":
        current_symbol = _instrument_name(current, current.get("target_instrument_key"))
        lines.extend([
            "",
            f"Open simulated position: {current_symbol}",
            f"Entry: {_money(current.get('entry_price'))}",
        ])
    if recovery.get("daily_target_locked"):
        lines.append("Daily target reached • further Supertrend entries locked")
    lines.append("SIMULATION ONLY • No live order sent")
    return "\n".join(lines)
