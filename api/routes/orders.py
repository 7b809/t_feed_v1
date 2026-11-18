"""HTTP endpoints for order execution, instrument search and order history."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import upstox_client
from fastapi import APIRouter, HTTPException, Query

from core.config import (
    DEFAULT_ORDER_QUANTITY,
    DEFAULT_PRODUCT,
    DEFAULT_ORDER_TYPE,
    DEFAULT_VALIDITY,
    ORDERS_DATA_DIR,
    TRADING_ENABLED,
    UPSTOX_SANDBOX,
)
from core.logger import get_logger
from models.order_models import ExitAllRequest, OrderIdsRequest, PlaceOrderRequest
from repositories.order_repository import OrderRepository
from services.instrument_search_service import (
    InstrumentSearchError,
    search_upstox_instruments,
)
from services.order_service import (
    OrderService,
    OrderServiceError,
    TradingDisabledError,
)

logger = get_logger(__file__)
router = APIRouter(prefix="/api/v1", tags=["orders"])
_service = OrderService()
_repository = OrderRepository()


def _extract_upstox_error(exc: Exception) -> tuple[str | None, str | None]:
    """Best-effort extraction of (error_code, message) from an Upstox ApiException.

    The SDK stores the raw HTTP body on ``exc.body`` (bytes). We parse it
    defensively: any failure returns ``(None, None)`` and never masks the
    original exception. The envelope we look for is::

        {"status":"error","errors":[{"errorCode":"UDAPI100050",
                                     "message":"Invalid token used to access API"}]}
    """
    body = getattr(exc, "body", None)
    if not body:
        return None, None
    try:
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8", errors="replace")
        payload = json.loads(body)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        first = errors[0]
        code = first.get("errorCode") or first.get("error_code")
        return code, first.get("message")
    return None, None


def _raise_http(exc: Exception):
    """Translate service-layer exceptions into HTTP responses.

    Order of checks:

    1. ``TradingDisabledError``           -> 403
    2. ``upstox_client.rest.ApiException`` -> 401 / 429, else fall through
    3. ``OrderServiceError`` / ``ValueError`` / ``KeyError`` -> 400
    4. Anything else                       -> 502 (with traceback in the log)
    """
    if isinstance(exc, TradingDisabledError):
        raise HTTPException(status_code=403, detail=str(exc))

    if isinstance(exc, upstox_client.rest.ApiException):
        status = getattr(exc, "status", None)
        upstox_code, upstox_message = _extract_upstox_error(exc)

        if status == 401:
            raise HTTPException(
                status_code=401,
                detail={
                    "message": (
                        "Upstox rejected the access token. Refresh "
                        "app_tokens/upstox_access_token in MongoDB "
                        "(access_token field) and retry."
                    ),
                    "upstream": "upstox",
                    "upstream_status": 401,
                    "upstox_error_code": upstox_code,
                    "upstox_message": upstox_message,
                },
            ) from exc

        if status == 429:
            raise HTTPException(
                status_code=429,
                detail={
                    "message": "Upstox rate limit reached. Retry shortly.",
                    "upstream": "upstox",
                    "upstream_status": 429,
                    "upstox_error_code": upstox_code,
                },
            ) from exc

    if isinstance(exc, (OrderServiceError, ValueError, KeyError)):
        raise HTTPException(status_code=400, detail=str(exc))

    logger.exception("Order API operation failed")
    raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")


def _normalize_enum(value: str | None) -> str | None:
    """Return a comma-separated enum value ready to send to Upstox, or None.

    Upstox documents ``exchanges``, ``segments`` and ``instrument_types``
    with "Default: ALL" — which means "omit this parameter to search all
    values", NOT "send the literal string ALL". Sending ``ALL`` produces
    HTTP 400 UDAPI100500 ("Please Enter Correct Segment And Exchange
    Values").

    This helper therefore:

    * returns ``None`` (meaning "omit") for ``None``, empty/whitespace,
      or any case variant of ``"ALL"``;
    * uppercases surviving values, matching the uppercase enum names in
      the docs (``NSE``, ``BSE``, ``MCX``, ``EQ``, ``FO``, ``CE``, ``PE``);
    * leaves commas intact so callers can pass multi-value filters
      (``"NSE,BSE"``, ``"CE,PE"``) unchanged.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped or stripped.upper() == "ALL":
        return None
    return stripped.upper()


@router.get("/config/order-defaults")
def order_defaults():
    return {
        "quantity": DEFAULT_ORDER_QUANTITY,
        "transaction_type": "BUY",
        "product": DEFAULT_PRODUCT,
        "order_type": DEFAULT_ORDER_TYPE,
        "validity": DEFAULT_VALIDITY,
        "trading_enabled": TRADING_ENABLED,
        "upstox_sandbox": UPSTOX_SANDBOX,
    }


@router.get("/instruments/search")
def search_instruments(
    query: str = Query(min_length=1, max_length=50),
    exchanges: str = "ALL",
    segments: str = "ALL",
    instrument_types: str | None = None,
    expiry: str | None = None,
    atm_offset: int | None = None,
    page_number: int = Query(default=1, ge=1),
    records: int = Query(default=15, ge=1, le=30),
):
    """Search Upstox and fall back to a bounded-age local cache on upstream failures.

    ``exchanges``, ``segments`` and ``instrument_types`` use Upstox's
    documented "Default: ALL" semantics: the literal string ``ALL``
    (or an empty value) means *omit the parameter* so Upstox searches
    every exchange/segment/type. Sending ``ALL`` verbatim to Upstox
    returns HTTP 400 UDAPI100500; see ``_normalize_enum``.
    """
    params: dict[str, object] = {
        "query": query.strip(),
        "page_number": page_number,
        "records": records,
    }
    normalized_exchanges = _normalize_enum(exchanges)
    normalized_segments = _normalize_enum(segments)
    normalized_types = _normalize_enum(instrument_types)
    if normalized_exchanges:
        params["exchanges"] = normalized_exchanges
    if normalized_segments:
        params["segments"] = normalized_segments
    if normalized_types:
        params["instrument_types"] = normalized_types
    if expiry:
        # Expiry accepts both keywords (current_month, next_week, …) and
        # yyyy-MM-dd dates. The docs don't define "ALL" for expiry, but we
        # treat it the same way for consistency with the other filters.
        expiry_clean = expiry.strip()
        if expiry_clean and expiry_clean.upper() != "ALL":
            params["expiry"] = expiry_clean
    if atm_offset is not None:
        params["atm_offset"] = atm_offset

    try:
        return search_upstox_instruments(params)
    except InstrumentSearchError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"message": str(exc), **exc.details},
        ) from exc
    except RuntimeError as exc:
        logger.exception("Instrument search could not load the Upstox access token")
        raise HTTPException(
            status_code=503, detail="Upstox access token is unavailable"
        ) from exc


@router.get("/orders/dates")
def order_dates():
    """Return available daily ledgers from local JSON and MongoDB."""
    dates: set[str] = set()
    folder = Path(ORDERS_DATA_DIR)
    if folder.exists():
        dates.update(path.stem for path in folder.glob("????-??-??.json"))
    try:
        rows = (
            _repository.collection.find({}, {"_id": 1, "date": 1})
            .sort("_id", -1)
            .limit(500)
        )
        for row in rows:
            value = row.get("date") or row.get("_id")
            if value:
                dates.add(str(value))
    except Exception:
        logger.exception("Could not enumerate MongoDB order dates; using local ledgers")
    return {"dates": sorted(dates, reverse=True)}


@router.get("/orders/history")
def order_history(date_key: str = Query(alias="date")):
    try:
        date.fromisoformat(date_key)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must use YYYY-MM-DD")
    try:
        return _repository.load_daily_document(date_key)
    except Exception as exc:
        _raise_http(exc)


@router.post("/orders/place")
def place_order(body: PlaceOrderRequest):
    try:
        return _service.place_order(body)
    except Exception as exc:
        _raise_http(exc)


@router.get("/orders/today")
def get_today_orders():
    try:
        return _repository.list_today()
    except Exception as exc:
        _raise_http(exc)


@router.post("/orders/cancel/bulk")
def cancel_orders(body: OrderIdsRequest):
    try:
        return {"results": _service.cancel_orders(body.order_ids)}
    except Exception as exc:
        _raise_http(exc)


@router.post("/orders/exit-by-ids")
def exit_orders_by_ids(body: OrderIdsRequest):
    try:
        return {"results": _service.exit_orders(body.order_ids)}
    except Exception as exc:
        _raise_http(exc)


@router.post("/orders/exit-all")
def exit_all_positions(body: ExitAllRequest = ExitAllRequest()):
    try:
        return _service.exit_all_positions(tag=body.tag, segment=body.segment)
    except Exception as exc:
        _raise_http(exc)


@router.post("/orders/{order_id}/cancel")
def cancel_single_order(order_id: str):
    try:
        return _service.cancel_order(order_id)
    except Exception as exc:
        _raise_http(exc)


@router.post("/orders/{order_id}/exit")
def exit_single_order(order_id: str):
    try:
        return _service.exit_order(order_id)
    except Exception as exc:
        _raise_http(exc)


@router.get("/orders/{order_id}")
def get_local_order(order_id: str):
    try:
        order = _repository.find_order(order_id)
        if not order:
            raise HTTPException(
                status_code=404,
                detail="Order not found in today's local ledger",
            )
        return order
    except HTTPException:
        raise
    except Exception as exc:
        _raise_http(exc)