import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[A-Z0-9]+")


def _tokens(value: Any) -> set[str]:
    return set(_TOKEN_RE.findall(str(value).upper()))


def _matches_underlying(item: dict, spec_value: str) -> bool:
    """
    Case-insensitive match of a spec string against the instrument's
    identity fields. Multi-word specs are supported ("NIFTY 50").

    Token-based matching avoids the classic NIFTY/BANKNIFTY trap.
    """
    spec_tokens = _TOKEN_RE.findall(spec_value.upper())
    if not spec_tokens:
        return True

    haystack = " ".join(
        str(item.get(field, ""))
        for field in ("underlying_key", "underlying", "trading_symbol")
    )
    item_tokens = _tokens(haystack)

    return all(tok in item_tokens for tok in spec_tokens)


def match_spec(item: dict, spec: dict) -> bool:
    """Return True if `item` satisfies every field present in `spec`."""
    if "instrument_key" in spec:
        if str(item.get("instrument_key", "")) != str(spec["instrument_key"]):
            return False

    if "underlying" in spec:
        if not _matches_underlying(item, spec["underlying"]):
            return False

    if "type" in spec:
        if str(item.get("instrument_type", "")).upper() != str(spec["type"]).upper():
            return False

    if "mode" in spec:
        if str(item.get("mode", "")).lower() != str(spec["mode"]).lower():
            return False

    if "expiry" in spec:
        if str(item.get("expiry", "")) != str(spec["expiry"]):
            return False

    if "strike" in spec:
        try:
            if float(item.get("strike_price")) != float(spec["strike"]):
                return False
        except (TypeError, ValueError):
            return False

    if "strike_min" in spec:
        try:
            if float(item.get("strike_price")) < float(spec["strike_min"]):
                return False
        except (TypeError, ValueError):
            return False

    if "strike_max" in spec:
        try:
            if float(item.get("strike_price")) > float(spec["strike_max"]):
                return False
        except (TypeError, ValueError):
            return False

    return True


def find_matches(items: list[dict], wanted: list[dict]) -> list[dict]:
    """
    Return the subset of `items` that matches at least one spec in
    `wanted`. De-duplicates by instrument_key.
    """
    if not wanted:
        return []

    matched: list[dict] = []
    seen: set[str] = set()

    for item in items:
        if not isinstance(item, dict):
            continue
        ikey = item.get("instrument_key")
        if not ikey or ikey in seen:
            continue
        for spec in wanted:
            if match_spec(item, spec):
                seen.add(ikey)
                matched.append(item)
                break
    return matched


async def fetch_subscriptions(api_url: str, timeout: float = 15.0) -> dict:
    """GET a subscriptions endpoint and return parsed JSON."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(api_url)
        resp.raise_for_status()
        return resp.json()