# upstox_services/place_order.py
"""Place a single lot-based Upstox order (buy or sell).

Function signature
------------------
    place_order(
        instrument_key,          # required, e.g. "NSE_FO|43919"
        lots=1,                  # number of lots (default 1)
        transaction_type="BUY",  # "BUY" | "SELL" (default BUY)
        product="D",             # "D" (intraday) | "I" (delivery) | "MTF"
        order_type="MARKET",     # "MARKET" | "LIMIT" | "SL" | "SL-M"
        price=0.0,
        trigger_price=0.0,
        validity="DAY",          # "DAY" | "IOC"
        tag="",                  # omitted when empty
        is_amo=False,
        slice=False,
        market_protection=None,  # optional float percentage
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
* Sends two Telegram notifications (HTML formatted):
    - "Order placing started" right before the Upstox call
    - "Order placed / Order failed" with the response or error preview

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

Telegram notifications
----------------------
Two HTML-formatted Telegram messages are sent around the Upstox call:
  * "Order placing started"
  * "Order placed" (with sanitised response) or "Order failed"
    (with sanitised error preview)

Telegram failures are swallowed — they never affect the order result.

The ``_mask_token`` helper is kept as a module-level function for
backward compatibility with any external import, but it is no
longer called from ``place_order`` — the function no longer has a
token to mask.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any

import upstox_client
from upstox_client.rest import ApiException

from core.logger import get_logger
from services.instrument_registry import instrument_registry
from services.normalization import normalize_underlying
from services.order_runtime_service import order_runtime_service
from services.telegram_service import telegram_service
from services.token_service import token_service
from upstox_services._sanitise import sanitise_payload
from upstox_services.lot_sizes import lots_to_quantity

logger = get_logger(__file__)


# Allowed values mirror the generated ``PlaceOrderV3Request`` model so we
# fail fast with a clear error instead of relying on the SDK's ValueError.
_ALLOWED_PRODUCTS = {"I", "D", "MTF"}
_ALLOWED_VALIDITY = {"DAY", "IOC"}
_ALLOWED_ORDER_TYPES = {"MARKET", "LIMIT", "SL", "SL-M"}
_ALLOWED_TXN_TYPES = {"BUY", "SELL"}


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


# ---------------------------------------------------------------------------
# Telegram helpers — all sends are best-effort and never raise.
# ---------------------------------------------------------------------------
def _tg_escape(value: Any) -> str:
    """HTML-escape a value for Telegram's HTML parse_mode."""
    return html.escape(str(value if value is not None else ""), quote=False)


def _format_response_for_tg(response_summary: dict[str, Any]) -> str:
    """Render the sanitised Upstox response as Telegram HTML lines."""
    if not response_summary:
        return "<i>(empty response)</i>"

    lines: list[str] = []
    for k, v in response_summary.items():
        if isinstance(v, dict):
            inner = ", ".join(
                f"{_tg_escape(ik)}={_tg_escape(iv)}" for ik, iv in v.items()
            )
            lines.append(f"• <b>{_tg_escape(k)}</b>: {{{inner}}}")
        elif isinstance(v, (list, tuple)):
            inner = ", ".join(_tg_escape(x) for x in v)
            lines.append(f"• <b>{_tg_escape(k)}</b>: [{inner}]")
        else:
            lines.append(f"• <b>{_tg_escape(k)}</b>: {_tg_escape(v)}")
    return "\n".join(lines) if lines else "<i>(empty response)</i>"


def _send_order_started(
    *,
    key: str,
    txn: str,
    lots: int,
    quantity: int,
    lot_size: Any,
    lot_source: str,
    underlying: str | None,
    product: str,
    order_type: str,
) -> None:
    """Send the 'order placing started' Telegram notification (HTML)."""
    try:
        title = "⏳ Order placing started"
        body = (
            f"<b>Instrument</b>: <code>{_tg_escape(key)}</code>\n"
            f"<b>Underlying</b>: {_tg_escape(underlying or '-')}\n"
            f"<b>Side</b>: {_tg_escape(txn)}\n"
            f"<b>Product</b>: {_tg_escape(product)}\n"
            f"<b>Order type</b>: {_tg_escape(order_type)}\n"
            f"<b>Lots</b>: {_tg_escape(lots)}\n"
            f"<b>Lot size</b>: {_tg_escape(lot_size)} "
            f"(<i>{_tg_escape(lot_source)}</i>)\n"
            f"<b>Quantity</b>: {_tg_escape(quantity)}"
        )
        telegram_service.send_message(title, body, level="INFO")
    except Exception:
        logger.exception("[place-order] telegram 'started' notify failed")


def _send_order_completed(
    *,
    key: str,
    txn: str,
    quantity: int,
    response_summary: dict[str, Any],
) -> None:
    """Send the 'order placed' Telegram notification (HTML)."""
    try:
        title = "✅ Order placed"
        body = (
            f"<b>Instrument</b>: <code>{_tg_escape(key)}</code>\n"
            f"<b>Side</b>: {_tg_escape(txn)}\n"
            f"<b>Quantity</b>: {_tg_escape(quantity)}\n\n"
            f"<b>Response</b>:\n{_format_response_for_tg(response_summary)}"
        )
        telegram_service.send_message(title, body, level="SUCCESS")
    except Exception:
        logger.exception("[place-order] telegram 'completed' notify failed")


def _send_order_failed(
    *,
    key: str,
    txn: str,
    quantity: int,
    error_text: str,
    http_status: Any = None,
) -> None:
    """Send the 'order failed' Telegram notification (HTML)."""
    try:
        title = "❌ Order failed"
        header = f"<b>Instrument</b>: <code>{_tg_escape(key)}</code>\n"
        if http_status is not None:
            header += f"<b>HTTP</b>: {_tg_escape(http_status)}\n"
        body = (
            header
            + f"<b>Side</b>: {_tg_escape(txn)}\n"
            + f"<b>Quantity</b>: {_tg_escape(quantity)}\n\n"
            + f"<b>Error</b>:\n<pre>{_tg_escape(error_text)}</pre>"
        )
        telegram_service.send_message(title, body, level="ERROR")
    except Exception:
        logger.exception("[place-order] telegram 'failed' notify failed")


def place_order(
    instrument_key: str,
    *,
    lots: int = 1,
    transaction_type: str = "BUY",
    product: str = "D",
    order_type: str = "MARKET",
    price: float = 0.0,
    trigger_price: float = 0.0,
    validity: str = "DAY",
    tag: str = "",
    is_amo: bool = False,
    slice: bool = False,
    market_protection: float | None = None,
) -> dict[str, Any]:
    """Place a lot-based Upstox order.

    Returns a dict with ``status`` in
    ``{"ok", "disabled", "no_token", "invalid_input", "error"}``.

    Security: the access token never appears in any log line or in
    the returned dict. Provider error bodies are redacted by
    ``sanitise_payload`` before they leave this function.

    Two HTML-formatted Telegram notifications are sent around the
    Upstox call (started / completed-or-failed). Telegram failures
    never affect the order result.
    """
    called_at = _now_iso()
    key = str(instrument_key or "").strip()
    txn = str(transaction_type or "BUY").upper()
    prod = str(product or "D").upper()
    otype = str(order_type or "MARKET").upper()
    valid = str(validity or "DAY").upper()

    # ---- 0. Input validation (fail fast, before any token/API work) -- #
    if not key:
        return {
            "status": "invalid_input",
            "error": "instrument_key is required",
            "called_at": called_at,
        }
    if txn not in _ALLOWED_TXN_TYPES:
        return {
            "status": "invalid_input",
            "error": "transaction_type must be BUY or SELL",
            "called_at": called_at,
        }
    if prod not in _ALLOWED_PRODUCTS:
        return {
            "status": "invalid_input",
            "error": f"product must be one of {sorted(_ALLOWED_PRODUCTS)}",
            "instrument_key": key,
            "called_at": called_at,
        }
    if otype not in _ALLOWED_ORDER_TYPES:
        return {
            "status": "invalid_input",
            "error": f"order_type must be one of {sorted(_ALLOWED_ORDER_TYPES)}",
            "instrument_key": key,
            "called_at": called_at,
        }
    if valid not in _ALLOWED_VALIDITY:
        return {
            "status": "invalid_input",
            "error": f"validity must be one of {sorted(_ALLOWED_VALIDITY)}",
            "instrument_key": key,
            "called_at": called_at,
        }
    if otype in {"SL", "SL-M"} and float(trigger_price or 0.0) <= 0.0:
        return {
            "status": "invalid_input",
            "error": "trigger_price must be > 0 for SL / SL-M orders",
            "instrument_key": key,
            "called_at": called_at,
        }
    if otype == "LIMIT" and float(price or 0.0) <= 0.0:
        return {
            "status": "invalid_input",
            "error": "price must be > 0 for LIMIT orders",
            "instrument_key": key,
            "called_at": called_at,
        }
    if market_protection is not None:
        try:
            market_protection = float(market_protection)
        except (TypeError, ValueError):
            return {
                "status": "invalid_input",
                "error": "market_protection must be a number or None",
                "instrument_key": key,
                "called_at": called_at,
            }
        if market_protection < 0:
            return {
                "status": "invalid_input",
                "error": "market_protection must be >= 0",
                "instrument_key": key,
                "called_at": called_at,
            }

    # ---- 1. Runtime toggle ----------------------------------------- #
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

    # ---- 2. Token -------------------------------------------------- #
    # Read once. Held only in this local variable for the duration of
    # the call. Never logged, never returned, never interpolated into
    # an error string, and never sent to Telegram.
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

    # ---- 3. Lots -> quantity -------------------------------------- #
    underlying = _resolve_underlying(key)
    quantity, lot_size, lot_source = lots_to_quantity(
        underlying=underlying,
        lots=lots,
        instrument_key=key,
    )

    # SECURITY: no token in this log line.
    logger.info(
        "[place-order] key=%s txn=%s lots=%s qty=%s lot_size=%s "
        "lot_source=%s underlying=%s",
        key, txn, lots, quantity, lot_size, lot_source, underlying,
    )

    # ---- 4. Build the request ------------------------------------- #
    # ``tag`` is omitted when empty because Upstox rejects empty-string
    # tags. The generated model only assigns ``tag`` when it is not None.
    tag_value = str(tag).strip() if tag else None
    try:
        body = upstox_client.PlaceOrderV3Request(
            quantity=int(quantity),
            product=prod,
            validity=valid,
            price=float(price or 0.0),
            tag=tag_value,
            instrument_token=key,
            order_type=otype,
            transaction_type=txn,
            disclosed_quantity=0,
            trigger_price=float(trigger_price or 0.0),
            is_amo=bool(is_amo),
            slice=bool(slice),
            market_protection=market_protection,
        )
    except Exception as exc:
        logger.exception("[place-order] PlaceOrderV3Request build failed")
        return {
            "status": "error",
            "error": f"request_build_failed: {type(exc).__name__}: {exc}",
            "instrument_key": key,
            "transaction_type": txn,
            "quantity": quantity,
            "lot_size": lot_size,
            "called_at": called_at,
        }

    # ---- 5. Telegram: "placing started" --------------------------- #
    _send_order_started(
        key=key,
        txn=txn,
        lots=int(lots),
        quantity=quantity,
        lot_size=lot_size,
        lot_source=lot_source,
        underlying=underlying,
        product=prod,
        order_type=otype,
    )

    # ---- 6. Call -------------------------------------------------- #
    try:
        api_client = _build_client(access_token)
        order_api = upstox_client.OrderApiV3(api_client)
        raw_response = order_api.place_order(body)
    except ApiException as exc:
        http_status = getattr(exc, "status", None)
        # Redact any credential-like run in the provider body before
        # it reaches a log line, the return dict, or Telegram.
        body_preview = sanitise_payload(
            str(getattr(exc, "body", None) or exc)
        )[:300]
        logger.warning(
            "[place-order] ApiException (HTTP %s): %s",
            http_status, body_preview,
        )
        _send_order_failed(
            key=key,
            txn=txn,
            quantity=quantity,
            error_text=str(body_preview),
            http_status=http_status,
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
        _send_order_failed(
            key=key,
            txn=txn,
            quantity=quantity,
            error_text=f"{type(exc).__name__}: {exc}",
        )
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "instrument_key": key,
            "transaction_type": txn,
            "quantity": quantity,
            "lot_size": lot_size,
            "called_at": called_at,
        }

    logger.info(
        "[place-order] OK — key=%s txn=%s qty=%s lot_size=%s",
        key, txn, quantity, lot_size,
    )

    # ---- 7. Telegram: "order placed" (with sanitised response) ---- #
    response_summary = _summarise_api_response(raw_response)
    _send_order_completed(
        key=key,
        txn=txn,
        quantity=quantity,
        response_summary=response_summary,
    )

    # SECURITY: no ``token_masked`` field in the returned dict.
    return {
        "status": "ok",
        "instrument_key": key,
        "underlying": underlying,
        "transaction_type": txn,
        "lots": int(lots),
        "lot_size": lot_size,
        "lot_source": lot_source,
        "quantity": quantity,
        "order_type": otype,
        "product": prod,
        "response": response_summary,
        "error": None,
        "called_at": called_at,
    }