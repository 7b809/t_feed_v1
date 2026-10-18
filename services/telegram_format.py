"""Shared helpers for nicely formatted Telegram messages.

Every Strategy 1 / Strategy 2 Telegram message uses these helpers so the
visual language stays consistent across the whole pipeline.

Design principles:
    * Header line starts with an emoji + bold UPPER headline.
    * Divider line separates header from body.
    * Body lines use "<emoji> <label>: <value>" with a fixed label width.
    * Section headers start with an emoji.
    * Status footer: final line with an emoji + status word.

All output is plain Unicode text — no HTML, no Markdown. That means the
messages render identically regardless of parse_mode and never break on
special characters.
"""

from __future__ import annotations

from typing import Any

DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
THIN = "─────────────────────────────"

# Status → emoji
STATUS_ICON = {
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

def status_icon(status: str | None) -> str:
    return STATUS_ICON.get(str(status or "").lower(), "ℹ️")

def header(emoji: str, title: str) -> str:
    """Return the top-of-message header line."""
    return f"{emoji} *{title.upper()}*"

def divider() -> str:
    return DIVIDER

def line(label: str, value: Any, *, icon: str = "•", pad: int = 12) -> str:
    """Return one formatted 'label: value' line."""
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

def build(
    emoji: str,
    title: str,
    fields: list[tuple[str, Any]] | None = None,
    sections: list[tuple[str, str, list[str]]] | None = None,
    status: str = "info",
    status_detail: str = "",
    icon_map: dict[str, str] | None = None,
) -> str:
    """Assemble a full formatted message.

    Parameters
    ----------
    emoji        : leading emoji for the header
    title        : UPPER headline (e.g. "STRATEGY 1 · STAGE 1 · RUNNING")
    fields       : list of (label, value) pairs shown as a key block
    sections     : list of (section_title, section_emoji, list_of_bullets)
    status       : "success" / "warning" / "error" / "info" / "running" / ...
    status_detail: extra text appended to the status line
    icon_map     : optional dict label -> icon override for field icons
    """
    parts: list[str] = [header(emoji, title), divider()]

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