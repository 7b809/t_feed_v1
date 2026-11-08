# services/telegram_format.py
"""Telegram message formatter.

This module now supports **two** output styles simultaneously:

1. **Plain-Unicode (legacy)** — original helpers: ``DIVIDER``, ``THIN``,
   ``STATUS_ICON``, ``status_icon``, ``header``, ``divider``, ``line``,
   ``section``, ``bullet``, ``footer``, and the *old* ``build(emoji, title, ...)``
   signature. These return plain Unicode text with ``*bold*`` markers and are
   safe with no ``parse_mode``.

2. **HTML (new)** — ``escape``, ``code``, ``bold``, ``italic``, ``kv_line``,
   ``build(title=..., emoji=..., ...)``, plus domain builders
   ``format_order_alert``, ``format_stage_tracker``, ``format_token_status``.
   These return HTML and require ``parse_mode="HTML"`` on the wire (the
   companion ``TelegramService.send_message`` auto-detects and sets it).

The single ``build()`` entry point dispatches between the two styles based on
the arguments supplied, so existing call sites importing ``build, divider, THIN``
from this module keep working unchanged.
"""

from __future__ import annotations

import html
import re
from typing import Any, Dict, List, Optional, Tuple, Union


# =============================================================================
# Plain-Unicode helpers (kept for backwards compatibility)
# =============================================================================

DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
THIN = "─────────────────────────────"

STATUS_ICON: Dict[str, str] = {
    "success": "✅",
    "ok": "✅",
    "completed": "✅",
    "started": "🚀",
    "running": "⏳",
    "warning": "⚠️",
    "finished": "⚠️",
    "skipped": "⏭️",
    "no_data": "📭",
    "error": "❌",
    "failed": "❌",
    "disabled": "🚫",
    "info": "ℹ️",
    "isolated": "🔒",
    "activated": "🎯",
    "placed": "💰",
}


def status_icon(status: Optional[str]) -> str:
    return STATUS_ICON.get(str(status or "").lower(), "ℹ️")


def header(emoji: str, title: str) -> str:
    """Return the top-of-message header line (plain text)."""
    return f"{emoji} *{title.upper()}*"


def divider() -> str:
    return DIVIDER


def line(label: str, value: Any, *, icon: str = "•", pad: int = 12) -> str:
    """Return one formatted 'label: value' line (plain text)."""
    lbl = f"{label}:".ljust(pad)
    return f"{icon} {lbl}{value}"


def section(title: str, emoji: str = "📊") -> str:
    return f"\n{emoji} *{title}*"


def bullet(text: str, emoji: str = "•") -> str:
    return f"   {emoji} {text}"


def footer(status: str, detail: str = "") -> str:
    icon = status_icon(status)
    word = status.upper()
    suffix = f" — {detail}" if detail else ""
    return f"\n{icon} *Status: {word}*{suffix}"


# =============================================================================
# HTML helpers (new)
# =============================================================================

def escape(text: Any) -> str:
    """Safely escape text for Telegram HTML parse_mode."""
    if text is None:
        return ""
    return html.escape(str(text))


def code(text: Any) -> str:
    """Format inline monospaced copyable text."""
    return f"<code>{escape(text)}</code>"


def bold(text: Any) -> str:
    """Format bold text."""
    return f"<b>{escape(text)}</b>"


def italic(text: Any) -> str:
    """Format italic text."""
    return f"<i>{escape(text)}</i>"


def kv_line(label: str, value: Any, mono: bool = True) -> str:
    """Render a single key-value row optimized for narrow mobile widths."""
    formatted_val = code(value) if mono else escape(value)
    return f"• <b>{escape(label)}:</b> {formatted_val}"


# =============================================================================
# Global notification layout
# =============================================================================

_LEVEL_STYLE: Dict[str, Tuple[str, str]] = {
    "SUCCESS": ("✅", "SUCCESS"),
    "WARNING": ("⚠️", "WARNING"),
    "ERROR": ("❌", "ERROR"),
    "CRITICAL": ("🚨", "CRITICAL"),
    "STARTUP": ("🚀", "STARTUP"),
    "INFO": ("ℹ️", "INFO"),
}


def _clean_body_lines(value: str) -> str:
    """Remove legacy decorative rules and excessive blank lines."""
    lines: List[str] = []
    for raw_line in str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line_value = raw_line.rstrip()
        compact = line_value.strip()
        if compact and all(ch in "━─—_ " for ch in compact):
            continue
        lines.append(line_value)
    cleaned = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def _legacy_markup_to_html(value: str) -> str:
    """Convert the legacy *bold* convention after HTML escaping."""
    escaped = escape(_clean_body_lines(value))
    escaped = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", escaped)
    # Normalize legacy bullet key/value rows to the same bold-label layout.
    return re.sub(
        r"(?m)^(\s*[•◦▪]\s*)([^:\n]{1,40}:)(\s*)",
        r"\1<b>\2</b>\3",
        escaped,
    )


def _looks_like_html_fragment(value: str) -> bool:
    return bool(re.search(
        r"</?(?:b|strong|i|em|u|s|code|pre|blockquote|a|tg-spoiler)\b",
        value or "",
        re.IGNORECASE,
    ))


def format_notification(
    title: str,
    message: str = "",
    level: str = "INFO",
) -> str:
    """Build every outbound notification with one compact Telegram HTML layout.

    Telegram has no responsive font-size controls. This layout therefore uses
    a consistent short heading, compact line spacing and readable key/value
    rows that wrap naturally on narrow phone screens.
    """
    normalized_level = str(level or "INFO").strip().upper()
    icon, label = _LEVEL_STYLE.get(normalized_level, _LEVEL_STYLE["INFO"])
    title_text = str(title or "").strip()

    if message:
        heading = f"{icon} <b>{escape(title_text)}</b>"
        body_text = _clean_body_lines(message)
        if _looks_like_html_fragment(body_text):
            body = body_text
        else:
            body = _legacy_markup_to_html(body_text)
        return heading if not body else f"{heading}\n\n{body}"

    # A preformatted HTML builder payload is retained as a body, but receives
    # the same severity strip used by every other outbound notification.
    if _looks_like_html_fragment(title_text):
        return f"<i>{icon} {label}</i>\n\n{title_text}"

    # Legacy build(...) callers pass complete multi-line plain-text messages
    # as the title positional argument. Treat them as a body, not a headline.
    if "\n" in title_text:
        body = _legacy_markup_to_html(title_text)
        return f"<i>{icon} {label}</i>\n\n{body}" if body else f"{icon} <b>MARKET ENGINE</b>"

    return f"{icon} <b>{escape(title_text)}</b>"


def format_bot_reply(message: str) -> str:
    """Apply the shared compact layout to Telegram command responses."""
    body = _legacy_markup_to_html(str(message or ""))
    heading = "🤖 <b>MARKET ENGINE</b>"
    return heading if not body else f"{heading}\n\n{body}"


# =============================================================================
# build() — dispatches between legacy (plain) and new (HTML) call styles
# =============================================================================

# Keywords that only exist in the legacy signature
_LEGACY_KWARGS = {"sections", "status", "status_detail", "icon_map"}
# Keywords that only exist in the new signature
_NEW_KWARGS = {"subtitle", "raw_lines", "details", "footer"}


def _build_legacy(
    emoji: str,
    title: str,
    fields: Optional[List[Tuple[str, Any]]] = None,
    sections: Optional[List[Tuple[str, str, List[str]]]] = None,
    status: str = "info",
    status_detail: str = "",
    icon_map: Optional[Dict[str, str]] = None,
) -> str:
    """Original plain-Unicode build — preserved verbatim."""
    parts: List[str] = [header(emoji, title), divider()]

    if fields:
        icon_map = icon_map or {}
        for label, value in fields:
            ic = icon_map.get(label, "•")
            parts.append(line(label, value, icon=ic))

    if sections:
        for sec_title, sec_emoji, bullets in sections:
            parts.append(section(sec_title, sec_emoji))
            for b in bullets:
                parts.append(bullet(b))

    parts.append(footer(status, status_detail))
    return "\n".join(parts)


def _build_html(
    title: str,
    emoji: str = "🎯",
    subtitle: Optional[str] = None,
    fields: Optional[List[Tuple[str, Any]]] = None,
    raw_lines: Optional[List[str]] = None,
    details: Optional[str] = None,
    footer: Optional[str] = None,
) -> str:
    """New HTML build — returns HTML that requires parse_mode='HTML'."""
    chunks: List[str] = []

    head = f"{emoji} <b>{escape(title)}</b>"
    if subtitle:
        head += f"\n<i>{escape(subtitle)}</i>"
    chunks.append(head)

    if fields:
        field_lines = [kv_line(lbl, val) for lbl, val in fields if val is not None]
        if field_lines:
            chunks.append("\n".join(field_lines))

    if raw_lines:
        valid = [ln for ln in raw_lines if ln]
        if valid:
            chunks.append("\n".join(valid))

    if details:
        chunks.append(f"<blockquote expandable>{code(details)}</blockquote>")

    if footer:
        chunks.append(f"⏱ <i>{escape(footer)}</i>")

    return "\n\n".join(chunks)


def _looks_like_legacy_positional(first: Any, second: Any) -> bool:
    """Heuristic: legacy ``build(emoji, title, ...)`` has a short emoji first."""
    if not (isinstance(first, str) and isinstance(second, str)):
        return False
    return len(first) <= 4 and len(second) > len(first)


def build(*args: Any, **kwargs: Any) -> str:
    """Unified builder supporting both the legacy and new call styles.

    Legacy — returns plain Unicode (no ``parse_mode`` needed)::

        build("🎯", "STRATEGY 1 · RUNNING",
              fields=[("Instrument", "NIFTY")],
              status="success")

    New — returns HTML (``parse_mode="HTML"``)::

        build(title="STRATEGY 1 · RUNNING", emoji="🎯",
              fields=[("Instrument", "NIFTY")],
              subtitle="Status: RUNNING")
    """
    # 1) Explicit legacy keyword → legacy rendering
    if _LEGACY_KWARGS & kwargs.keys():
        return _build_legacy(*args, **kwargs)

    # 2) Explicit new keyword → HTML rendering
    if _NEW_KWARGS & kwargs.keys():
        return _build_html(*args, **kwargs)

    # 3) Positional heuristic: build(emoji, title, ...) vs build(title, ...)
    if len(args) >= 2 and _looks_like_legacy_positional(args[0], args[1]):
        return _build_legacy(*args, **kwargs)

    # 4) Default: new-style HTML build
    return _build_html(*args, **kwargs)


# =============================================================================
# Domain-Specific Builders (Orders, Strategy Alerts, Token Status)
# =============================================================================

def format_order_alert(
    action: str,
    symbol: str,
    quantity: int,
    price: float,
    order_id: Optional[str] = None,
    order_type: str = "MARKET",
    sl: Optional[float] = None,
    target: Optional[float] = None,
    tag: Optional[str] = None,
) -> str:
    """Format trade executions and order notifications (HTML)."""
    action_upper = action.upper()
    emoji = "🟢" if "BUY" in action_upper else "🔴" if "SELL" in action_upper else "⚡"

    fields: List[Tuple[str, Any]] = [
        ("Instrument", symbol),
        ("Qty", quantity),
        ("Price", f"{price:.2f}" if isinstance(price, (int, float)) else price),
        ("Type", order_type),
    ]

    if sl is not None:
        fields.append(("SL", f"{sl:.2f}" if isinstance(sl, (int, float)) else sl))
    if target is not None:
        fields.append(("Target", f"{target:.2f}" if isinstance(target, (int, float)) else target))
    if order_id:
        fields.append(("Order ID", order_id))
    if tag:
        fields.append(("Tag", tag))

    return build(title=f"Order {action_upper}", emoji=emoji, fields=fields)


def format_stage_tracker(
    strategy_name: str,
    stage: Union[str, int],
    status: str,
    symbol: Optional[str] = None,
    ltp: Optional[float] = None,
    trigger_rule: Optional[str] = None,
    metrics: Optional[Dict[str, Any]] = None,
    extra_details: Optional[str] = None,
) -> str:
    """Format strategy execution stage progress (HTML)."""
    fields: List[Tuple[str, Any]] = []

    if symbol:
        fields.append(("Symbol", symbol))
    if ltp is not None:
        fields.append(("LTP", f"{ltp:.2f}" if isinstance(ltp, (int, float)) else ltp))
    if trigger_rule:
        fields.append(("Trigger", trigger_rule))

    if metrics:
        for k, v in metrics.items():
            formatted_val = f"{v:.2f}" if isinstance(v, float) else v
            fields.append((k, formatted_val))

    return build(
        title=f"{strategy_name} · Stage {stage}",
        emoji="🎯",
        subtitle=f"Status: {status.upper()}",
        fields=fields,
        details=extra_details,
    )


def format_token_status(
    service: str,
    status: str,
    expiry_time: Optional[str] = None,
    is_valid: bool = True,
    error_message: Optional[str] = None,
) -> str:
    """Format daily token refresh / auth lifecycle alerts (HTML)."""
    emoji = "🔑" if is_valid else "⚠️"
    fields: List[Tuple[str, Any]] = [
        ("Service", service),
        ("Status", status),
    ]
    if expiry_time:
        fields.append(("Expires At", expiry_time))
    if error_message:
        fields.append(("Error", error_message))

    return build(title="Token Lifecycle Event", emoji=emoji, fields=fields)