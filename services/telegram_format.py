# services/telegram_format.py
"""Telegram message formatter tailored for both mobile and desktop readability.

Design Decisions:
-----------------
1. HTML formatting: avoids Markdown escaping bugs with special characters (., _, -, ()).
2. Monospace values: wrapping key data (prices, strike keys, tokens) in <code> blocks
   makes them monospaced and tap-to-copy on mobile Telegram apps.
3. Compact row-by-row layout: keeps lines under 35-40 chars so nothing soft-wraps
   awkwardly on small mobile screens, while staying clean and organized on desktop.
4. Expandable blocks: long diagnostic text, traces, or JSON payloads are encapsulated
   inside <blockquote expandable> so mobile chats don't get bloated.
"""

import html
from typing import Any, Dict, List, Optional, Tuple, Union


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


def build(
    title: str,
    emoji: str = "🎯",
    subtitle: Optional[str] = None,
    fields: Optional[List[Tuple[str, Any]]] = None,
    raw_lines: Optional[List[str]] = None,
    details: Optional[str] = None,
    footer: Optional[str] = None,
) -> str:
    """Build a standard pre-formatted message payload (Style B).

    Parameters
    ----------
    title : str
        Main headline/action.
    emoji : str
        Visual prefix icon.
    subtitle : Optional[str]
        Secondary descriptor line.
    fields : Optional[List[Tuple[str, Any]]]
        List of (Label, Value) tuples rendered as compact key-value bullets.
    raw_lines : Optional[List[str]]
        Pre-formatted descriptive lines added to the body.
    details : Optional[str]
        Long/technical payload placed inside a collapsible Telegram blockquote.
    footer : Optional[str]
        Closing meta line (e.g. timestamp or engine run id).
    """
    chunks: List[str] = []

    # Header line
    header = f"{emoji} <b>{escape(title)}</b>"
    if subtitle:
        header += f"\n<i>{escape(subtitle)}</i>"
    chunks.append(header)

    # Key-value structured rows
    if fields:
        field_lines = [kv_line(lbl, val) for lbl, val in fields if val is not None]
        if field_lines:
            chunks.append("\n".join(field_lines))

    # Freeform narrative rows
    if raw_lines:
        valid_lines = [line for line in raw_lines if line]
        if valid_lines:
            chunks.append("\n".join(valid_lines))

    # Expandable section for heavy payloads / stacktraces
    if details:
        chunks.append(f'<blockquote expandable>{code(details)}</blockquote>')

    # Footer note
    if footer:
        chunks.append(f"⏱ <i>{escape(footer)}</i>")

    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# Domain-Specific Builders (Orders, Strategy Alerts, Tracking, Health)
# ---------------------------------------------------------------------------


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
    """Format trade executions and order notifications."""
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

    return build(
        title=f"Order {action_upper}",
        emoji=emoji,
        fields=fields,
    )


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
    """Format strategy execution stage progress (e.g. S2 Stage 1/2/3)."""
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
    """Format daily token refresh / auth lifecycle alerts."""
    emoji = "🔑" if is_valid else "⚠️"
    fields: List[Tuple[str, Any]] = [
        ("Service", service),
        ("Status", status),
    ]
    if expiry_time:
        fields.append(("Expires At", expiry_time))
    if error_message:
        fields.append(("Error", error_message))

    return build(
        title="Token Lifecycle Event",
        emoji=emoji,
        fields=fields,
    )