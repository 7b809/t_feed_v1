# File: upstox_services/place_order.py
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

Security guarantees (2026-10-22 token-leak fix)
-----------------------------------------------
The project's security policy is absolute: **no access token — masked
or otherwise — appears in any log line or returned dict.** The
previous revision violated this in two places:

  * the pre-call ``logger.info`` line included ``token=%s`` with the
    masked token (``eyJ0eX…p9X0``), which is still sensitive because
    it reveals the token prefix and suffix;
  * the ``ok`` return dict carried a ``token_masked`` field which
    could be persisted by callers (routes, audit trail, Mongo doc,
    etc.).

This revision removes both:

  * the pre-call log line no longer accepts or emits a token; it
    logs ``key / txn / lots / qty / lot_size / lot_source /
    underlying`` only;
  * the ``ok`` return dict no longer carries ``token_masked``;
  * the token is read once from ``token_service``, held in a local
    variable, and passed only to ``upstox_client.Configuration()``;
  * Upstox ``ApiException`` bodies and verbose SDK responses are
    passed through ``upstox_services._sanitise.sanitise_payload``
    before being logged or returned, so a provider-side error body
    cannot leak an ``Authorization`` header or a bearer token.

The ``_mask_token`` helper is kept as a module-level function for
backward compatibility with any external import, but it is no
longer called from ``place_order`` — the function no longer has a
token to mask.
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
from upstox_services._sanitise import sanitise_payload
from upstox_services.lot_sizes import lots_to_quantity

logger = get_logger(__file__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask_token(token: str) -> str:
    """Return a masked token for callers that explicitly want one.

    NOTE: ``place_order`` does **not** use this helper and does not
    emit a masked token anywhere. It is kept here only for backward
    compatibility with external imports that may still call it.
    """
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
        logger.exception(
            "[place-order] registry lookup failed for %s", instrument_key
        )
    return None


def _summarise_api_response(raw: Any) -> dict[str, Any]:
    """Return a sanitised, JSON-serialisable summary of the Upstox response.

    Anything the SDK returns — dict, object with ``to_dict()``, or an
    opaque string — is passed through ``sanitise_payload`` so a
    provider body that happens to embed an ``Authorization`` header
    or a bearer token cannot leak through the return dict.
    """
    if raw is None:
        return {"raw": None}
    if isinstance(raw, dict):
        return sanitise_payload(raw)
    if hasattr(raw, "to_dict"):
        try:
            return sanitise_payload(raw.to_dict())
        except Exception:
            pass
    try:
        return sanitise_payload(dict(raw))
    except Exception:
        return {"raw": sanitise_payload(str(raw))}


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

    Security: the access token never appears in any log line or in
    the returned dict. Provider error bodies are redacted by
    ``sanitise_payload`` before they leave this function.
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
    # Read once. Held only in this local variable for the duration of
    # the call. Never logged, never returned, never interpolated into
    # an error string.
    try:
        access_token = token_service.get_access_token()
    except Exception as exc:
        logger.exception("[place-order] token lookup raised")
        return {
            "status": "error",
            "error": f"token_lookup_failed: {type(exc).__name__}",
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

    # ---- 2. Lots -> quantity -------------------------------------- #
    underlying = _resolve_underlying(key)
    quantity, lot_size, lot_source = lots_to_quantity(
        underlying=underlying,
        lots=lots,
        instrument_key=key,
    )

    # SECURITY: no token in this log line. The previous revision
    # included ``token=<masked>``; that violated the project's
    # "no token in any log line" policy even in masked form.
    logger.info(
        "[place-order] key=%s txn=%s lots=%s qty=%s lot_size=%s "
        "lot_source=%s underlying=%s",
        key, txn, lots, quantity, lot_size, lot_source, underlying,
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
        # Redact any credential-like run in the provider body before
        # it reaches a log line or the return dict.
        body_preview = sanitise_payload(
            str(getattr(exc, "body", None) or exc)
        )[:300]
        logger.warning(
            "[place-order] ApiException (HTTP %s): %s",
            http_status, body_preview,
        )
        return {
            "status": "error",
            "error": (
                f"Upstox ApiException (HTTP {http_status}): "
                f"{body_preview}"
            ),
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
    # SECURITY: no ``token_masked`` field in the returned dict. The
    # previous revision included one; callers could persist it in a
    # Mongo doc, an audit trail, or an API response.
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
        "called_at": called_at,
    }