"""
Shared helpers for manual isolation.

Used by both the HTTP router and the Telegram bot so parsing rules,
error messages, and reply formatting stay consistent.

Public API
----------
    parse_isolate_args(args)        -> ParsedIsolationRequest | None
    parse_strike(value)             -> float | None
    normalize_option_type(value)    -> "CE" | "PE" | None
    normalize_underlying(value)     -> str
    resolve_from_metadata(...)      -> ResolutionResult
    format_isolated_summary(isolated) -> str
    format_resolution_summary(res)  -> str
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def normalize_underlying(value: Any) -> str:
    """Uppercase, strip, replace spaces with nothing."""
    return str(value or "").strip().upper().replace(" ", "")


def parse_strike(value: Any) -> Optional[float]:
    """Accept ints, floats, and numeric strings. Reject <= 0."""
    if value is None:
        return None
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return f


def normalize_option_type(value: Any) -> Optional[str]:
    """Return 'CE', 'PE', or None."""
    if value is None:
        return None
    text = str(value).strip().upper()
    if text in ("CE", "CALL", "C"):
        return "CE"
    if text in ("PE", "PUT", "P"):
        return "PE"
    return None


@dataclass
class ParsedIsolationRequest:
    """
    Result of parsing a one-shot `/isolate <args>` command.

    Fields
    ------
    instrument_key : str | None
        Set when the user provided a direct key (e.g. "NSE_FO|40809").
        Mutually exclusive with (underlying, strike, option_type).
    underlying     : str | None
    strike         : float | None
    option_type    : str | None
    interactive    : bool
        True when the user typed `/isolate` with no args and we should
        start the step-by-step flow instead of executing immediately.
    error          : str | None
        Set when the args were invalid. Non-empty implies no resolution.
    """
    instrument_key: Optional[str] = None
    underlying: Optional[str] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None
    interactive: bool = False
    error: Optional[str] = None

    @property
    def is_one_shot(self) -> bool:
        return bool(
            not self.interactive
            and not self.error
            and (self.instrument_key or (self.underlying and self.strike and self.option_type))
        )


def parse_isolate_args(args: List[str]) -> ParsedIsolationRequest:
    """
    Parse the tokens after `/isolate`.

    Accepted forms
    --------------
    (empty)                              -> interactive flow
    ["NSE_FO|40809"]                     -> direct key
    ["nifty","23500","ce"]               -> underlying + strike + type
    """
    tokens = [str(a).strip() for a in (args or []) if str(a).strip()]

    if not tokens:
        return ParsedIsolationRequest(interactive=True)

    # Direct instrument key
    if len(tokens) == 1 and "|" in tokens[0]:
        return ParsedIsolationRequest(instrument_key=tokens[0])

    # underlying strike type
    if len(tokens) == 3:
        underlying = normalize_underlying(tokens[0])
        strike = parse_strike(tokens[1])
        option_type = normalize_option_type(tokens[2])

        if not underlying:
            return ParsedIsolationRequest(error="Underlying index is empty.")
        if strike is None:
            return ParsedIsolationRequest(
                error=f"Invalid strike value: {tokens[1]!r}"
            )
        if option_type is None:
            return ParsedIsolationRequest(
                error=f"Invalid option type: {tokens[2]!r} (use CE or PE)"
            )

        return ParsedIsolationRequest(
            underlying=underlying,
            strike=strike,
            option_type=option_type,
        )

    return ParsedIsolationRequest(
        error=(
            "Usage:\n"
            "  /isolate                     (interactive)\n"
            "  /isolate <instrument_key>\n"
            "  /isolate <underlying> <strike> <CE|PE>\n"
            "Example: /isolate NIFTY 23500 CE"
        )
    )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
@dataclass
class ResolutionResult:
    """Outcome of resolving (key | underlying+strike+type) against metadata."""
    resolved: Optional[Dict[str, Any]] = None
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    error_code: Optional[str] = None  # "not_found" | "ambiguous" | "invalid"

    @property
    def ok(self) -> bool:
        return self.resolved is not None and self.error is None


def _sort_key(meta: Dict[str, Any]) -> Tuple[int, float]:
    """
    Sort candidates: nearest future expiry first, then lowest strike.
    Strings are compared lexicographically, which matches YYYY-MM-DD.
    """
    expiry = str(meta.get("expiry") or "9999-12-31")
    strike = meta.get("strike") or 0.0
    try:
        return (0, float(str(strike)))
    except (TypeError, ValueError):
        return (0, 0.0)


def resolve_from_metadata(
    metadata: Dict[str, Dict[str, Any]],
    *,
    instrument_key: Optional[str] = None,
    underlying: Optional[str] = None,
    strike: Optional[float] = None,
    option_type: Optional[str] = None,
) -> ResolutionResult:
    """
    Look up the target contract in the isolation service's `_metadata`
    cache. The cache is populated at startup from the option-chain load,
    so it reflects every contract the isolation layer is tracking.

    Matching is case-insensitive on underlying and option type, and exact
    (float) on strike.
    """
    if not isinstance(metadata, dict) or not metadata:
        return ResolutionResult(
            error="Isolation metadata cache is empty — no contracts loaded yet.",
            error_code="not_found",
        )

    # --- Direct key ------------------------------------------------------
    if instrument_key:
        meta = metadata.get(instrument_key)
        if meta is None:
            # Try a case-insensitive scan as a courtesy
            target = instrument_key.strip().lower()
            for k, v in metadata.items():
                if k.lower() == target:
                    meta = v
                    break
        if meta is None:
            return ResolutionResult(
                error=f"Instrument key {instrument_key!r} is not tracked.",
                error_code="not_found",
            )
        return ResolutionResult(resolved=dict(meta), candidates=[dict(meta)])

    # --- underlying + strike + type -------------------------------------
    if not (underlying and strike is not None and option_type):
        return ResolutionResult(
            error="Provide either an instrument_key or (underlying, strike, option_type).",
            error_code="invalid",
        )

    want_underlying = normalize_underlying(underlying)
    want_type = normalize_option_type(option_type)

    matches: List[Dict[str, Any]] = []
    for meta in metadata.values():
        if normalize_underlying(meta.get("underlying")) != want_underlying:
            continue
        if normalize_option_type(meta.get("option_type")) != want_type:
            continue
        try:
            if float(meta.get("strike") or 0.0) != float(strike):
                continue
        except (TypeError, ValueError):
            continue
        matches.append(dict(meta))

    if not matches:
        return ResolutionResult(
            error=(
                f"No tracked contract found for "
                f"{want_underlying} {strike:g} {want_type}."
            ),
            error_code="not_found",
        )

    matches.sort(key=_sort_key)

    chosen = matches[0]
    if len(matches) > 1:
        # Multiple expiries match — pick nearest expiry, but tell the
        # caller that other candidates exist.
        chosen["_multiple_matches"] = True

    return ResolutionResult(resolved=chosen, candidates=matches)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def format_isolated_summary(isolated: Any) -> str:
    """
    Format a compact one-line summary of an IsolatedInstrument
    (dataclass or dict).
    """
    def _g(obj, key, default=None):
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    key = _g(isolated, "instrument_key") or "?"
    sym = _g(isolated, "trading_symbol") or key
    underlying = _g(isolated, "underlying") or "?"
    strike = _g(isolated, "strike")
    otype = _g(isolated, "option_type") or "?"
    level = _g(isolated, "selected_level") or "?"
    reason = _g(isolated, "selection_reason") or ""

    strike_str = f"{float(strike):g}" if strike is not None else "?"
    lines = [
        f"*{sym}*",
        f"  key:    `{key}`",
        f"  index:  {underlying}",
        f"  strike: {strike_str} {otype}",
        f"  level:  {level}",
    ]
    if reason:
        lines.append(f"  reason: {reason}")
    return "\n".join(lines)


def format_resolution_summary(res: ResolutionResult) -> str:
    """Human-readable summary for a resolution attempt."""
    if res.ok:
        meta = res.resolved or {}
        return (
            f"Resolved: {meta.get('trading_symbol') or meta.get('instrument_key')}\n"
            f"  key:    `{meta.get('instrument_key')}`\n"
            f"  index:  {meta.get('underlying')}\n"
            f"  strike: {meta.get('strike')} {meta.get('option_type')}\n"
            f"  expiry: {meta.get('expiry') or '?'}"
        )
    return f"Resolution failed: {res.error}"


__all__ = [
    "ParsedIsolationRequest",
    "ResolutionResult",
    "parse_isolate_args",
    "parse_strike",
    "normalize_option_type",
    "normalize_underlying",
    "resolve_from_metadata",
    "format_isolated_summary",
    "format_resolution_summary",
]