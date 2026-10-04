"""
Interactive isolation flow for the Telegram bot.

This module owns:

  * one-shot `/isolate nifty 23500 ce` and `/isolate NSE_FO|40809`
  * the step-by-step flow: `/isolate` -> underlying -> strike -> type
  * `/unisolate <bucket>` and `/unisolate` (interactive)
  * `/cancel` support at any step

Integration contract (see the caller snippet at the bottom of this
file's docstring; the actual bot.py is project-specific):

    from telegram_app import isolation_flow

    # 1. In your message dispatcher, BEFORE anything else:
    reply = isolation_flow.feed_message(chat_id, text)
    if reply is not None:
        send(chat_id, reply.text)
        return

    # 2. When a message starts with `/isolate` or `/unisolate`:
    reply = isolation_flow.start_isolate(chat_id, args)
    # or
    reply = isolation_flow.start_unisolate(chat_id, args)
    send(chat_id, reply.text)

    # 3. In your /cancel handler:
    reply = isolation_flow.cancel_flow(chat_id)
    if reply is not None:
        send(chat_id, reply.text)
        return
    # else fall through to whatever /cancel already did
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from ema_app.isolation.manual_ops import (
    format_isolated_summary,
    normalize_option_type,
    normalize_underlying,
    parse_isolate_args,
    parse_strike,
)
from ema_app.isolation.service import isolation_service
from ema_app.isolation.state import GLOBAL_SCOPE_KEY

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Flow state
# ---------------------------------------------------------------------------
@dataclass
class PendingFlow:
    """State for one in-progress interactive flow, per chat."""
    kind: str                        # "isolate" | "unisolate"
    step: str                        # "underlying" | "strike" | "option_type" | "bucket"
    underlying: str = ""
    strike: Optional[float] = None
    option_type: str = ""
    bucket_key: str = ""
    started_at: float = field(default_factory=time.monotonic)


@dataclass
class FlowReply:
    """Reply payload returned to the caller for delivery."""
    text: str
    done: bool = False               # True when the flow ended (success or cancel)
    error: bool = False              # True when the reply is an error message


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------
_LOCK = threading.Lock()
_PENDING: Dict[int, PendingFlow] = {}
_PENDING_TTL_SEC = 300               # auto-expire after 5 minutes of inactivity


def _prune_expired() -> None:
    now = time.monotonic()
    with _LOCK:
        stale = [
            chat_id for chat_id, flow in _PENDING.items()
            if (now - flow.started_at) > _PENDING_TTL_SEC
        ]
        for chat_id in stale:
            _PENDING.pop(chat_id, None)


def is_active(chat_id: int) -> bool:
    _prune_expired()
    with _LOCK:
        return chat_id in _PENDING


def _set_flow(chat_id: int, flow: PendingFlow) -> None:
    with _LOCK:
        _PENDING[chat_id] = flow


def _pop_flow(chat_id: int) -> Optional[PendingFlow]:
    with _LOCK:
        return _PENDING.pop(chat_id, None)


def _touch(chat_id: int) -> None:
    with _LOCK:
        flow = _PENDING.get(chat_id)
        if flow is not None:
            flow.started_at = time.monotonic()


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
def _prompt_underlying() -> str:
    return (
        "Which underlying index? (e.g. NIFTY, SENSEX)\n"
        "Type /cancel at any step to abort."
    )


def _prompt_strike(underlying: str) -> str:
    return f"Strike value for {underlying}? (e.g. 23500)"


def _prompt_option_type(underlying: str, strike: float) -> str:
    return f"Strike type for {underlying} {strike:g}? (CE or PE)"


def _prompt_bucket() -> str:
    return (
        "Which bucket to clear? (e.g. NIFTY, SENSEX)\n"
        "Use * to clear every bucket.\n"
        "Type /cancel to abort."
    )


# ---------------------------------------------------------------------------
# Execution helpers
# ---------------------------------------------------------------------------
def _execute_isolate(
    chat_id: int,
    *,
    underlying: Optional[str] = None,
    strike: Optional[float] = None,
    option_type: Optional[str] = None,
    instrument_key: Optional[str] = None,
) -> FlowReply:
    """
    Run manual_select via the service and format a Telegram reply.
    """
    result = isolation_service.manual_select(
        instrument_key=instrument_key,
        underlying=underlying,
        strike=strike,
        option_type=option_type,
        reason="telegram_manual_override",
        actor=f"telegram:{chat_id}",
    )

    if not result.get("success"):
        return FlowReply(
            text=f"Isolation failed: {result.get('error') or 'unknown error'}",
            done=True,
            error=True,
        )

    previous = result.get("previous_instrument_key")
    lines = ["Isolated."]
    lines.append(result.get("summary") or "")
    lines.append(f"bucket: `{result.get('bucket_key')}`")
    if previous:
        lines.append(f"previous: `{previous}`")
    lines.append("")
    lines.append("Clear with /unisolate " + str(result.get("bucket_key")))

    return FlowReply(text="\n".join(lines).strip(), done=True)


def _execute_clear(chat_id: int, bucket_key: str) -> FlowReply:
    result = isolation_service.manual_clear(bucket_key)
    if not result.get("success"):
        return FlowReply(
            text=f"Clear failed: {result.get('error') or 'unknown error'}",
            done=True,
            error=True,
        )

    cleared = result.get("cleared") or []
    if not cleared:
        return FlowReply(text="Nothing was cleared.", done=True)

    lines = ["Cleared:"]
    for c in cleared:
        prev = c.get("previous_instrument_key") or "-"
        lines.append(f"  `{c.get('bucket_key')}` (was {prev})")
    return FlowReply(text="\n".join(lines), done=True)


# ---------------------------------------------------------------------------
# Entry points — /isolate
# ---------------------------------------------------------------------------
def start_isolate(chat_id: int, args: List[str]) -> FlowReply:
    """
    Called when the user types `/isolate <args>`.

    args is the list of tokens after the command, e.g. ["nifty","23500","ce"].
    Empty args -> interactive flow.
    """
    _prune_expired()

    parsed = parse_isolate_args(args)

    # ---- Parse error -----------------------------------------------------
    if parsed.error:
        return FlowReply(text=parsed.error, done=True, error=True)

    # ---- Interactive mode ------------------------------------------------
    if parsed.interactive:
        _set_flow(
            chat_id,
            PendingFlow(kind="isolate", step="underlying"),
        )
        return FlowReply(text=_prompt_underlying(), done=False)

    # ---- One-shot: direct key -------------------------------------------
    if parsed.instrument_key:
        return _execute_isolate(chat_id, instrument_key=parsed.instrument_key)

    # ---- One-shot: underlying + strike + type ---------------------------
    return _execute_isolate(
        chat_id,
        underlying=parsed.underlying,
        strike=parsed.strike,
        option_type=parsed.option_type,
    )


# ---------------------------------------------------------------------------
# Entry points — /unisolate
# ---------------------------------------------------------------------------
def start_unisolate(chat_id: int, args: List[str]) -> FlowReply:
    """
    Called when the user types `/unisolate <bucket>`.

    args empty        -> interactive prompt
    args = ["nifty"]  -> clear NIFTY bucket
    args = ["*"]      -> clear all buckets
    args = ["global"] -> clear the global sentinel bucket
    """
    _prune_expired()

    tokens = [str(a).strip() for a in (args or []) if str(a).strip()]

    if not tokens:
        _set_flow(chat_id, PendingFlow(kind="unisolate", step="bucket"))
        return FlowReply(text=_prompt_bucket(), done=False)

    if len(tokens) > 1:
        return FlowReply(
            text="Usage: /unisolate <bucket|*>\n"
                 "Examples: `/unisolate nifty`, `/unisolate *`",
            done=True,
            error=True,
        )

    token = tokens[0]
    if token == "*":
        return _execute_clear(chat_id, "*")

    upper = token.upper()
    if upper in ("GLOBAL", "__GLOBAL__"):
        upper = GLOBAL_SCOPE_KEY

    return _execute_clear(chat_id, upper)


# ---------------------------------------------------------------------------
# Message feeding (called for EVERY inbound message from the bot)
# ---------------------------------------------------------------------------
def feed_message(chat_id: int, text: str) -> Optional[FlowReply]:
    """
    If the chat has an active flow, consume `text` as the next step and
    return the reply. Otherwise return None so the caller can dispatch
    normally.

    Never raises: all internal errors are converted to a FlowReply.
    """
    _prune_expired()

    with _LOCK:
        flow = _PENDING.get(chat_id)

    if flow is None:
        return None

    _touch(chat_id)
    stripped = (text or "").strip()
    if not stripped:
        return FlowReply(text="Empty input. Type /cancel to abort.")

    # ---- /isolate flow ---------------------------------------------------
    if flow.kind == "isolate":
        if flow.step == "underlying":
            underlying = normalize_underlying(stripped)
            if not underlying:
                return FlowReply(text="Underlying must be non-empty.")

            flow.underlying = underlying
            flow.step = "strike"
            _set_flow(chat_id, flow)
            return FlowReply(text=_prompt_strike(underlying))

        if flow.step == "strike":
            strike = parse_strike(stripped)
            if strike is None:
                return FlowReply(
                    text=f"Invalid strike: {stripped!r}. Try a number like 23500."
                )
            flow.strike = strike
            flow.step = "option_type"
            _set_flow(chat_id, flow)
            return FlowReply(
                text=_prompt_option_type(flow.underlying, strike)
            )

        if flow.step == "option_type":
            opt = normalize_option_type(stripped)
            if opt is None:
                return FlowReply(text="Type CE or PE.")

            # Flow complete — resolve and execute.
            _pop_flow(chat_id)
            return _execute_isolate(
                chat_id,
                underlying=flow.underlying,
                strike=flow.strike,
                option_type=opt,
            )

        # Should not happen.
        _pop_flow(chat_id)
        return FlowReply(text="Internal flow error; state reset.", done=True, error=True)

    # ---- /unisolate flow -------------------------------------------------
    if flow.kind == "unisolate":
        # Only one step.
        _pop_flow(chat_id)
        token = stripped
        if token == "*":
            return _execute_clear(chat_id, "*")
        upper = token.upper()
        if upper in ("GLOBAL", "__GLOBAL__"):
            upper = GLOBAL_SCOPE_KEY
        return _execute_clear(chat_id, upper)

    # Unknown flow kind — clean up defensively.
    _pop_flow(chat_id)
    return FlowReply(text="Unknown flow state; reset.", done=True, error=True)


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------
def cancel_flow(chat_id: int) -> Optional[FlowReply]:
    """
    Cancel any in-progress flow for this chat.

    Returns None if there was nothing to cancel — the caller should then
    fall through to whatever /cancel did before this module existed
    (e.g. the token-save cancel).
    """
    flow = _pop_flow(chat_id)
    if flow is None:
        return None
    return FlowReply(text="Cancelled.", done=True)


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------
def help_text() -> str:
    return (
        "Isolation commands:\n"
        "  /isolate                     interactive: ask underlying, strike, type\n"
        "  /isolate NIFTY 23500 CE      one-shot\n"
        "  /isolate NSE_FO|40809        one-shot by key\n"
        "  /unisolate NIFTY             clear one bucket\n"
        "  /unisolate *                 clear every bucket\n"
        "  /cancel                      abort an in-progress flow"
    )


__all__ = [
    "FlowReply",
    "PendingFlow",
    "start_isolate",
    "start_unisolate",
    "feed_message",
    "cancel_flow",
    "is_active",
    "help_text",
]