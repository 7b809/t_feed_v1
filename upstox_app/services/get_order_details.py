# upstox_services/get_order_details.py
"""Fetch a single Upstox order's fill state by broker order id.

Used by:
    services/sandbox_execution_service.py::_wait_for_fill()
to confirm that a MARKET entry actually filled before the ledger entry moves
from PENDING_ENTRY to ENTERED, and to capture the real fill timestamp and
average price that the latency / slippage instrumentation records.

Why this file exists at all:
    place_order() returning status=="ok" only means "the broker accepted the
    request". It does NOT prove a fill, does NOT give the fill price, and does
    NOT give the fill time. A MARKET order can come back ok and still end up
    rejected or unfilled moments later. The sandbox executor must not mark an
    entry as ENTERED until it has seen filled_quantity > 0 here.

Contract (returned dict — always returned, never raises):
    {
        # Broker-side order state, lowercased.
        # The caller treats {complete, filled, traded} as success,
        # {rejected, cancelled, canceled, failed} as terminal failure,
        # anything else (open / partially_filled / trigger_pending / "")
        # as "keep polling".
        "status": "complete" | "open" | "partially_filled" | "rejected"
                  | "cancelled" | "failed" | "trigger_pending" | "",

        # Fill accounting.
        "filled_quantity": int,
        "pending_quantity": int,
        "average_price": float,

        # API call outcome — kept separate from broker order state so callers
        # can distinguish "auth expired" from "order rejected".
        #   ok            -> call succeeded, "status" is meaningful
        #   no_token      -> 401 / no token available (caller should pause)
        #   invalid_input -> 404 / empty order id
        #   error         -> network / 5xx / unparsable payload
        "api_status": "ok" | "no_token" | "invalid_input" | "error",
        "api_error": str | None,

        # Raw Upstox response body, for audit / postmortem.
        "raw": dict,
    }

Token acquisition order (first that yields a non-empty string wins):
    1. A shared token service — probed at:
         services.token_service.get_access_token
         services.upstox_auth_service.get_access_token
         core.upstox_auth.get_access_token
         upstox_services.auth.get_access_token
       (adjust the probe list once the exact module is confirmed; this file
        intentionally tries several so it does not hard-couple to one layout).
    2. Environment variables UPSTOX_ACCESS_TOKEN, then UPSTOX_TOKEN.
    3. A token file at UPSTOX_TOKEN_FILE (default data/upstox_token.txt).

If none is available the function returns api_status="no_token" with a zero
filled_quantity. The caller (sandbox_execution_service._handle_entry) is
expected to translate that into a pause + alert.

Note for the next session:
    _wait_for_fill() in services/sandbox_execution_service.py currently only
    inspects "status" and "filled_quantity" from this dict. When we revisit
    that file, add a check on api_status=="no_token" inside _wait_for_fill so
    an expired token mid-wait triggers _pause("token_expired") instead of
    silently timing out the entry.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

UPSTOX_BASE_URL   = os.getenv("UPSTOX_BASE_URL", "https://api.upstox.com").rstrip("/")
ORDER_DETAILS_PATH = "/v2/order/details"
UPSTOX_TOKEN_FILE = os.getenv("UPSTOX_TOKEN_FILE", "data/upstox_token.txt")
DEFAULT_TIMEOUT_S = 8.0


# ---------------------------------------------------------------------------
# Token acquisition
# ---------------------------------------------------------------------------

# Candidate accessors, tried in order. Keep this list short — if the project
# standardises on one module later, prune the rest.
_TOKEN_SERVICE_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("services.token_service", "get_access_token"),
    ("services.upstox_auth_service", "get_access_token"),
    ("core.upstox_auth", "get_access_token"),
    ("upstox_services.auth", "get_access_token"),
)


def _get_access_token() -> Optional[str]:
    # 1. Shared token service.
    for mod_path, attr in _TOKEN_SERVICE_CANDIDATES:
        try:
            mod = __import__(mod_path, fromlist=[attr])
            fn = getattr(mod, attr, None)
            if callable(fn):
                tok = fn()
                if tok and str(tok).strip():
                    return str(tok).strip()
        except Exception:
            continue

    # 2. Environment.
    for name in ("UPSTOX_ACCESS_TOKEN", "UPSTOX_TOKEN"):
        v = os.getenv(name)
        if v and v.strip():
            return v.strip()

    # 3. Token file.
    try:
        if os.path.exists(UPSTOX_TOKEN_FILE):
            with open(UPSTOX_TOKEN_FILE, "r", encoding="utf-8") as f:
                tok = f.read().strip()
            if tok:
                return tok
    except Exception as e:
        logger.debug(f"[get_order_details] token file read failed: {e}")

    return None


# ---------------------------------------------------------------------------
# Status normalisation
# ---------------------------------------------------------------------------

# Map the various spellings Upstox emits across order types into a small
# canonical set so the caller's set-membership checks stay stable.
_STATUS_MAP = {
    "complete":         "complete",
    "completed":        "complete",
    "filled":           "complete",
    "traded":           "complete",
    "open":             "open",
    "pending":          "open",
    "trigger_pending":  "trigger_pending",
    "triggered":        "trigger_pending",
    "partially_filled": "partially_filled",
    "partial":          "partially_filled",
    "rejected":         "rejected",
    "cancelled":        "cancelled",
    "canceled":         "cancelled",
    "failed":           "failed",
}


def _norm_status(s: Any) -> str:
    if s is None:
        return ""
    key = str(s).strip().lower().replace(" ", "_")
    return _STATUS_MAP.get(key, key)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def get_order_details(broker_order_id: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """Fetch the current fill state of a single order.

    Always returns a dict matching the module docstring contract — never raises.
    Callers decide what "terminal" means (see sandbox_execution_service).
    """
    result: dict = {
        "status":            "",
        "filled_quantity":   0,
        "pending_quantity":  0,
        "average_price":     0.0,
        "api_status":        "error",
        "api_error":         None,
        "raw":               {},
    }

    oid = str(broker_order_id or "").strip()
    if not oid:
        result["api_status"] = "invalid_input"
        result["api_error"]  = "empty_order_id"
        return result

    token = _get_access_token()
    if not token:
        result["api_status"] = "no_token"
        result["api_error"]  = "access_token_unavailable"
        logger.warning(f"[get_order_details] no access token available for order {oid}")
        return result

    url = f"{UPSTOX_BASE_URL}{ORDER_DETAILS_PATH}"
    headers = {
        "Accept":        "application/json",
        "Authorization": f"Bearer {token}",
    }
    params = {"order_id": oid}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=timeout_s)
    except requests.exceptions.Timeout as e:
        result["api_status"] = "error"
        result["api_error"]  = f"timeout:{e}"
        logger.warning(f"[get_order_details] timeout for {oid}: {e}")
        return result
    except requests.exceptions.RequestException as e:
        result["api_status"] = "error"
        result["api_error"]  = f"request_error:{e}"
        logger.warning(f"[get_order_details] request error for {oid}: {e}")
        return result

    # ---- HTTP-level classification ------------------------------------
    if resp.status_code == 401:
        result["api_status"] = "no_token"
        result["api_error"]  = "http_401_unauthorized"
        logger.warning(f"[get_order_details] 401 for {oid} — token expired/invalid")
        return result

    if resp.status_code == 404:
        result["api_status"] = "invalid_input"
        result["api_error"]  = "http_404_order_not_found"
        logger.warning(f"[get_order_details] 404 for {oid} — order not found")
        return result

    if resp.status_code >= 500:
        result["api_status"] = "error"
        result["api_error"]  = f"http_{resp.status_code}"
        logger.warning(f"[get_order_details] {resp.status_code} for {oid}")
        return result

    if resp.status_code != 200:
        result["api_status"] = "error"
        result["api_error"]  = f"http_{resp.status_code}:{resp.text[:200]}"
        logger.warning(f"[get_order_details] unexpected {resp.status_code} for {oid}")
        return result

    # ---- Parse body ---------------------------------------------------
    try:
        payload = resp.json()
    except Exception as e:
        result["api_status"] = "error"
        result["api_error"]  = f"json_decode:{e}"
        logger.warning(f"[get_order_details] bad JSON for {oid}: {e}")
        return result

    result["raw"] = payload if isinstance(payload, dict) else {"value": payload}

    if not isinstance(payload, dict):
        result["api_status"] = "error"
        result["api_error"]  = "payload_not_dict"
        return result

    api_status = str(payload.get("status") or "").lower()
    if api_status and api_status not in {"success", "ok"}:
        result["api_status"] = "error"
        result["api_error"]  = f"upstox_status:{api_status}"
        return result

    data = payload.get("data")
    # Upstox occasionally wraps a single record in a list; tolerate both.
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        result["api_status"] = "error"
        result["api_error"]  = "missing_data"
        return result

    # ---- Normalise fill fields ----------------------------------------
    result["api_status"] = "ok"
    result["status"]     = _norm_status(data.get("status"))

    try:
        result["filled_quantity"] = int(data.get("filled_quantity") or 0)
    except Exception:
        result["filled_quantity"] = 0
    try:
        result["pending_quantity"] = int(data.get("pending_quantity") or 0)
    except Exception:
        result["pending_quantity"] = 0
    try:
        result["average_price"] = float(data.get("average_price") or 0.0)
    except Exception:
        result["average_price"] = 0.0

    logger.debug(
        f"[get_order_details] {oid} status={result['status']} "
        f"filled={result['filled_quantity']} avg={result['average_price']}"
    )
    return result