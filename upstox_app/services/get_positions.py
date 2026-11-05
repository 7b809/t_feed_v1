# upstox_services/get_positions.py
"""Fetch every open Upstox position and annotate it with lock status.

Returns the "show all positions" view: every open position with its
side, quantity, P&L, and a boolean ``locked`` flag plus the lock
metadata (if any).

Function
--------
    get_positions() -> dict

Return shape::

    {
      "status": "ok" | "no_token" | "error",
      "positions": [
        {
          "instrument_token": "NSE_FO|43919",
          "trading_symbol": "NIFTY24NOV24000CE",
          "exchange": "NSE_FO",
          "product": "D",
          "quantity": 65,             # +long / -short
          "average_price": 100.0,
          "last_price": 110.0,
          "pnl": 650.0,
          "side": "LONG" | "SHORT" | "FLAT",
          "locked": true | false,
          "lock_meta": {...} | null,
          ...                          # original fields preserved
        }
      ],
      "total": N,
      "locked_count": N,
      "unlocked_count": N,
      "called_at": "..."
    }

Token never appears in any log line or returned dict. Provider error
bodies are redacted by ``sanitise_payload`` before being returned.

Standalone CLI::

    python -m upstox_services.get_positions
    python -m upstox_services.get_positions --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any

import requests

from core.logger import get_logger
from services.token_service import token_service
from upstox_app.services._sanitise import sanitise_payload
from upstox_app.services.position_lock_service import list_locked

logger = get_logger(__file__)

_UPSTOX_BASE = "https://api.upstox.com"
_POSITIONS_PATH = "/v2/portfolio/short-term-positions"
_HTTP_TIMEOUT = 10


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _annotate(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    locked_entries = list_locked().get("locked", {})
    out: list[dict[str, Any]] = []
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        token = str(pos.get("instrument_token") or "").strip()
        try:
            qty = int(pos.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0
        side = "LONG" if qty > 0 else ("SHORT" if qty < 0 else "FLAT")
        enriched = dict(pos)
        enriched["side"] = side
        enriched["locked"] = token in locked_entries
        enriched["lock_meta"] = locked_entries.get(token) if token else None
        out.append(enriched)
    return out


def get_positions() -> dict[str, Any]:
    """Return every open position, annotated with lock status.

    Security: the access token is read once and used only as the
    ``Authorization`` header of a single ``requests.get``. It is
    never logged and never returned. Error bodies are redacted by
    ``sanitise_payload``.
    """
    called_at = _now_iso()

    # ---- 1. Token -------------------------------------------------- #
    try:
        access_token = token_service.get_access_token()
    except Exception as exc:
        logger.exception("[get-positions] token lookup raised")
        return {
            "status": "error",
            "error": f"token_lookup_failed: {type(exc).__name__}",
            "called_at": called_at,
        }

    if not access_token:
        return {
            "status": "no_token",
            "error": "access_token_missing",
            "called_at": called_at,
        }

    # ---- 2. Call -------------------------------------------------- #
    try:
        resp = requests.get(
            f"{_UPSTOX_BASE}{_POSITIONS_PATH}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning(
            "[get-positions] network error: %s", type(exc).__name__
        )
        return {
            "status": "error",
            "error": f"request_error: {type(exc).__name__}",
            "called_at": called_at,
        }

    if resp.status_code == 401:
        logger.warning("[get-positions] 401 unauthorized")
        return {
            "status": "no_token",
            "error": "http_401_unauthorized",
            "called_at": called_at,
        }

    if resp.status_code >= 400:
        preview = sanitise_payload(resp.text[:300])
        logger.warning(
            "[get-positions] HTTP %s: %s", resp.status_code, preview
        )
        return {
            "status": "error",
            "error": f"http_{resp.status_code}",
            "body": preview,
            "called_at": called_at,
        }

    try:
        payload = resp.json()
    except ValueError as exc:
        return {
            "status": "error",
            "error": f"json_decode: {exc}",
            "called_at": called_at,
        }

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return {
            "status": "error",
            "error": "missing_data_array",
            "called_at": called_at,
        }

    annotated = _annotate(data)
    locked_count = sum(1 for p in annotated if p["locked"])
    logger.info(
        "[get-positions] OK — total=%d locked=%d unlocked=%d",
        len(annotated), locked_count, len(annotated) - locked_count,
    )
    return {
        "status": "ok",
        "positions": annotated,
        "total": len(annotated),
        "locked_count": locked_count,
        "unlocked_count": len(annotated) - locked_count,
        "called_at": called_at,
    }


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Show all open Upstox positions.")
    p.add_argument(
        "--json", action="store_true",
        help="Print raw JSON instead of a human table.",
    )
    return p


def _print_table(result: dict[str, Any]) -> None:
    print(
        f"status={result.get('status')} "
        f"total={result.get('total', 0)} "
        f"locked={result.get('locked_count', 0)} "
        f"unlocked={result.get('unlocked_count', 0)}"
    )
    header = (
        f"{'instrument_token':<22} {'side':<6} "
        f"{'qty':>7} {'pnl':>10} {'lock':<6}"
    )
    print(header)
    print("-" * len(header))
    for p in result.get("positions", []):
        lock_flag = "YES" if p.get("locked") else "-"
        pnl = p.get("pnl")
        pnl_s = f"{float(pnl):.2f}" if isinstance(pnl, (int, float)) else "-"
        print(
            f"{str(p.get('instrument_token') or '')[:22]:<22} "
            f"{str(p.get('side') or '')[:6]:<6} "
            f"{int(p.get('quantity') or 0):>7} "
            f"{pnl_s:>10} "
            f"{lock_flag:<6}"
        )


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    result = get_positions()
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_table(result)
    if result.get("status") == "ok":
        return 0
    return 2 if result.get("status") == "no_token" else 1


if __name__ == "__main__":
    sys.exit(main())