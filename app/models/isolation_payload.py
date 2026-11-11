# app/models/isolation_payload.py
"""Typed helpers for the incoming isolation document.

The isolation document is deeply nested (`explanation` alone has 9 sections).
We intentionally do NOT model it with Pydantic — app1 evolves the shape and
we want to persist the payload verbatim. Instead this module exposes a small
set of read-only helpers used by the validator and the subscriber.
"""
from __future__ import annotations

from typing import Any, Dict


def is_active(payload: Dict[str, Any]) -> bool:
    return payload.get("status") == "ACTIVE"


def isolated_instrument_key(payload: Dict[str, Any]) -> str:
    return payload.get("instrument_key", "") or ""


def opposite_instrument_key(payload: Dict[str, Any]) -> str:
    opp = payload.get("opposite_leg") or {}
    if not isinstance(opp, dict):
        return ""
    return opp.get("instrument_key", "") or ""


def legs(payload: Dict[str, Any]) -> Dict[str, str]:
    """Return ``{'isolated': key, 'opposite': key}``.

    Callers must ensure ``is_active(payload)`` before using the result.
    """
    return {
        "isolated": isolated_instrument_key(payload),
        "opposite": opposite_instrument_key(payload),
    }