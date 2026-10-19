"""Place a single lot-based Upstox order (buy or sell).

Function signature
------------------
    place_order(
        instrument_key,          # required, e.g. "NSE_FO|43919"
        lots=1,                  # number of lots (default 1)
        transaction_type="SELL", # "BUY" | "SELL" (default SELL)
        product="D",             # "D" (intraday) | "I" (delivery)
        order_type="MARKET",     # "MARKET" | "LIMIT" | "SL" | "SL-M"
        price=0.0,
        trigger_price=0.0,
        validity="DAY",
        tag="",
        is_amo=False,
        slice=False,
    ) -> dict

Behaviour
---------
* Reads the access token from ``services.token_service`` — never pastes
  a token in code, argv, env, or a file.
* Converts ``lots`` to ``quantity`` using ``upstox_services.lot_sizes``
  (NIFTY 65, SENSEX 20, BANKNIFTY 30, FINNIFTY 60, MIDCPNIFTY 120).
  When the underlying cannot be resolved, falls back to qty = lots.
* Checks ``order_runtime_service.is_enabled()`` first and returns
  ``{"status": "disabled"}`` when orders are toggled off.
* Never raises for expected failure modes — always returns a dict.
* Masks the token in every log line.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import upstox_client
from upstox_client.rest import ApiException

from core.logger import get_logger
from services.instrument_registry import instrument_registry
from services.normalization import normalize_underlying
from services.order_runtime_service import order_runtime_service
from services.token_service import token_service
from upstox_services.lot_sizes import lots_to_quantity

logger = get_logger(__file__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask_token(token: str) -> str:
    token = str(token or "").strip()
    if len(token) <= 20:
        return "***masked***"
    return f"{token[:6]}…{token[-4:]}"


def _build_client(access_token: str) -> "upstox_client.ApiClient":
    configuration = upstox_client.Configuration()
    configuration.access_token = access_token
    return upstox_client.ApiClient(configuration)


def _resolve_underlying(instrument_key: str) -> str | None:
    """Look up the underlying for an instrument key from the registry."""
    try:
        entry = instrument_registry.get(instrument_key)
        if entry:
            return entry.get("underlying")
    except Exception:
        logger.exception("[place-order] registry lookup failed for %s", instrument_key)
    return None


def _summarise_api_response(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"raw": None}
    if isinstance(raw, dict):
        return raw
    if hasattr(raw, "to_dict"):
        try:
            return raw.to_dict()
        except Exception:
            pass
    try:
        return dict(raw)
    except Exception:
        return {"raw": str(raw)}


def place_order(
    instrument_key: str,
    *,
    lots: int = 1,
    transaction_type: str = "SELL",
    product: str = "D",
    order_type: str = "MARKET",
    price: float = 0.0,
    trigger_price: float = 0.0,
    validity: str = "DAY",
    tag: str = "",
    is_amo: bool = False,
    slice: bool = False,
) -> dict[str, Any]:
    """Place a lot-based Upstox order.

    Returns a dict with ``status`` in
    ``{"ok", "disabled", "no_token", "invalid_input", "error"}``.
    """
    called_at = _now_iso()
    key = str(instrument_key or "").strip()
    txn = str(transaction_type or "SELL").upper()
    if txn not in {"BUY", "SELL"}:
        return {
            "status": "invalid_input",
            "error": "transaction_type must be BUY or SELL",
            "called_at": called_at,
        }
    if not key:
        return {
            "status": "invalid_input",
            "error": "instrument_key is required",
            "called_at": called_at,
        }

    # ---- 0. Runtime toggle ----------------------------------------- #
    if not order_runtime_service.is_enabled():
        logger.warning(
            "[place-order] disabled by runtime toggle (key=%s txn=%s)",
            key, txn,
        )
        return {
            "status": "disabled",
            "error": "orders_disabled",
            "instrument_key": key,
            "transaction_type": txn,
            "called_at": called_at,
        }

    # ---- 1. Token -------------------------------------------------- #
    try:
        access_token = token_service.get_access_token()
    except Exception as exc:
        logger.exception("[place-order] token lookup raised")
        return {
            "status": "error",
            "error": f"token_lookup_failed: {type(exc).__name__}: {exc}",
            "instrument_key": key,
            "called_at": called_at,
        }

    if not access_token:
        return {
            "status": "no_token",
            "error": "access_token_missing",
            "instrument_key": key,
            "called_at": called_at,
        }

    token_masked = _mask_token(access_token)

    # ---- 2. Lots -> quantity -------------------------------------- #
    underlying = _resolve_underlying(key)
    quantity, lot_size, lot_source = lots_to_quantity(
        underlying=underlying,
        lots=lots,
        instrument_key=key,
    )

    logger.info(
        "[place-order] key=%s txn=%s lots=%s qty=%s lot_size=%s "
        "lot_source=%s underlying=%s token=%s",
        key, txn, lots, quantity, lot_size, lot_source,
        underlying, token_masked,
    )

    # ---- 3. Build the request ------------------------------------- #
    try:
        body = upstox_client.PlaceOrderV3Request(
            quantity=int(quantity),
            product=str(product or "D"),
            validity=str(validity or "DAY"),
            price=float(price or 0.0),
            tag=str(tag or ""),
            instrument_token=key,
            order_type=str(order_type or "MARKET"),
            transaction_type=txn,
            disclosed_quantity=0,
            trigger_price=float(trigger_price or 0.0),
            is_amo=bool(is_amo),
            slice=bool(slice),
        )
    except Exception as exc:
        logger.exception("[place-order] PlaceOrderV3Request build failed")
        return {
            "status": "error",
            "error": f"request_build_failed: {type(exc).__name__}: {exc}",
            "instrument_key": key,
            "called_at": called_at,
        }

    # ---- 4. Call -------------------------------------------------- #
    try:
        api_client = _build_client(access_token)
        order_api = upstox_client.OrderApiV3(api_client)
        raw_response = order_api.place_order(body)
    except ApiException as exc:
        http_status = getattr(exc, "status", None)
        body_preview = str(getattr(exc, "body", None) or exc)[:300]
        logger.warning(
            "[place-order] ApiException (HTTP %s): %s",
            http_status, body_preview,
        )
        return {
            "status": "error",
            "error": f"Upstox ApiException (HTTP {http_status}): {body_preview}",
            "http_status": http_status,
            "instrument_key": key,
            "transaction_type": txn,
            "quantity": quantity,
            "lot_size": lot_size,
            "called_at": called_at,
        }
    except Exception as exc:
        logger.exception("[place-order] unexpected error")
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "instrument_key": key,
            "transaction_type": txn,
            "called_at": called_at,
        }

    logger.info(
        "[place-order] OK — key=%s txn=%s qty=%s lot_size=%s",
        key, txn, quantity, lot_size,
    )
    return {
        "status": "ok",
        "instrument_key": key,
        "underlying": underlying,
        "transaction_type": txn,
        "lots": int(lots),
        "lot_size": lot_size,
        "lot_source": lot_source,
        "quantity": quantity,
        "order_type": str(order_type or "MARKET"),
        "product": str(product or "D"),
        "response": _summarise_api_response(raw_response),
        "error": None,
        "token_masked": token_masked,
        "called_at": called_at,
    }