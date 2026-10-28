# upstox_app/services/expired_instruments_service.py
"""Service layer for Upstox *expired instrument* APIs.

Three public methods, each wrapping a single
``upstox_client.ExpiredInstrumentApi`` call:

    1. ``get_expiries(instrument_key)``
         -> list of expired expiry dates for the given index
    2. ``get_expired_option_contracts(instrument_key, expiry_date)``
         -> list of expired option contracts (CE / PE / FUT)
    3. ``get_expired_historical_candles(expired_instrument_key,
                                        interval, from_date, to_date)``
         -> historical candle list for one expired instrument

Security policy
---------------
No access token appears in any log line or returned dict. Every
Upstox response body and every ``ApiException.body`` is passed
through ``upstox_services._sanitise.sanitise_payload`` before being
logged or returned.

SDK argument-order quirk (2026-10-28)
-------------------------------------
``ExpiredInstrumentApi.get_expired_historical_candle_data`` has the
Python signature ``(expired_instrument_key, interval, from_date,
to_date, ...)``, but the generated SDK builds the URL as:

    /v2/expired-instruments/historical-candle/{key}/{interval}
                                        /{to_date}/{from_date}

i.e. on the wire ``to_date`` comes BEFORE ``from_date``. The SDK's
parameter names therefore do NOT correspond to the URL slots they
fill, and passing ``from_date=2026-09-28, to_date=2026-09-29`` results
in the SDK emitting ``.../2026-09-29/2026-09-28`` — which Upstox
rejects with ``UDAPI1015: to_date must be >= from_date``.

Fix: swap the two date arguments at the SDK call site so the wire
URL receives ``{from_date}/{to_date}`` in the order Upstox expects:

    api.get_expired_historical_candle_data(
        key, interval,
        to_date,    # SDK "from_date" slot  -> wire "{to_date}"   (later)
        from_date,  # SDK "to_date"   slot  -> wire "{from_date}" (earlier)
    )

The public method signature stays ``(..., from_date, to_date)`` for
the API callers; only the SDK call is flipped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import upstox_client
from upstox_client.rest import ApiException

from core.logger import get_logger
from services.token_service import token_service
from upstox_services._sanitise import sanitise_payload

logger = get_logger(__file__)


# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_api() -> "upstox_client.ExpiredInstrumentApi | None":
    """Build a configured ``ExpiredInstrumentApi``.

    Returns ``None`` when no access token is available. The token is
    held only in the local ``access_token`` variable and is never
    logged, never returned, never interpolated into an error string.
    """
    try:
        access_token = token_service.get_access_token()
    except Exception:
        logger.exception("[expired] token lookup raised")
        return None

    if not access_token:
        return None

    configuration = upstox_client.Configuration()
    configuration.access_token = access_token
    return upstox_client.ExpiredInstrumentApi(
        upstox_client.ApiClient(configuration)
    )


def _summarise_response(raw: Any) -> dict[str, Any]:
    """Return a sanitised, JSON-serialisable summary of the Upstox response."""
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


def _api_exception_preview(exc: ApiException) -> str:
    """Return a redacted, length-capped preview of an ApiException body."""
    body = getattr(exc, "body", None) or exc
    text = sanitise_payload(str(body))
    return text[:300]


def _extract_data(summary: dict[str, Any]) -> Any:
    """Return the inner ``data`` payload from a sanitised response."""
    if not isinstance(summary, dict):
        return None
    return summary.get("data")


# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class ExpiredInstrumentsService:
    """Wraps ``upstox_client.ExpiredInstrumentApi``."""

    # ------------------------------------------------------------------ #
    # 1. Expiries list                                                    #
    # ------------------------------------------------------------------ #
    def get_expiries(self, instrument_key: str) -> dict[str, Any]:
        """Return the list of expired expiry dates for the given index."""
        called_at = _now_iso()
        key = str(instrument_key or "").strip()

        if not key:
            return {
                "status": "invalid_input",
                "error": "instrument_key is required",
                "called_at": called_at,
            }

        api = _build_api()
        if api is None:
            return {
                "status": "no_token",
                "error": "access_token_missing",
                "instrument_key": key,
                "called_at": called_at,
            }

        try:
            response = api.get_expiries(key)
        except ApiException as exc:
            http_status = getattr(exc, "status", None)
            preview = _api_exception_preview(exc)
            logger.warning(
                "[expired] get_expiries ApiException (HTTP %s): %s",
                http_status, preview,
            )
            return {
                "status": "error",
                "error": (
                    f"Upstox ApiException (HTTP {http_status}): {preview}"
                ),
                "http_status": http_status,
                "instrument_key": key,
                "called_at": called_at,
            }
        except Exception as exc:
            logger.exception("[expired] get_expiries unexpected error")
            return {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "instrument_key": key,
                "called_at": called_at,
            }

        summary = _summarise_response(response)
        data = _extract_data(summary)
        expiries: list[Any] = data if isinstance(data, list) else []

        logger.info(
            "[expired] get_expiries OK — key=%s count=%d",
            key, len(expiries),
        )
        return {
            "status": "ok",
            "instrument_key": key,
            "count": len(expiries),
            "expiries": expiries,
            "response": summary,
            "called_at": called_at,
        }

    # ------------------------------------------------------------------ #
    # 2. Expired contracts for one expiry                                 #
    # ------------------------------------------------------------------ #
    def get_expired_option_contracts(
        self,
        instrument_key: str,
        expiry_date: str,
    ) -> dict[str, Any]:
        """Return the list of expired contracts for one expiry date."""
        called_at = _now_iso()
        key = str(instrument_key or "").strip()
        exp = str(expiry_date or "").strip()

        if not key:
            return {
                "status": "invalid_input",
                "error": "instrument_key is required",
                "called_at": called_at,
            }
        if not exp:
            return {
                "status": "invalid_input",
                "error": "expiry_date is required (YYYY-MM-DD)",
                "instrument_key": key,
                "called_at": called_at,
            }

        api = _build_api()
        if api is None:
            return {
                "status": "no_token",
                "error": "access_token_missing",
                "instrument_key": key,
                "expiry_date": exp,
                "called_at": called_at,
            }

        try:
            response = api.get_expired_option_contracts(key, exp)
        except ApiException as exc:
            http_status = getattr(exc, "status", None)
            preview = _api_exception_preview(exc)
            logger.warning(
                "[expired] get_expired_option_contracts ApiException "
                "(HTTP %s): %s", http_status, preview,
            )
            return {
                "status": "error",
                "error": (
                    f"Upstox ApiException (HTTP {http_status}): {preview}"
                ),
                "http_status": http_status,
                "instrument_key": key,
                "expiry_date": exp,
                "called_at": called_at,
            }
        except Exception as exc:
            logger.exception(
                "[expired] get_expired_option_contracts unexpected error"
            )
            return {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "instrument_key": key,
                "expiry_date": exp,
                "called_at": called_at,
            }

        summary = _summarise_response(response)
        data = _extract_data(summary)
        contracts: list[Any] = data if isinstance(data, list) else []

        logger.info(
            "[expired] get_expired_option_contracts OK — key=%s "
            "expiry=%s count=%d",
            key, exp, len(contracts),
        )
        return {
            "status": "ok",
            "instrument_key": key,
            "expiry_date": exp,
            "count": len(contracts),
            "contracts": contracts,
            "response": summary,
            "called_at": called_at,
        }

    # ------------------------------------------------------------------ #
    # 3. Historical candles for one expired instrument                    #
    # ------------------------------------------------------------------ #
    def get_expired_historical_candles(
        self,
        expired_instrument_key: str,
        interval: str,
        from_date: str,
        to_date: str,
    ) -> dict[str, Any]:
        """Return historical candles for one expired instrument.

        ``from_date`` is the EARLIER date, ``to_date`` the LATER date —
        this is the natural reading and matches the public HTTP
        contract of ``GET /upstox-app/expired/candles``.

        The SDK call below flips the two date arguments on purpose —
        see the module docstring "SDK argument-order quirk" section
        for the full explanation. Do not "fix" the flip; the wire URL
        requires this ordering.
        """
        called_at = _now_iso()
        key = str(expired_instrument_key or "").strip()
        itv = str(interval or "").strip()
        fd  = str(from_date or "").strip()
        td  = str(to_date or "").strip()

        if not key:
            return {
                "status": "invalid_input",
                "error": (
                    "expired_instrument_key is required "
                    "(format: <exchange>|<token>|<DD-MM-YYYY>)"
                ),
                "called_at": called_at,
            }
        if not itv:
            return {
                "status": "invalid_input",
                "error": (
                    "interval is required (e.g. '1minute', '30minute', "
                    "'1day', '1week', '1month')"
                ),
                "expired_instrument_key": key,
                "called_at": called_at,
            }
        if not fd or not td:
            return {
                "status": "invalid_input",
                "error": "from_date and to_date are required (YYYY-MM-DD)",
                "expired_instrument_key": key,
                "interval": itv,
                "called_at": called_at,
            }

        api = _build_api()
        if api is None:
            return {
                "status": "no_token",
                "error": "access_token_missing",
                "expired_instrument_key": key,
                "interval": itv,
                "from_date": fd,
                "to_date": td,
                "called_at": called_at,
            }

        try:
            # ------------------------------------------------------------------
            # SDK ARGUMENT-ORDER QUIRK (see module docstring):
            # the Python SDK builds the URL as
            #     .../{interval}/{to_date}/{from_date}
            # so the parameter NAMED "from_date" in Python actually
            # lands in the URL's {to_date} slot, and vice versa.
            #
            # Passing the natural (fd, td) here makes the SDK emit
            #     /.../{interval}/{fd}/{td}
            # = {to_date}=fd, {from_date}=td  ->  fromDate > toDate
            #   ->  UDAPI1015.
            #
            # Flipping the two arguments makes the wire URL carry
            # {to_date}=td, {from_date}=fd  as Upstox expects.
            # ------------------------------------------------------------------
            response = api.get_expired_historical_candle_data(
                key, itv,
                td,   # SDK "from_date" slot -> wire {to_date}   (later)
                fd,   # SDK "to_date"   slot -> wire {from_date} (earlier)
            )
        except ApiException as exc:
            http_status = getattr(exc, "status", None)
            preview = _api_exception_preview(exc)
            logger.warning(
                "[expired] get_expired_historical_candle_data "
                "ApiException (HTTP %s): %s", http_status, preview,
            )
            return {
                "status": "error",
                "error": (
                    f"Upstox ApiException (HTTP {http_status}): {preview}"
                ),
                "http_status": http_status,
                "expired_instrument_key": key,
                "interval": itv,
                "from_date": fd,
                "to_date": td,
                "called_at": called_at,
            }
        except Exception as exc:
            logger.exception(
                "[expired] get_expired_historical_candle_data "
                "unexpected error"
            )
            return {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "expired_instrument_key": key,
                "interval": itv,
                "from_date": fd,
                "to_date": td,
                "called_at": called_at,
            }

        summary = _summarise_response(response)
        data = _extract_data(summary)
        candles: list[Any] = []
        if isinstance(data, dict):
            maybe = data.get("candles")
            if isinstance(maybe, list):
                candles = maybe
        elif isinstance(data, list):
            candles = data

        logger.info(
            "[expired] get_expired_historical_candle_data OK — "
            "key=%s interval=%s from=%s to=%s count=%d",
            key, itv, fd, td, len(candles),
        )
        return {
            "status": "ok",
            "expired_instrument_key": key,
            "interval": itv,
            "from_date": fd,
            "to_date": td,
            "count": len(candles),
            "candles": candles,
            "response": summary,
            "called_at": called_at,
        }


# --------------------------------------------------------------------------- #
# Singleton                                                                   #
# --------------------------------------------------------------------------- #
expired_instruments_service = ExpiredInstrumentsService()