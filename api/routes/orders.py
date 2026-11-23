"""HTTP endpoints for order execution, instrument search and order history."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import upstox_client
from fastapi import APIRouter, HTTPException, Query

from core.config import (
    DEFAULT_ORDER_QUANTITY,
    DEFAULT_ORDER_TYPE,
    DEFAULT_PRODUCT,
    DEFAULT_VALIDITY,
    ORDERS_DATA_DIR,
    TRADING_ENABLED,
    UPSTOX_SANDBOX,
)
from core.logger import get_logger
from models.order_models import ExitAllRequest, OrderIdsRequest, PlaceOrderRequest
from repositories.order_repository import OrderRepository
from services.daily_profit_guard_service import DailyProfitGuardError
from services.instrument_search_service import (
    InstrumentSearchError,
    search_upstox_instruments,
)
from services.order_service import (
    OrderService,
    OrderServiceError,
    TradingDisabledError,
    _exception_text,
    _parse_api_exception,
)

logger = get_logger(__file__)
router = APIRouter(prefix="/api/v1", tags=["orders"])
_service = OrderService()
_repository = OrderRepository()


def _infer_action_from_exc(exc: Exception) -> str:
    """Best-effort action label so simplified error messages match the operation."""
    msg = str(exc).lower()
    if "cancel" in msg:
        return "cancel"
    if "exit" in msg:
        return "exit"
    return "order"


def _build_error_detail(
    exc: Exception,
    *,
    status_code: int,
    action: str | None = None,
    override_message: str | None = None,
) -> dict[str, Any]:
    """Return a clean, frontend-ready error payload with simplified `error` and `broker_error`."""
    resolved_action = action or _infer_action_from_exc(exc)
    broker_error = _parse_api_exception(exc, action=resolved_action)
    if broker_error.get("http_status") is None:
        broker_error["http_status"] = status_code

    simplified_error = (
        override_message
        or _exception_text(exc, action=resolved_action)
        or f"{resolved_action.capitalize()} failed."
    )

    detail: dict[str, Any] = {
        "status": "error",
        "error": simplified_error,
        "message": simplified_error,
        "broker_error": broker_error,
    }

    first_err = (
        broker_error.get("errors")[0]
        if isinstance(broker_error.get("errors"), list) and broker_error.get("errors")
        else {}
    )
    upstox_code = first_err.get("errorCode") or first_err.get("error_code")
    upstox_message = first_err.get("message")
    if upstox_code:
        detail["upstox_error_code"] = upstox_code
    if upstox_message:
        detail["upstox_message"] = upstox_message

    return detail


def _raise_http(exc: Exception, *, action: str | None = None):
    """Translate service-layer exceptions into HTTP responses.

    Order of checks:

    1. ``TradingDisabledError``           -> 403
    2. ``upstox_client.rest.ApiException`` -> upstream status (401/403/429/etc., default 502)
    3. ``OrderServiceError`` / ``ValueError`` / ``KeyError`` -> 400
    4. Anything else                       -> 502 (with traceback in the log)
    """
    resolved_action = action or _infer_action_from_exc(exc)

    if isinstance(exc, DailyProfitGuardError):
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": str(exc), "code": exc.code, **exc.details},
        ) from exc

    if isinstance(exc, TradingDisabledError):
        raise HTTPException(
            status_code=403,
            detail=_build_error_detail(
                exc,
                status_code=403,
                action=resolved_action,
                override_message=str(exc)
                or "Order not placed because trading is disabled.",
            ),
        ) from exc

    if isinstance(exc, upstox_client.rest.ApiException):
        raw_status = getattr(exc, "status", None)
        status_code = (
            raw_status if isinstance(raw_status, int) and 400 <= raw_status < 600 else 502
        )

        override_msg: str | None = None
        if status_code == 401:
            override_msg = (
                "Upstox rejected the access token. Please refresh the token and retry."
            )
        elif status_code == 429:
            override_msg = "Upstox rate limit reached. Please retry shortly."

        if status_code >= 500:
            logger.exception("Upstox API returned server error %s", status_code)

        raise HTTPException(
            status_code=status_code,
            detail=_build_error_detail(
                exc,
                status_code=status_code,
                action=resolved_action,
                override_message=override_msg,
            ),
        ) from exc

    if isinstance(exc, (OrderServiceError, ValueError, KeyError)):
        raise HTTPException(
            status_code=400,
            detail=_build_error_detail(
                exc,
                status_code=400,
                action=resolved_action,
            ),
        ) from exc

    logger.exception("Order API operation failed")
    raise HTTPException(
        status_code=502,
        detail=_build_error_detail(
            exc,
            status_code=502,
            action=resolved_action,
        ),
    ) from exc


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
        _raise_http(exc, action="order")


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
        _raise_http(exc, action="cancel")


@router.post("/orders/exit-by-ids")
def exit_orders_by_ids(body: OrderIdsRequest):
    try:
        return {"results": _service.exit_orders(body.order_ids)}
    except Exception as exc:
        _raise_http(exc, action="exit")


@router.post("/orders/exit-all")
def exit_all_positions(body: ExitAllRequest = ExitAllRequest()):
    try:
        return _service.exit_all_positions(tag=body.tag, segment=body.segment)
    except Exception as exc:
        _raise_http(exc, action="exit")


@router.post("/orders/{order_id}/cancel")
def cancel_single_order(order_id: str):
    try:
        return _service.cancel_order(order_id)
    except Exception as exc:
        _raise_http(exc, action="cancel")


@router.post("/orders/{order_id}/exit")
def exit_single_order(order_id: str):
    try:
        return _service.exit_order(order_id)
    except Exception as exc:
        _raise_http(exc, action="exit")


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