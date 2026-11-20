"""Upstox v3 multi-leg GTT placement for the configured Upstox account.

GTT placement does not close or cancel unrelated existing positions. The
broker-side GTT manages only its own entry, target and stop-loss lifecycle.
"""
from __future__ import annotations

from typing import Any

import requests

from core.config import TRADING_ENABLED, UPSTOX_SANDBOX, UPSTOX_HTTP_TIMEOUT_SECONDS
from core.logger import get_logger
from core.token_service import fetch_access_token

logger = get_logger(__file__)
GTT_PLACE_URL = "https://api.upstox.com/v3/order/gtt/place"
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

def _close_all_existing_positions() -> dict[str, Any]:
    """Hard-close every open position via the global exit-all service.

    This is the broker-native ``exit_positions`` call used by
    ``OrderService.exit_all_positions`` (no order IDs required). It is run
    unconditionally before a new GTT placement — "nothing to close" is a
    normal, non-fatal outcome.

    Import is lazy to avoid any module-load-order coupling with
    ``services.order_service``.

    Raises:
        GTTServiceError: if the square-off request fails, so the caller can
            abort GTT placement instead of stacking a new order on an
            un-closed book.
    """
    try:
        from services.order_service import OrderService
    except Exception as exc:  # pragma: no cover - defensive import guard
        logger.exception("Could not import OrderService for pre-GTT square-off")
        raise GTTServiceError(
            "Could not load the order service required to close existing "
            "positions before placing a GTT.",
            status_code=500,
        ) from exc

    try:
        result = OrderService().exit_all_positions()
    except Exception as exc:
        logger.exception("Pre-GTT global square-off failed; aborting GTT placement")
        raise GTTServiceError(
            "Existing positions could not be closed, so the new GTT was not "
            "placed. Resolve the square-off failure and retry.",
            status_code=502,
            payload={"square_off_error": f"{type(exc).__name__}: {exc}"},
        ) from exc

    order_ids = list(result.get("order_ids") or [])
    logger.info(
        "Pre-GTT global square-off submitted: status=%s exit_order_ids=%s",
        result.get("status"),
        order_ids or "none-returned",
    )
    return result

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
    cancel_existing: bool = False,
) -> dict[str, Any]:
    """Place a multi-leg BUY GTT.

    Example: place_gtt_order("NSE_FO|...", None, 150, 130)
    means one lot, ENTRY at current LTP, TARGET 150 and STOPLOSS 130.

    Existing positions are left untouched. GTT placement must not call the
    global exit-all API because that could close unrelated live trades.
    """
    if UPSTOX_SANDBOX:
        raise GTTServiceError(
            "GTT placement uses the Upstox live v3 endpoint. Set UPSTOX_SANDBOX=false "
            "and use a live access token before enabling this feature.",
            status_code=409,
        )
    if not TRADING_ENABLED:
        raise GTTServiceError(
            "Trading is disabled. Set TRADING_ENABLED=true only after verifying the live account.",
            status_code=403,
        )
    if target is None or stoploss is None:
        raise GTTServiceError("Both target and stoploss prices are mandatory.")
    if lot < 1:
        raise GTTServiceError("lot must be at least 1.")
    if transaction_type != "BUY":
        raise GTTServiceError("This application currently supports BUY-entry GTT orders only.")

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
    }
    return result