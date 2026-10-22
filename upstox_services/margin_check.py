# File: upstox_services/margin_check.py
"""Check the Upstox margin requirement for a single instrument.

Standalone module + CLI.

Function
--------
    check_margin(
        instrument_key,        # required, e.g. "NSE_FO|47162"
        lots=1,                # number of lots (default 1)
        transaction_type="BUY",# "BUY" | "SELL" (default BUY)
        product="D",           # "D" (intraday) | "I" (delivery)
        price=0.0,
        verbose=False,         # False -> only margin numbers;
                               # True  -> include the Upstox response
    ) -> dict

Return shape (default, verbose=False)::

    {
      "status":             "ok",
      "instrument_key":     "NSE_FO|47162",
      "underlying":         ...,
      "transaction_type":   "BUY",
      "lots":               1,
      "lot_size":           65,
      "lot_source":         "exchange_default",
      "quantity":           65,
      "product":            "D",
      "required_margin":    1898.0,
      "final_margin":       1898.0,
      "error":              None,
      "called_at":          "2026-10-19T…",
    }

Return shape (verbose=True) additionally carries a **sanitised**
``response`` dict. See ``upstox_services._sanitise`` for the redaction
rules.

Security guarantees (2026-10-22 token-leak fix)
-----------------------------------------------
The project's security policy is absolute: **no access token — masked
or otherwise — appears in any log line or returned dict.**

The previous revision was already compliant inside ``check_margin``
itself (the token was only held in a local variable). This revision
adds two extra layers of defence:

  * the ``_error_body_preview`` helper now goes through
    ``sanitise_payload`` (it always did) and the caller truncates to
    300 chars — so a provider body that happens to embed an
    ``Authorization`` header cannot leak;
  * the ``_summarise_margin_response`` helper already sanitises the
    verbose ``response`` payload — this revision keeps that
    behaviour and documents it as part of the security contract.

Additional hardening added in this revision
-------------------------------------------
  * ``check_margin`` now returns ``"quantity": quantity`` **and**
    ``"lot_size": lot_size`` on every error branch that has already
    resolved the lot size. Previously, an ``ApiException`` from
    ``post_margin`` returned the lot metadata but a subsequent
    "request build failed" branch did not — inconsistent shapes made
    downstream accounting harder.
  * The CLI ``main`` exit codes are unchanged (0 ok / 1 error /
    2 no_token).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Any

import requests
import upstox_client
from upstox_client.rest import ApiException

from core.logger import get_logger
from services.instrument_registry import instrument_registry
from services.token_service import token_service
from upstox_services._sanitise import sanitise_payload
from upstox_services.lot_sizes import lots_to_quantity

logger = get_logger(__file__)

_UPSTOX_BASE = "https://api.upstox.com"
_HTTP_TIMEOUT = 10


# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lookup_registry_underlying(instrument_key: str) -> str | None:
    try:
        entry = instrument_registry.get(instrument_key)
        if entry:
            return entry.get("underlying")
    except Exception:
        logger.exception(
            "[margin] registry lookup failed for %s", instrument_key
        )
    return None


def _register_and_read_underlying(instrument_key: str) -> str | None:
    try:
        entry = instrument_registry.register_adhoc(instrument_key)
        if entry:
            return entry.get("underlying")
    except Exception:
        logger.exception(
            "[margin] register_adhoc failed for %s", instrument_key
        )
    return None


def _search_upstox_underlying(
    instrument_key: str,
    access_token: str,
) -> str | None:
    """Best-effort Upstox search using the tail of the key as the query.

    The token is passed only as the ``Authorization`` header value of
    a single ``requests.get`` call. It is never logged and never
    returned.
    """
    key = str(instrument_key or "").strip()
    if "|" not in key:
        return None
    tail = key.split("|", 1)[1]
    if not tail:
        return None
    try:
        resp = requests.get(
            f"{_UPSTOX_BASE}/v2/instruments/search",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            params={
                "query": tail,
                "records": 5,
                "page_number": 1,
            },
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException:
        logger.info("[margin] Upstox search network error for %s", key)
        return None
    if resp.status_code >= 400:
        logger.info(
            "[margin] Upstox search HTTP %s for %s",
            resp.status_code, key,
        )
        return None
    try:
        items = resp.json().get("data") or []
    except ValueError:
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("instrument_key") or "") == key:
            underlying = (
                item.get("underlying_symbol")
                or item.get("name")
                or item.get("trading_symbol")
            )
            if underlying:
                return str(underlying)
    return None


def _resolve_underlying(instrument_key: str, access_token: str) -> str | None:
    """Resolve the underlying using the full fallback chain."""
    u = _lookup_registry_underlying(instrument_key)
    if u:
        return u

    u = _register_and_read_underlying(instrument_key)
    if u:
        return u

    u = _search_upstox_underlying(instrument_key, access_token)
    if u:
        return u

    # Fall through — lots_to_quantity has a per-exchange default.
    return None


def _summarise_margin_response(raw: Any) -> dict[str, Any]:
    """Return a sanitised, serialisable summary of the Upstox response.

    Every branch is passed through ``sanitise_payload`` so a provider
    body that happens to embed an ``Authorization`` header or a
    bearer token cannot leak into the verbose ``response`` field.
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


def _extract_margin_numbers(
    summary: dict[str, Any],
) -> tuple[float | None, float | None]:
    """Return (required_margin, final_margin) from a ChargeApi response."""
    if not isinstance(summary, dict):
        return None, None
    data = summary.get("data")
    if not isinstance(data, dict):
        return None, None

    def _flt(v: Any) -> float | None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return _flt(data.get("required_margin")), _flt(data.get("final_margin"))


def _error_body_preview(exc: ApiException) -> str:
    """Return a redacted, length-capped preview of an ApiException body.

    ``sanitise_payload`` redacts any run of 20+ token-like characters
    and any value under a key in ``SECRET_KEYS`` (``access_token``,
    ``authorization``, ``bearer``, ``x-api-key``, …). The 300-char
    cap keeps the log line readable.
    """
    body = getattr(exc, "body", None) or exc
    text = sanitise_payload(str(body))
    return text[:300]


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #
def check_margin(
    instrument_key: str,
    *,
    lots: int = 1,
    transaction_type: str = "BUY",
    product: str = "D",
    price: float = 0.0,
    verbose: bool = False,
) -> dict[str, Any]:
    """Return the Upstox margin requirement for one instrument.

    ``verbose=False`` (default) returns only the margin numbers plus
    the lot / quantity metadata. ``verbose=True`` includes a sanitised
    copy of the Upstox response under the ``response`` key.

    Security: the access token never appears in any log line or in the
    returned dict. Provider error bodies and verbose responses are
    redacted by ``sanitise_payload`` before they leave this function.
    """
    called_at = _now_iso()
    key = str(instrument_key or "").strip()
    txn = str(transaction_type or "BUY").upper()

    if not key:
        return {
            "status": "invalid_input",
            "error": "instrument_key is required",
            "called_at": called_at,
        }
    if txn not in {"BUY", "SELL"}:
        return {
            "status": "invalid_input",
            "error": "transaction_type must be BUY or SELL",
            "instrument_key": key,
            "called_at": called_at,
        }

    # ---- 1. Token -------------------------------------------------- #
    # Held only in this local variable. Never logged, never returned.
    try:
        access_token = token_service.get_access_token()
    except Exception as exc:
        logger.exception("[margin] token lookup raised")
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

    # ---- 2. Underlying + lots -> quantity ------------------------- #
    underlying = _resolve_underlying(key, access_token)
    quantity, lot_size, lot_source = lots_to_quantity(
        underlying=underlying,
        lots=lots,
        instrument_key=key,
    )

    # SECURITY: no token in this log line. The ``underlying`` value
    # can legitimately be the ticker ("NIFTY", "SENSEX", …); it is not
    # sensitive.
    logger.info(
        "[margin] key=%s txn=%s lots=%s qty=%s lot_size=%s "
        "lot_source=%s underlying=%s verbose=%s",
        key, txn, lots, quantity, lot_size, lot_source,
        underlying, verbose,
    )

    # ---- 3. Build the request ------------------------------------- #
    try:
        instruments = [
            upstox_client.Instrument(
                instrument_key=key,
                quantity=int(quantity),
                product=str(product or "D"),
                transaction_type=txn,
                price=float(price or 0.0),
            )
        ]
        margin_body = upstox_client.MarginRequest(instruments)
    except Exception as exc:
        logger.exception("[margin] MarginRequest build failed")
        return {
            "status": "error",
            "error": f"request_build_failed: {type(exc).__name__}",
            "instrument_key": key,
            "transaction_type": txn,
            # Carried through so the caller has the resolved lot data
            # even when the request object could not be built.
            "lots": int(lots),
            "lot_size": lot_size,
            "lot_source": lot_source,
            "quantity": quantity,
            "called_at": called_at,
        }

    # ---- 4. Call -------------------------------------------------- #
    try:
        configuration = upstox_client.Configuration()
        configuration.access_token = access_token
        api_instance = upstox_client.ChargeApi(
            upstox_client.ApiClient(configuration)
        )
        raw_response = api_instance.post_margin(margin_body)
    except ApiException as exc:
        http_status = getattr(exc, "status", None)
        preview = _error_body_preview(exc)
        logger.warning(
            "[margin] ApiException (HTTP %s): %s",
            http_status, preview,
        )
        return {
            "status": "error",
            "error": f"Upstox ApiException (HTTP {http_status}): {preview}",
            "http_status": http_status,
            "instrument_key": key,
            "transaction_type": txn,
            "quantity": quantity,
            "lot_size": lot_size,
            "called_at": called_at,
        }
    except Exception as exc:
        logger.exception("[margin] unexpected error")
        return {
            "status": "error",
            "error": f"{type(exc).__name__}",
            "instrument_key": key,
            "transaction_type": txn,
            "quantity": quantity,
            "lot_size": lot_size,
            "called_at": called_at,
        }

    # ---- 5. Summarise (sanitised) -------------------------------- #
    response_summary = _summarise_margin_response(raw_response)
    required_margin, final_margin = _extract_margin_numbers(response_summary)

    logger.info(
        "[margin] OK — key=%s txn=%s qty=%s lot_size=%s "
        "required_margin=%s final_margin=%s",
        key, txn, quantity, lot_size, required_margin, final_margin,
    )

    base: dict[str, Any] = {
        "status": "ok",
        "instrument_key": key,
        "underlying": underlying,
        "transaction_type": txn,
        "lots": int(lots),
        "lot_size": lot_size,
        "lot_source": lot_source,
        "quantity": quantity,
        "product": str(product or "D"),
        "required_margin": required_margin,
        "final_margin": final_margin,
        "error": None,
        "called_at": called_at,
    }

    # SECURITY: no ``token_masked`` field on the returned dict. The
    # verbose ``response`` is the sanitised summary produced above.
    if verbose:
        base["response"] = response_summary

    return base


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Check Upstox margin requirement for one instrument.",
    )
    p.add_argument(
        "--instrument-key", required=True,
        help='Instrument key, e.g. "NSE_FO|47162".',
    )
    p.add_argument("--lots", type=int, default=1)
    p.add_argument(
        "--type", dest="transaction_type", default="BUY",
        choices=["BUY", "SELL"],
    )
    p.add_argument("--product", default="D", choices=["D", "I"])
    p.add_argument("--price", type=float, default=0.0)
    p.add_argument(
        "--verbose", action="store_true",
        help="Include a sanitised copy of the Upstox response payload.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    result = check_margin(
        args.instrument_key,
        lots=args.lots,
        transaction_type=args.transaction_type,
        product=args.product,
        price=args.price,
        verbose=args.verbose,
    )
    status = result.get("status")
    if status == "ok":
        if args.verbose:
            print(f"Margin check OK: {result.get('response')}")
        else:
            print(
                f"Margin check OK: required_margin="
                f"{result.get('required_margin')} "
                f"final_margin={result.get('final_margin')} "
                f"qty={result.get('quantity')} "
                f"lot_size={result.get('lot_size')}"
            )
        return 0
    print(f"ERROR ({status}): {result.get('error')}")
    return 2 if status == "no_token" else 1


if __name__ == "__main__":
    sys.exit(main())