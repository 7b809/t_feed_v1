"""Upstox v3 multi-leg GTT placement for the configured Upstox account.

GTT placement does not close or cancel unrelated existing positions. The
broker-side GTT manages only its own entry, target and stop-loss lifecycle.
"""
from __future__ import annotations

from typing import Any

import requests

from core.config import GTT_ENABLED, GTT_CANCEL_ENABLED, ORDERFLOW_CLEANUP_ENABLED, UPSTOX_SANDBOX, UPSTOX_HTTP_TIMEOUT_SECONDS
from core.logger import get_logger
from core.token_service import fetch_access_token
from repositories.order_repository import OrderRepository, now_iso
from services.daily_profit_guard_service import assert_new_entries_allowed

logger = get_logger(__file__)
GTT_PLACE_URL = "https://api.upstox.com/v3/order/gtt/place"
GTT_DETAILS_URL = "https://api.upstox.com/v3/order/gtt"
GTT_CANCEL_URL = "https://api.upstox.com/v3/order/gtt/cancel"
LTP_URL = "https://api.upstox.com/v2/market-quote/ltp"

# Current lot-size fallbacks for commonly used index option families.
# Instrument Search metadata takes precedence whenever the frontend provides it.
LOT_SIZE_MAP = {
    # Longer overlapping names must be checked before NIFTY.
    "MIDCPNIFTY": 120,
    "BANKNIFTY": 30,
    "FINNIFTY": 60,
    "BANKEX": 15,
    "SENSEX": 20,
    "NIFTY": 65,
}

class GTTServiceError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400, payload: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}

def resolve_lot_size(
    instrument_key: str,
    supplied_lot_size: int | None = None,
    trading_symbol: str | None = None,
) -> int:
    if supplied_lot_size:
        return supplied_lot_size
    # Instrument keys are often opaque numeric IDs; use the selected
    # instrument's trading symbol for the static fallback when available.
    normalized = f"{instrument_key} {trading_symbol or ''}".upper()
    for symbol, size in LOT_SIZE_MAP.items():
        if symbol in normalized:
            return size
    raise GTTServiceError(
        "Lot size is unavailable for this instrument. Select it through Instrument Search "
        "or provide its verified lot_size metadata."
    )

def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

def get_live_ltp(instrument_key: str, token: str | None = None) -> float:
    token = token or fetch_access_token()
    try:
        response = requests.get(
            LTP_URL,
            headers=_headers(token),
            params={"instrument_key": instrument_key},
            timeout=UPSTOX_HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        logger.exception("GTT LTP request failed for %s", instrument_key)
        raise GTTServiceError("Could not retrieve the live market price.", status_code=502) from None

    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not response.ok:
        logger.warning("GTT LTP lookup failed: HTTP %s", response.status_code)
        raise GTTServiceError(
            "Upstox could not provide the live price for this instrument.",
            status_code=502,
            payload={"http_status": response.status_code, "upstox_response": payload},
        )

    data = payload.get("data") or {}
    quote = data.get(instrument_key)
    if quote is None and data:
        quote = next(iter(data.values()))
    try:
        ltp = float(quote["last_price"])
    except (TypeError, KeyError, ValueError, StopIteration):
        raise GTTServiceError("Live price was missing from the Upstox response.", status_code=502) from None
    if ltp <= 0:
        raise GTTServiceError("Upstox returned an invalid live price.", status_code=502)
    return ltp

def build_gtt_payload(
    *,
    instrument_key: str,
    entry: float | None,
    target: float,
    stoploss: float,
    quantity: int,
    product: str = "I",
    transaction_type: str = "BUY",
    ltp: float,
) -> dict[str, Any]:
    """Build a MULTIPLE ENTRY/TARGET/STOPLOSS GTT payload for a long BUY."""
    entry_price = float(entry) if entry is not None else float(ltp)
    if not (stoploss < entry_price < target):
        raise GTTServiceError(
            f"BUY GTT levels must satisfy stoploss < entry < target. "
            f"Received SL={stoploss}, entry={entry_price}, target={target}."
        )

    if entry is None or abs(entry_price - ltp) < 1e-9:
        entry_trigger_type = "IMMEDIATE"
    else:
        entry_trigger_type = "ABOVE" if entry_price > ltp else "BELOW"

    return {
        "type": "MULTIPLE",
        "quantity": quantity,
        "product": product,
        "instrument_token": instrument_key,
        "transaction_type": transaction_type,
        "rules": [
            {
                "strategy": "ENTRY",
                "trigger_type": entry_trigger_type,
                "trigger_price": round(entry_price, 2),
            },
            {
                "strategy": "TARGET",
                "trigger_type": "IMMEDIATE",
                "trigger_price": round(float(target), 2),
            },
            {
                "strategy": "STOPLOSS",
                "trigger_type": "IMMEDIATE",
                "trigger_price": round(float(stoploss), 2),
            },
        ],
    }

def place_gtt_order(
    instrument_key: str,
    entry: float | None = None,
    target: float | None = None,
    stoploss: float | None = None,
    lot: int = 1,
    *,
    lot_size: int | None = None,
    trading_symbol: str | None = None,
    product: str = "I",
    transaction_type: str = "BUY",
    source_order_id: str | None = None,
    isolation_id: str | None = None,
) -> dict[str, Any]:
    """Place a multi-leg BUY GTT.

    Example: place_gtt_order("NSE_FO|...", None, 150, 130)
    means one lot, ENTRY at current LTP, TARGET 150 and STOPLOSS 130.

    Existing positions are left untouched. GTT placement must not call the
    global exit-all API because that could close unrelated live trades.
    """
    if not GTT_ENABLED:
        raise GTTServiceError(
            "GTT placement is disabled. Set GTT_ENABLED=true to enable live GTT orders.",
            status_code=403,
        )
    if UPSTOX_SANDBOX:
        raise GTTServiceError(
            "GTT placement uses the Upstox live v3 endpoint. Set UPSTOX_SANDBOX=false "
            "and use a live access token before enabling this feature.",
            status_code=409,
        )
    if target is None or stoploss is None:
        raise GTTServiceError("Both target and stoploss prices are mandatory.")
    if lot < 1:
        raise GTTServiceError("lot must be at least 1.")
    if transaction_type != "BUY":
        raise GTTServiceError("This application currently supports BUY-entry GTT orders only.")

    # Check account-wide P&L before creating a new broker-side GTT.
    # This does not cancel or alter GTTs which were already submitted.
    assert_new_entries_allowed()

    # Deliberately do not square off existing positions. The GTT manages
    # only its own broker-side entry, target and stop-loss legs.

    resolved_lot_size = resolve_lot_size(instrument_key, lot_size, trading_symbol)
    quantity = int(lot) * resolved_lot_size
    token = fetch_access_token()
    ltp = get_live_ltp(instrument_key, token)
    payload = build_gtt_payload(
        instrument_key=instrument_key,
        entry=entry,
        target=target,
        stoploss=stoploss,
        quantity=quantity,
        product=product,
        transaction_type=transaction_type,
        ltp=ltp,
    )

    try:
        response = requests.post(
            GTT_PLACE_URL,
            headers=_headers(token),
            json=payload,
            timeout=UPSTOX_HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        logger.exception("Upstox GTT placement transport failure for %s", instrument_key)
        raise GTTServiceError("Could not reach Upstox GTT API. Check logs before retrying.", status_code=502) from None

    try:
        broker_payload = response.json()
    except ValueError:
        broker_payload = {"raw": response.text[:1000]}

    if not response.ok or broker_payload.get("status") == "error":
        logger.warning("Upstox GTT placement rejected: HTTP %s", response.status_code)
        raise GTTServiceError(
            "Upstox rejected the GTT order.",
            status_code=502 if response.status_code >= 500 else 400,
            payload={"http_status": response.status_code, "upstox_response": broker_payload},
        )

    ids = ((broker_payload.get("data") or {}).get("gtt_order_ids") or [])
    logger.info(
        "GTT order accepted: instrument=%s quantity=%s lots=%s gtt_ids=%s",
        instrument_key, quantity, lot, ids,
    )
    audit_warning = None
    for gtt_id in [str(value) for value in ids if value]:
        audit = {
            "local_id": f"GTT-{gtt_id}",
            "record_type": "GTT",
            "status": "SUBMITTED",
            "order_id": gtt_id,
            "order_ids": [gtt_id],
            "gtt_order_ids": [gtt_id],
            "instrument_token": instrument_key,
            "trading_symbol": trading_symbol,
            "quantity": quantity,
            "lot": lot,
            "lot_size": resolved_lot_size,
            "product": product,
            "transaction_type": transaction_type,
            "source_order_id": source_order_id,
            "isolation_id": isolation_id,
            "created_time": now_iso(),
            "last_modified_time": now_iso(),
            "events": [{"event_type": "GTT_SUBMITTED", "event_time": now_iso(),
                        "source_order_id": source_order_id, "isolation_id": isolation_id,
                        "broker_response": broker_payload}],
            "exit_orders_by_order_id": {},
        }
        try:
            OrderRepository().append_entry(audit, require_mongo=True)
        except Exception as exc:
            logger.exception("GTT %s was accepted but lifecycle audit persistence failed", gtt_id)
            audit_warning = f"GTT accepted but audit persistence failed: {type(exc).__name__}"

    result: dict[str, Any] = {
        "success": True,
        "instrument_key": instrument_key,
        "entry_price": payload["rules"][0]["trigger_price"],
        "entry_trigger_type": payload["rules"][0]["trigger_type"],
        "live_ltp": ltp,
        "target": target,
        "stoploss": stoploss,
        "lot": lot,
        "lot_size": resolved_lot_size,
        "quantity": quantity,
        "gtt_order_ids": ids,
        "broker_response": broker_payload,
        "request": payload,
        "existing_positions_closed": {"attempted": False},
        "source_order_id": source_order_id,
        "isolation_id": isolation_id,
        "audit_warning": audit_warning,
    }
    return result


def get_gtt_order_details(gtt_order_id: str) -> dict[str, Any]:
    """Fetch one active GTT's rules and generated order IDs from Upstox."""
    if UPSTOX_SANDBOX:
        raise GTTServiceError("GTT lifecycle operations require the live Upstox account.", status_code=409)
    if not str(gtt_order_id).startswith("GTT-"):
        raise GTTServiceError("Invalid GTT order ID.", status_code=400)
    token = fetch_access_token()
    try:
        response = requests.get(
            GTT_DETAILS_URL,
            headers=_headers(token),
            params={"gtt_order_id": gtt_order_id},
            timeout=UPSTOX_HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        logger.exception("Upstox GTT details transport failure for %s", gtt_order_id)
        raise GTTServiceError("Could not reach Upstox GTT details API; status is unknown.",
                              status_code=502) from None
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:1000]}
    if not response.ok or str(payload.get("status") or "").lower() == "error":
        raise GTTServiceError(
            "Upstox could not confirm the GTT lifecycle status.",
            status_code=502 if response.status_code >= 500 else 409,
            payload={"http_status": response.status_code, "upstox_response": payload},
        )
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise GTTServiceError(
            "No active GTT details were returned. Completed GTTs are not queryable; reconcile generated order IDs manually.",
            status_code=409,
            payload={"gtt_order_id": gtt_order_id, "upstox_response": payload},
        )
    return payload


def cancel_gtt_order(gtt_order_id: str, *, cleanup: bool = False) -> dict[str, Any]:
    """Cancel one untriggered GTT. Never infer success from a transport timeout."""
    if cleanup and not ORDERFLOW_CLEANUP_ENABLED:
        raise GTTServiceError("Orderflow GTT cleanup is disabled. Set ORDERFLOW_CLEANUP_ENABLED=true.",
                              status_code=403)
    if not cleanup and not GTT_CANCEL_ENABLED:
        raise GTTServiceError("GTT cancellation is disabled. Set GTT_CANCEL_ENABLED=true.",
                              status_code=403)
    if UPSTOX_SANDBOX:
        raise GTTServiceError("GTT cancellation requires the live Upstox account.", status_code=409)
    if not str(gtt_order_id).startswith("GTT-"):
        raise GTTServiceError("Invalid GTT order ID.", status_code=400)
    token = fetch_access_token()
    try:
        response = requests.delete(
            GTT_CANCEL_URL,
            headers=_headers(token),
            json={"gtt_order_id": gtt_order_id},
            timeout=UPSTOX_HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        logger.exception("Upstox GTT cancellation transport outcome unknown for %s", gtt_order_id)
        raise GTTServiceError("GTT cancellation outcome is unknown; reconcile before replacement.",
                              status_code=502) from None
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:1000]}
    if not response.ok or str(payload.get("status") or "").lower() == "error":
        raise GTTServiceError(
            "Upstox rejected or could not confirm GTT cancellation.",
            status_code=502 if response.status_code >= 500 else 409,
            payload={"http_status": response.status_code, "upstox_response": payload},
        )
    audit_warning = None
    try:
        repository = OrderRepository()
        entry = repository.find_order(gtt_order_id)
        if entry:
            events = list(entry.get("events") or [])
            events.append({"event_type": "GTT_CANCEL_SUBMITTED", "event_time": now_iso(),
                           "broker_response": payload})
            repository.update_entry(entry["local_id"], {
                "status": "CANCEL_SUBMITTED",
                "last_gtt_cancel_response": payload,
                "events": events,
                "last_modified_time": now_iso(),
            }, require_mongo=True)
    except Exception as exc:
        logger.exception("GTT %s cancellation was accepted but audit update failed", gtt_order_id)
        audit_warning = f"Cancellation accepted but audit update failed: {type(exc).__name__}"
    return {"success": True, "gtt_order_id": gtt_order_id, "status": "CANCEL_SUBMITTED",
            "broker_response": payload, "audit_warning": audit_warning}



def cancel_gtt_orders(gtt_order_ids: list[str]) -> dict[str, Any]:
    """Cancel multiple GTTs through the official one-ID-per-request REST endpoint."""
    ids = list(dict.fromkeys(str(value).strip() for value in gtt_order_ids if str(value).strip()))
    if not ids or len(ids) > 20:
        raise GTTServiceError("Provide between 1 and 20 GTT order IDs.", status_code=400)

    results: list[dict[str, Any]] = []
    for gtt_order_id in ids:
        try:
            results.append(cancel_gtt_order(gtt_order_id))
        except GTTServiceError as exc:
            results.append({
                "success": False,
                "gtt_order_id": gtt_order_id,
                "status": "ERROR",
                "error": str(exc),
                "broker_error": exc.payload or None,
            })
        except Exception as exc:
            logger.exception("Unexpected GTT cancellation failure for %s", gtt_order_id)
            results.append({
                "success": False,
                "gtt_order_id": gtt_order_id,
                "status": "ERROR",
                "error": f"Unexpected cancellation error: {type(exc).__name__}",
            })

    cancelled = sum(1 for item in results if item.get("success"))
    failed = len(results) - cancelled
    return {
        "success": failed == 0,
        "requested": len(ids),
        "cancelled": cancelled,
        "failed": failed,
        "results": results,
    }
