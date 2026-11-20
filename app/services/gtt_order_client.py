"""Server-to-server GTT submission for newly created OrderFlow entries.

The shared secret is read from Settings and is never logged or persisted.
The ENTRY property is intentionally omitted: the receiving service resolves
its own live LTP and creates the immediate-entry GTT rule.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from ..config import Settings

IST = ZoneInfo("Asia/Kolkata")


class GTTOrderSubmissionError(RuntimeError):
    def __init__(self, message: str, *, outcome_unknown: bool = False,
                 http_status: int | None = None) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown
        self.http_status = http_status


def submit_gtt_order(config: Settings, order: dict[str, Any]) -> dict[str, Any]:
    """Submit calculated risk levels. Never send entry and never auto-retry."""
    if not config.gtt_orders_enabled:
        raise GTTOrderSubmissionError("GTT submission is disabled.")

    instrument_key = str(
        order.get("target_instrument_key") or order.get("instrument_key") or ""
    ).strip()
    trading_symbol = str(order.get("trading_symbol") or "").strip()
    try:
        target = float(order.get("target_price"))
        stoploss = float(order.get("stop_loss_price"))
        lot = int(order.get("quantity"))
    except (TypeError, ValueError):
        raise GTTOrderSubmissionError(
            "Calculated target, stop-loss or lot quantity is invalid."
        ) from None

    if not instrument_key or target <= stoploss or lot < 1:
        raise GTTOrderSubmissionError(
            "Instrument, calculated GTT levels or lot quantity is invalid."
        )

    payload: dict[str, Any] = {
        "instrument_key": instrument_key,
        "target": round(target, 2),
        "stoploss": round(stoploss, 2),
        "lot": lot,
        "product": config.gtt_product,
        "transaction_type": "BUY",
        "api_token": config.gtt_api_token,
        "source_order_id": str(order.get("order_id") or ""),
        "isolation_id": str(order.get("isolation_id") or ""),
    }
    if trading_symbol:
        payload["trading_symbol"] = trading_symbol
    lot_size = order.get("instrument_lot_size")
    if lot_size not in (None, "", 0, "0"):
        payload["lot_size"] = int(lot_size)

    # Deliberately do not include an "entry" key.
    try:
        response = requests.post(
            config.gtt_api_url,
            json=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=config.gtt_request_timeout_seconds,
        )
    except requests.RequestException:
        raise GTTOrderSubmissionError(
            "GTT API transport outcome is unknown; inspect the receiving service before retrying.",
            outcome_unknown=True,
        ) from None

    try:
        body = response.json()
    except ValueError:
        body = {}

    api_error = (
        not response.ok
        or (isinstance(body, dict) and str(body.get("status") or "").lower() == "error")
        or (isinstance(body, dict) and body.get("success") is False)
    )
    if api_error:
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, dict):
            detail = detail.get("error") or detail.get("detail")
        message = str(detail or "Receiving GTT service rejected the request.")[:500]
        raise GTTOrderSubmissionError(
            message, outcome_unknown=response.status_code >= 500,
            http_status=response.status_code,
        )

    data = body.get("data") if isinstance(body, dict) else {}
    ids = (data.get("gtt_order_ids") or []) if isinstance(data, dict) else []
    if not ids and isinstance(body, dict):
        ids = body.get("gtt_order_ids") or []

    return {
        "status": "SUBMITTED",
        "submitted_at": datetime.now(IST).isoformat(),
        "http_status": response.status_code,
        "gtt_order_ids": [str(item) for item in ids],
        "instrument_key": instrument_key,
        "target": round(target, 2),
        "stoploss": round(stoploss, 2),
        "lot": lot,
        "lot_size": int(lot_size) if lot_size not in (None, "", 0, "0") else None,
        "product": config.gtt_product,
        "entry_sent": False,
    }



def cleanup_gtt_orders(config: Settings, gtt_order_ids: list[str], *,
                       isolation_id: str, source_order_id: str | None = None) -> dict[str, Any]:
    """Ask sandbox_v1 to reconcile and remove only the supplied Orderflow GTTs.

    A timeout or malformed response is treated as an unknown cleanup outcome.
    The caller must block replacement GTT submission in that case.
    """
    ids = list(dict.fromkeys(str(value).strip() for value in gtt_order_ids if str(value).strip()))
    if not ids:
        return {"success": True, "cleanup_complete": True, "results": [], "skipped": "NO_PRIOR_GTT"}
    if not config.gtt_orders_enabled:
        raise GTTOrderSubmissionError("GTT cleanup cannot run while GTT integration is disabled.")
    payload = {
        "gtt_order_ids": ids,
        "isolation_id": isolation_id,
        "source_order_id": source_order_id,
        "api_token": config.gtt_api_token,
    }
    try:
        response = requests.post(
            config.gtt_cleanup_url,
            json=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=config.gtt_request_timeout_seconds,
        )
    except requests.RequestException:
        raise GTTOrderSubmissionError(
            "Orderflow broker cleanup transport outcome is unknown; replacement GTT is blocked.",
            outcome_unknown=True,
        ) from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not response.ok or not isinstance(body, dict) or body.get("cleanup_complete") is not True:
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, dict):
            detail = detail.get("error") or detail
        raise GTTOrderSubmissionError(
            f"Orderflow broker cleanup was not confirmed: {str(detail or 'invalid response')[:500]}",
            outcome_unknown=response.status_code >= 500,
            http_status=response.status_code,
        )
    return {
        "status": "CLEANUP_CONFIRMED",
        "cleanup_complete": True,
        "requested_at": datetime.now(IST).isoformat(),
        "http_status": response.status_code,
        "gtt_order_ids": ids,
        "broker_response": body,
    }
