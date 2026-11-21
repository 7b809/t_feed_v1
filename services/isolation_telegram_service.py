"""Telegram notifications for isolation lifecycle events.

General runtime notifications continue to use TELEGRAM_CHAT_ID.
New isolation announcements are routed only to TELEGRAM_GROUP_CHAT_ID.
"""
from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from core.config import (
    TELEGRAM_ENABLED,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_GROUP_ENABLED,
    TELEGRAM_GROUP_CHAT_ID,
    TIMEZONE,
)
from core.logger import get_logger

logger = get_logger(__file__)
IST = ZoneInfo("Asia/Kolkata")


def _v(value, fallback="—"):
    return fallback if value in (None, "") else str(value)


def _safe(value, fallback="—") -> str:
    return html.escape(_v(value, fallback), quote=True)


def _first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _number(value, decimals: int = 2) -> str:
    if value in (None, ""):
        return "—"
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return _safe(value)


def _time_ist(value) -> str:
    """Render ISO timestamps in India time using a readable 12-hour clock."""
    if value in (None, ""):
        return "—"
    try:
        if isinstance(value, datetime):
            parsed = value
        else:
            raw = str(value).strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        else:
            parsed = parsed.astimezone(IST)
        return parsed.strftime("%d %b %Y, %I:%M:%S %p IST")
    except (TypeError, ValueError, OverflowError):
        return _safe(value)


def send_html(message: str) -> bool:
    """Send an operational/runtime notification to the legacy chat target."""
    if not (TELEGRAM_ENABLED and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    return _post_html(TELEGRAM_CHAT_ID, message, "runtime")


def send_group_html(message: str) -> bool:
    """Send an isolation announcement exclusively to the official group."""
    if not TELEGRAM_GROUP_ENABLED:
        logger.info("Isolation group notification skipped: TELEGRAM_GROUP_ENABLED=false")
        return False
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_GROUP_CHAT_ID:
        logger.warning(
            "Isolation group notification skipped: TELEGRAM_BOT_TOKEN or "
            "TELEGRAM_GROUP_CHAT_ID is not configured"
        )
        return False
    return _post_html(TELEGRAM_GROUP_CHAT_ID, message, "isolation-group")


def _post_html(chat_id: str, message: str, label: str) -> bool:
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        response.raise_for_status()
        logger.info("Telegram %s message delivered to configured chat", label)
        return True
    except Exception:
        logger.exception("Telegram %s notification failed", label)
        return False


def _format_leg(title: str, leg: dict, *, isolated: bool = False) -> list[str]:
    if not isinstance(leg, dict) or not leg:
        return [f"<b>{title}</b>", "Contract details: —"]
    option_type = _first(leg.get("option_type"), leg.get("instrument_type"))
    strike = _first(leg.get("strike"), leg.get("strike_price"))
    lines = [
        f"<b>{title}</b>",
        f"Symbol: <code>{_safe(leg.get('trading_symbol'))}</code>",
        f"Instrument key: <code>{_safe(leg.get('instrument_key'))}</code>",
        f"Exchange token: <code>{_safe(leg.get('exchange_token'))}</code>",
        (
            f"Contract: {_safe(leg.get('name') or leg.get('underlying') or 'NIFTY')} "
            f"· {_safe(option_type)} · Strike {_number(strike, 0)}"
        ),
        f"Expiry: {_safe(leg.get('expiry'))}",
        (
            f"Lot size: {_safe(leg.get('lot_size'))} "
            f"· Tick size: {_number(leg.get('tick_size'), 2)}"
        ),
        (
            f"Exchange / Segment: {_safe(leg.get('exchange'))} / "
            f"{_safe(leg.get('segment'))}"
        ),
    ]
    ltp = _first(leg.get("ltp_at_isolation"), leg.get("ltp"))
    if isolated:
        lines.append(
            f"Option LTP at isolation: <b>{_number(ltp)}</b> "
            f"({_safe(_first(leg.get('ltp_source'), leg.get('option_ltp_source')))})"
        )
    return lines


def _build_isolation_message(record: dict) -> str:
    """Accept either the Mongo isolation record or isolated_instrument.json shape."""
    archive_doc = record if isinstance(record, dict) else {}
    is_archive = isinstance(archive_doc.get("source_record"), dict)
    source = archive_doc.get("source_record") if is_archive else archive_doc
    source = source if isinstance(source, dict) else {}

    instruments = archive_doc.get("instruments") if is_archive else {}
    instruments = instruments if isinstance(instruments, dict) else {}
    isolated = instruments.get("isolated") if instruments else source
    opposite = instruments.get("opposite") if instruments else source.get("opposite_leg")
    isolated = isolated if isinstance(isolated, dict) else source
    opposite = opposite if isinstance(opposite, dict) else {}

    trigger = archive_doc.get("trigger") if is_archive else {}
    trigger = trigger if isinstance(trigger, dict) else {}
    explanation = source.get("explanation") or {}
    trigger_context = explanation.get("trigger_context") or {}
    if not isinstance(trigger_context, dict):
        trigger_context = {}

    trigger_index = _first(
        trigger.get("index"), trigger_context.get("index_key"), source.get("trigger_index")
    )
    canonical = _first(
        trigger.get("canonical"), trigger_context.get("canonical"),
        source.get("trigger_index_canonical"), source.get("underlying"), "NIFTY"
    )
    trigger_side = _first(trigger.get("side"), trigger_context.get("side"), source.get("trigger_side"))
    level_name = _first(
        trigger.get("level_name"), trigger_context.get("level_name"),
        source.get("trigger_level_name")
    )
    trigger_level = _first(
        trigger.get("level"), trigger.get("level_value"),
        trigger_context.get("level_value"), source.get("trigger_level")
    )
    index_ltp = _first(
        trigger.get("index_ltp"), trigger.get("index_ltp_at_touch"),
        trigger_context.get("index_ltp_at_touch"), source.get("trigger_value")
    )
    market = archive_doc.get("market_context") if is_archive else {}
    market = market if isinstance(market, dict) else {}
    nifty_ltp = _first(
        market.get("nifty_ltp"), source.get("nifty_ltp_at_isolation"),
        source.get("routed_ltp")
    )
    nifty_source = _first(market.get("nifty_ltp_source"), source.get("nifty_ltp_source"))

    execution = archive_doc.get("execution") if is_archive else {}
    execution = execution if isinstance(execution, dict) else {}
    mode = _first(execution.get("mode"), source.get("standard_execution_mode"), "—")
    reason = _first(execution.get("reason"), source.get("execution_mode_reason"))
    bias = _first(archive_doc.get("bias"), source.get("bias"))
    trend = explanation.get("trend") or {}
    if not isinstance(trend, dict):
        trend = {}

    timings = archive_doc.get("timings") if is_archive else {}
    timings = timings if isinstance(timings, dict) else {}
    touch_time = _first(timings.get("touch_time_ist"), source.get("isolation_touch_time"))
    created_time = _first(timings.get("created_at_ist"), source.get("isolation_created_at"))
    decision_time = _first(
        timings.get("execution_mode_decided_at"), source.get("execution_mode_decided_at")
    )
    archived_time = timings.get("archived_at_ist")
    trade_date = _first(archive_doc.get("trade_date_ist"), source.get("trade_date_ist"))
    backfilled = bool(_first(source.get("isolation_backfilled"), explanation.get("backfilled"), False))

    title = "🟠 BACKFILLED ISOLATION" if backfilled else "🟢 NEW INSTRUMENT ISOLATION"
    lines = [
        f"<b>{title}</b>",
        f"<i>Trade date: {_safe(trade_date)} · Time zone: IST</i>",
        "",
        "━━━━━━━━━━━━━━━━━━",
        "📍 <b>WHY WAS THIS INSTRUMENT ISOLATED?</b>",
        (
            f"Trigger index: <b>{_safe(canonical)}</b> "
            f"(<code>{_safe(trigger_index)}</code>)"
        ),
        f"Level touched: <b>{_safe(trigger_side)} · {_safe(level_name)}</b>",
        f"Trigger level: <b>{_number(trigger_level)}</b>",
        f"Index LTP at touch: <b>{_number(index_ltp)}</b>",
        (
            f"NIFTY LTP used for option selection: <b>{_number(nifty_ltp)}</b> "
            f"({_safe(nifty_source)})"
        ),
        "",
    ]
    lines.extend(_format_leg("🟢 ISOLATED INSTRUMENT", isolated, isolated=True))
    lines += [""]
    lines.extend(_format_leg("🔴 OPPOSITE INSTRUMENT", opposite))
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━",
        "🧭 <b>ISOLATION DECISION</b>",
        f"Selected bias: <b>{_safe(bias)}</b>",
        f"Execution mode: <b>{_safe(mode)}</b>",
    ]
    if reason:
        lines.append(f"Mode reason: {_safe(reason)}")
    if trend:
        lines.append(
            f"Market trend: <b>{_safe(trend.get('trend'))}</b> "
            f"· EMA: {_number(trend.get('ema'))}"
        )
        if trend.get("reason"):
            lines.append(f"Trend reason: {_safe(trend.get('reason'))}")
    lines += [
        "",
        "🕒 <b>EVENT TIMELINE (INDIA TIME)</b>",
        f"Index touch: {_time_ist(touch_time)}",
        f"Isolation created: {_time_ist(created_time)}",
        f"Execution mode decided: {_time_ist(decision_time)}",
    ]
    if archived_time:
        lines.append(f"Local archive saved: {_time_ist(archived_time)}")
    if backfilled:
        lines.append("⚠️ This isolation was reconstructed by startup backfill.")
    lines += [
        "",
        f"Status: <b>{_safe(_first(source.get('status'), archive_doc.get('status'), 'ACTIVE'))}</b>",
        f"Isolation ID: <code>{_safe(_first(archive_doc.get('isolation_id'), source.get('isolation_id')))}</code>",
    ]
    return "\n".join(lines)


def notify_isolation_created(record: dict, archive_path: str | None = None) -> bool:
    """Send a full isolation announcement only to TELEGRAM_GROUP_CHAT_ID."""
    try:
        message = _build_isolation_message(record)
        return send_group_html(message)
    except Exception:
        logger.exception("Could not format isolation group notification")
        return False


def notify_runtime_event(title: str, detail: str, emoji: str = "ℹ️") -> bool:
    return send_html(f"{emoji} <b>{html.escape(title)}</b>\n{html.escape(detail)}")


def notify_indicator_event(indicator: str, payload: dict, symbol: str, role: str) -> bool:
    """Log individual SR/Supertrend updates locally; never send Telegram spam."""
    event = payload.get("event")
    if indicator == "sr" and event == "sr_update":
        levels = payload.get("new_levels") or payload.get("confirmed_levels") or payload.get("levels")
        count = len(levels) if isinstance(levels, list) else 0
        logger.info(
            "[INDICATOR] New SR level update identified | instrument=%s | leg=%s | levels=%d",
            symbol, role, count,
        )
        return False
    if indicator == "supertrend" and event == "supertrend_signal":
        signal = _v(payload.get("signal") or payload.get("trend"))
        logger.info(
            "[INDICATOR] Supertrend signal identified | instrument=%s | leg=%s | signal=%s",
            symbol, role, signal,
        )
        return False
    return False
