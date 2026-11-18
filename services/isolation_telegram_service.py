"""Professional, compact Telegram notifications for isolation lifecycle events."""
from __future__ import annotations

import html
import requests

from core.config import TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from core.logger import get_logger

logger = get_logger(__file__)


def _v(value, fallback="—"):
    return fallback if value in (None, "") else str(value)


def send_html(message: str) -> bool:
    if not (TELEGRAM_ENABLED and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except Exception:
        logger.exception("Telegram isolation notification failed")
        return False


def notify_isolation_created(record: dict, archive_path: str | None = None) -> bool:
    opposite = record.get("opposite_leg") or {}
    mode = _v(record.get("standard_execution_mode"))
    mode_label = "Early Extreme Reversal" if mode == "EARLY_EXTREME_REVERSAL" else "Standard"
    lines = [
        "🟢 <b>NEW ISOLATION CREATED</b>",
        "",
        f"<b>Underlying:</b> {html.escape(_v(record.get('underlying'), 'NIFTY'))}",
        f"<b>Bias:</b> {html.escape(_v(record.get('bias')))}",
        f"<b>Trigger:</b> {html.escape(_v(record.get('trigger_index_canonical')))} "
        f"{html.escape(_v(record.get('trigger_side')))} · "
        f"{html.escape(_v(record.get('trigger_level_name')))} "
        f"@ {html.escape(_v(record.get('trigger_level')))}",
        "",
        "<b>ISOLATED LEG</b>",
        f"Symbol: <code>{html.escape(_v(record.get('trading_symbol')))}</code>",
        f"Key: <code>{html.escape(_v(record.get('instrument_key')))}</code>",
        f"LTP: {_v(record.get('isolated_option_ltp'))}",
        "",
        "<b>OPPOSITE LEG</b>",
        f"Symbol: <code>{html.escape(_v(opposite.get('trading_symbol')))}</code>",
        f"Key: <code>{html.escape(_v(opposite.get('instrument_key')))}</code>",
        "",
        f"<b>Execution:</b> {html.escape(mode_label)}",
        f"<b>Touch time:</b> {html.escape(_v(record.get('isolation_touch_time')))}",
        f"<b>Created:</b> {html.escape(_v(record.get('isolation_created_at')))}",
        f"<b>Backfilled:</b> {'Yes' if record.get('isolation_backfilled') else 'No'}",
    ]
    if archive_path:
        lines += ["", f"<b>Archive:</b> <code>{html.escape(archive_path)}</code>"]
    return send_html("\n".join(lines))


def notify_runtime_event(title: str, detail: str, emoji: str = "ℹ️") -> bool:
    return send_html(f"{emoji} <b>{html.escape(title)}</b>\n{html.escape(detail)}")


def notify_indicator_event(indicator: str, payload: dict, symbol: str, role: str) -> bool:
    """Notify only meaningful confirmed indicator events, never snapshots/heartbeats."""
    event = payload.get("event")
    if indicator == "supertrend" and event == "supertrend_signal":
        signal = _v(payload.get("signal") or payload.get("trend"))
        when = _v(payload.get("confirmed_time") or payload.get("timestamp"))
        price = _v(payload.get("price") or payload.get("close"))
        message = (
            "📈 <b>SUPERTREND SIGNAL CONFIRMED</b>\n"
            f"<b>Instrument:</b> {html.escape(symbol)}\n"
            f"<b>Leg:</b> {html.escape(role)}\n"
            f"<b>Signal:</b> {html.escape(signal)}\n"
            f"<b>Time:</b> {html.escape(when)}\n"
            f"<b>Price:</b> {html.escape(price)}"
        )
        return send_html(message)

    if indicator == "sr" and event == "sr_update":
        levels = payload.get("new_levels") or payload.get("confirmed_levels") or payload.get("levels")
        if not isinstance(levels, list) or not levels:
            return False
        lines = [
            "🧭 <b>SUPPORT / RESISTANCE UPDATE</b>",
            f"<b>Instrument:</b> {html.escape(symbol)}",
            f"<b>Leg:</b> {html.escape(role)}",
        ]
        for item in levels[:8]:
            if not isinstance(item, dict):
                continue
            kind = _v(item.get("level_type") or item.get("sr_type") or item.get("type"))
            price = _v(item.get("price") or item.get("level_price") or item.get("value"))
            confirmed = _v(item.get("confirmed_time") or item.get("confirmed_at") or item.get("timestamp"))
            lines.append(f"• {html.escape(kind)} · <code>{html.escape(price)}</code> · {html.escape(confirmed)}")
        if len(lines) == 3:
            return False
        return send_html("\n".join(lines))
    return False
