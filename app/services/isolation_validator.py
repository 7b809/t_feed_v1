# app/services/isolation_validator.py
from __future__ import annotations

import re
from typing import Any, Dict, List

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_REQUIRED_FIELDS = (
    "instrument_key",
    "trade_date_ist",
    "strike",
    "option_type",
    "expiry",
    "bias",
    "status",
)


def validate_isolation_payload(payload: Any) -> List[str]:
    """Return a list of human-readable validation errors.

    Empty list means valid. This mirrors §3 of the spec exactly.
    """
    errors: List[str] = []

    if not isinstance(payload, dict):
        return ["payload must be a JSON object"]

    # Presence of top-level required fields
    for field in _REQUIRED_FIELDS:
        if field not in payload or payload.get(field) in (None, ""):
            errors.append(f"missing required field: {field}")

    ikey = payload.get("instrument_key")
    if ikey is not None and (not isinstance(ikey, str) or not ikey.strip()):
        errors.append("instrument_key must be a non-empty string")

    tdate = payload.get("trade_date_ist")
    if tdate is not None:
        if not isinstance(tdate, str) or not _DATE_RE.match(tdate):
            errors.append("trade_date_ist must match YYYY-MM-DD")

    bias = payload.get("bias")
    if bias is not None and bias not in ("CE", "PE"):
        errors.append("bias must be one of CE, PE")

    status = payload.get("status")
    if status is not None and not isinstance(status, str):
        errors.append("status must be a string")

    # opposite_leg validation
    opp = payload.get("opposite_leg")
    if not isinstance(opp, dict):
        errors.append("opposite_leg is required and must be an object")
    else:
        opp_key = opp.get("instrument_key")
        if not isinstance(opp_key, str) or not opp_key.strip():
            errors.append("opposite_leg.instrument_key must be a non-empty string")
        elif isinstance(ikey, str) and ikey.strip() and ikey == opp_key:
            errors.append(
                "instrument_key must differ from opposite_leg.instrument_key"
            )

    return errors