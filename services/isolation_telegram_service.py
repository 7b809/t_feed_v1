"""Telegram notifications for isolation lifecycle events.

General runtime notifications continue to use TELEGRAM_CHAT_ID.
Isolation announcements and Supertrend alerts are sent to each enabled Telegram destination.
"""
from __future__ import annotations

import html
from urllib.parse import quote
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
    CHART_BASE_URL,
    INSTRUMENT_KEYS,
    INDEX_METADATA,
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


def send_isolation_html(message: str) -> bool:
    """Backward-compatible helper: send identical content to enabled destinations."""
    return send_isolation_notifications(message, message)


def send_isolation_notifications(group_message: str, private_message: str) -> bool:
    """Send audience-specific content to the group and private Telegram chat.

    The group receives only the concise public summary. The private chat keeps
    the detailed operational message. Each destination remains independently
    configurable, and duplicate chat IDs are delivered only once (group first).
    """
    if not TELEGRAM_BOT_TOKEN:
        return False

    destinations = []
    if TELEGRAM_GROUP_ENABLED and TELEGRAM_GROUP_CHAT_ID:
        destinations.append((TELEGRAM_GROUP_CHAT_ID, "isolation-group", group_message))
    if TELEGRAM_ENABLED and TELEGRAM_CHAT_ID:
        destinations.append((TELEGRAM_CHAT_ID, "isolation-chat", private_message))

    delivered = False
    seen = set()
    for chat_id, label, message in destinations:
        if chat_id in seen:
            logger.warning(
                "Telegram destination %s is configured for both group and private alerts; "
                "the group-safe message was sent only once.",
                chat_id,
            )
            continue
        seen.add(chat_id)
        delivered = _post_html(chat_id, message, label) or delivered
    if not destinations:
        logger.warning("Isolation Telegram notification skipped: no enabled destination is configured")
    return delivered


def send_isolation_notification_results(
    group_message: str,
    private_message: str,
    *,
    send_group: bool = True,
    send_private: bool = True,
) -> dict:
    """Deliver group/private variants independently and report each outcome.

    Disabled destinations are treated as intentionally skipped. A duplicate
    configured chat ID receives the group-safe message only, never a duplicate
    private message containing additional details.
    """
    results = {"group": True, "private": True}
    if not TELEGRAM_BOT_TOKEN:
        if send_group and TELEGRAM_GROUP_ENABLED:
            results["group"] = False
        if send_private and TELEGRAM_ENABLED:
            results["private"] = False
        return results

    group_id = str(TELEGRAM_GROUP_CHAT_ID or "")
    private_id = str(TELEGRAM_CHAT_ID or "")
    group_active = bool(send_group and TELEGRAM_GROUP_ENABLED)
    private_active = bool(send_private and TELEGRAM_ENABLED)

    if group_active:
        results["group"] = bool(group_id and _post_html(group_id, group_message, "time-window-group"))
    if private_active:
        if private_id and group_id and private_id == group_id and TELEGRAM_GROUP_ENABLED:
            # The same chat is already receiving the group-safe version.
            results["private"] = True
            logger.warning(
                "Telegram group/private destinations share one chat ID; "
                "only the group-safe message is delivered."
            )
        else:
            results["private"] = bool(
                private_id and _post_html(private_id, private_message, "time-window-private")
            )
    return results


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
    ltp = _first(leg.get("ltp_at_isolation"), leg.get("isolated_option_ltp"), leg.get("ltp"))
    if isolated:
        lines.append(
            f"Option LTP at isolation: <b>{_number(ltp)}</b> "
            f"({_safe(_first(leg.get('ltp_source'), leg.get('option_ltp_source')))})"
        )
    return lines


def format_chart_links(record: dict) -> list[str]:
    """Build safe chart links for both option legs, trigger index and configured main indexes."""
    if not CHART_BASE_URL:
        return []

    archive = record if isinstance(record, dict) else {}
    is_archive = isinstance(archive.get("source_record"), dict)
    source = archive.get("source_record") if is_archive else archive
    source = source if isinstance(source, dict) else {}
    instruments = archive.get("instruments") if is_archive else {}
    instruments = instruments if isinstance(instruments, dict) else {}
    isolated = instruments.get("isolated") if instruments else source
    opposite = instruments.get("opposite") if instruments else source.get("opposite_leg")
    trigger = archive.get("trigger") if is_archive else {}
    trigger = trigger if isinstance(trigger, dict) else {}
    trigger_key = trigger.get("index") or source.get("trigger_index")

    items = []
    def add(key, label):
        key = str(key or "").strip()
        if key and all(existing[0] != key for existing in items):
            items.append((key, label))

    if isinstance(isolated, dict):
        add(isolated.get("instrument_key"), "Isolated instrument chart")
    if isinstance(opposite, dict):
        add(opposite.get("instrument_key"), "Opposite instrument chart")
    related = source.get("related_instruments") or archive.get("related_instruments") or []
    if isinstance(related, list):
        for item in related:
            if isinstance(item, dict):
                add(item.get("instrument_key"), item.get("chart_label") or item.get("trading_symbol") or "Related instrument")
    related_keys = source.get("related_instrument_keys") or []
    if isinstance(related_keys, list):
        for key in related_keys:
            add(key, "Related instrument")

    if trigger_key:
        canonical = (
            trigger.get("canonical")
            or source.get("trigger_index_canonical")
            or INDEX_METADATA.get(trigger_key, {}).get("canonical")
            or trigger_key
        )
        add(trigger_key, f"Trigger index ({canonical})")

    # Include every configured main index so recipients can compare the
    # isolated contract with the wider index context.
    for key in INSTRUMENT_KEYS:
        meta = INDEX_METADATA.get(key, {})
        add(key, f"Main index ({meta.get('canonical') or key})")

    return [
        f'<a href="{html.escape(CHART_BASE_URL + "/chart/" + quote(key, safe=""), quote=True)}">📈 {html.escape(label)}</a>'
        for key, label in items
    ]


def format_option_chart_links(isolated_leg: dict, opposite_leg: dict) -> list[str]:
    """Return only the two clearly labelled option-chart links."""
    if not CHART_BASE_URL:
        return []
    links = []
    for leg, label, emoji in (
        (isolated_leg, "Isolated instrument chart", "📈"),
        (opposite_leg, "Opposite instrument chart", "📉"),
    ):
        key = str((leg or {}).get("instrument_key") or "").strip()
        if not key:
            continue
        url = CHART_BASE_URL.rstrip("/") + "/chart/" + quote(key, safe="")
        links.append(
            f'<a href="{html.escape(url, quote=True)}">{emoji} {html.escape(label)}</a>'
        )
    return links


def _build_group_isolation_message(record: dict) -> str:
    """Build a strategy-safe isolation announcement for the public group."""
    archive = record if isinstance(record, dict) else {}
    is_archive = isinstance(archive.get("source_record"), dict)
    source = archive.get("source_record") if is_archive else archive
    source = source if isinstance(source, dict) else {}
    instruments = archive.get("instruments") if is_archive else {}
    instruments = instruments if isinstance(instruments, dict) else {}
    isolated = instruments.get("isolated") if instruments else source
    opposite = instruments.get("opposite") if instruments else source.get("opposite_leg")
    isolated = isolated if isinstance(isolated, dict) else {}
    opposite = opposite if isinstance(opposite, dict) else {}
    timings = archive.get("timings") if is_archive else {}
    timings = timings if isinstance(timings, dict) else {}
    created_at = _first(
        timings.get("created_at_ist"), source.get("isolation_created_at"),
        source.get("isolation_touch_time")
    )
    trade_date = _first(archive.get("trade_date_ist"), source.get("trade_date_ist"))
    backfilled = bool(source.get("isolation_backfilled"))
    title = "🟠 INSTRUMENT ISOLATION RESTORED" if backfilled else "🟢 NEW INSTRUMENT ISOLATION"
    lines = [
        f"<b>{title}</b>",
        f"<i>{_safe(trade_date)} · IST</i>",
        "",
        f"🟢 <b>Isolated:</b> <code>{_safe(isolated.get('trading_symbol') or isolated.get('instrument_key'))}</code>",
        f"🔴 <b>Opposite:</b> <code>{_safe(opposite.get('trading_symbol') or opposite.get('instrument_key'))}</code>",
        f"🕒 <b>Created:</b> {_time_ist(created_at)}",
        f"<b>Status:</b> {_safe(_first(source.get('status'), archive.get('status'), 'ACTIVE'))}",
    ]
    links = format_option_chart_links(isolated, opposite)
    if links:
        lines += ["", "📊 <b>OPTION CHARTS</b>", *links]
    return "\n".join(lines)


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
    chart_links = format_chart_links(archive_doc)
    if chart_links:
        lines += ["", "📊 <b>CHARTS</b>", *chart_links]
    return "\n".join(lines)


def notify_isolation_created(record: dict, archive_path: str | None = None) -> bool:
    """Send a concise group announcement and the full detail to private chat."""
    try:
        group_message = _build_group_isolation_message(record)
        private_message = _build_isolation_message(record)
        return send_isolation_notifications(group_message, private_message)
    except Exception:
        logger.exception("Could not format isolation Telegram notifications")
        return False


def send_existing_isolation_test(record: dict) -> bool:
    """Send the saved isolation snapshot as a clearly labelled test."""
    try:
        message = _build_group_isolation_message(record)
        message = "🧪 <b>TEST MESSAGE · EXISTING ISOLATION DATA</b>\n\n" + message
        return send_group_html(message)
    except Exception:
        logger.exception("Could not format existing-isolation Telegram test")
        return False


def notify_runtime_event(title: str, detail: str, emoji: str = "ℹ️") -> bool:
    return send_html(f"{emoji} <b>{html.escape(title)}</b>\n{html.escape(detail)}")
