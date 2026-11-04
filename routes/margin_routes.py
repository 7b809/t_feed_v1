# File: routes/margin_routes.py
"""FastAPI routes for Upstox margin checks.

Mounted under ``/margin``.

Endpoints
---------
GET  /margin/single      – check one instrument (query params, one input box per field)
POST /margin/single      – same check via a JSON body (Swagger "Try it out" with a body)
POST /margin/bulk        – check many instruments in one call

Every endpoint takes the same five knobs, and every one has a default::

    instrument_key    (required)
    lots              = 1
    transaction_type  = "BUY"    # BUY | SELL
    product           = "D"      # D = intraday, I = delivery
    price             = 0.0
    verbose           = false

So ``GET /margin/single?instrument_key=NSE_FO%7C47162`` checks
**BUY × 1 lot × D** for that key.  For NIFTY the default ``lots=1``
resolves to the exchange lot size (65 in the current registry — 75
after the December 2024 revision; whatever ``lots_to_quantity``
reports is what you get). That resolution happens inside
``upstox_services.margin_check``; the route only forwards ``lots``.

Why the GET takes its inputs as individual query params
-------------------------------------------------------
Swagger UI renders query parameters as **one small input box per
field** (that is the ``Name / Description`` table you see on the
historical routes).  A POST body, by contrast, renders as one big
JSON editor — which is what you *don't* want in Swagger.  So the
``GET /margin/single`` route here uses plain query parameters and
``POST /margin/single`` uses a body; use whichever fits your client.
The ``POST /margin/bulk`` remains a body-based endpoint, because a
list cannot be expressed cleanly as query params.

HTTP status on ``/margin/single`` (both verbs) follows the
``status`` field returned by ``check_margin``::

    ok             -> 200
    invalid_input  -> 400
    no_token       -> 503
    error          -> 502
    anything else  -> 500

``/margin/bulk`` always returns **200**; each item in the response
list carries its own ``status`` field.  Items are processed
sequentially to stay friendly to the Upstox rate limit.

Security note
-------------
The whole security contract of ``check_margin`` carries through: the
access token is never in any response or log line, and the verbose
``response`` payload is already sanitised before it reaches this
layer.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from upstox_services.margin_check import check_margin

router = APIRouter(prefix="/margin", tags=["margin"])


_STATUS_CODE = {
    "ok": 200,
    "invalid_input": 400,
    "no_token": 503,
    "error": 502,
}


# --------------------------------------------------------------------------- #
# Pydantic models (used for POST bodies and for response typing)              #
# --------------------------------------------------------------------------- #
class MarginCheckRequest(BaseModel):
    """JSON body for ``POST /margin/single``.

    Defaults match the module docstring: BUY × 1 lot × D.
    """

    instrument_key: str = Field(
        ...,
        description='Instrument key, e.g. "NSE_FO|47162".',
        examples=["NSE_FO|47162"],
    )
    lots: int = Field(1, ge=1, description="Number of lots (default 1).")
    transaction_type: Literal["BUY", "SELL"] = Field(
        "BUY",
        description="BUY or SELL (default BUY). Case-insensitive.",
    )
    product: Literal["D", "I"] = Field(
        "D",
        description="D = intraday, I = delivery (default D). Case-insensitive.",
    )
    price: float = Field(0.0, ge=0.0, description="Order price (default 0).")
    verbose: bool = Field(
        False,
        description="Include a sanitised copy of the Upstox response.",
    )

    @field_validator("instrument_key")
    @classmethod
    def _strip_key(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("instrument_key is required")
        return v

    @field_validator("transaction_type", mode="before")
    @classmethod
    def _upper_txn(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v

    @field_validator("product", mode="before")
    @classmethod
    def _upper_product(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v


class BulkMarginCheckRequest(BaseModel):
    """JSON body for ``POST /margin/bulk``."""

    instruments: list[MarginCheckRequest] = Field(
        ...,
        min_length=1,
        description="One entry per instrument to check.",
    )


class MarginCheckResult(BaseModel):
    """Per-instrument result (mirrors ``check_margin`` exactly)."""

    # ``extra="allow"`` so a future field added to check_margin doesn't
    # trip response validation, and verbose=True payloads pass through.
    model_config = ConfigDict(extra="allow")

    status: str
    instrument_key: str | None = None
    underlying: str | None = None
    transaction_type: str | None = None
    lots: int | None = None
    lot_size: int | None = None
    lot_source: str | None = None
    quantity: int | None = None
    product: str | None = None
    required_margin: float | None = None
    final_margin: float | None = None
    error: str | None = None
    called_at: str | None = None
    response: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Internal helpers                                                            #
# --------------------------------------------------------------------------- #
def _run_one(req: MarginCheckRequest) -> dict[str, Any]:
    """Call ``check_margin`` for one request.

    Defensive catch-all so a single unexpected raise in ``bulk`` does
    not nuke the whole batch. ``check_margin`` itself already returns
    structured error dicts; this is purely belt-and-braces.
    """
    try:
        return check_margin(
            req.instrument_key,
            lots=req.lots,
            transaction_type=req.transaction_type,
            product=req.product,
            price=req.price,
            verbose=req.verbose,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "status": "error",
            "error": f"internal_error: {type(exc).__name__}",
            "instrument_key": req.instrument_key,
            "transaction_type": req.transaction_type,
            "lots": req.lots,
            "product": req.product,
        }


def _respond(result: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        content=result,
        status_code=_STATUS_CODE.get(result.get("status", "error"), 500),
    )


def _from_query(
    instrument_key: str,
    lots: int,
    transaction_type: str,
    product: str,
    price: float,
    verbose: bool,
) -> MarginCheckRequest:
    """Build a ``MarginCheckRequest`` from query-param values.

    Validation errors raised by Pydantic are turned into a structured
    ``invalid_input`` dict and a 400 response by the caller.
    """
    return MarginCheckRequest(
        instrument_key=instrument_key,
        lots=lots,
        transaction_type=transaction_type,
        product=product,
        price=price,
        verbose=verbose,
    )


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #
@router.get(
    "/single",
    response_model=MarginCheckResult,
    summary="Check margin for a single instrument (query params)",
    description=(
        "Each input is its own Swagger **query input box**. "
        "Defaults: `lots=1`, `transaction_type=BUY`, `product=D`, "
        "`price=0`, `verbose=false` — i.e. BUY × 1 lot × D. "
        "For NIFTY the default `lots=1` resolves to the exchange lot "
        "size via `lots_to_quantity`."
    ),
    responses={
        400: {"description": "invalid_input"},
        502: {"description": "upstox_error"},
        503: {"description": "no_token"},
    },
)
def check_single_get(
    instrument_key: str = Query(
        ...,
        min_length=1,
        description='Instrument key, e.g. "NSE_FO|47162".',
        examples=["NSE_FO|47162"],
    ),
    lots: int = Query(1, ge=1, description="Number of lots (default 1)."),
    transaction_type: str = Query(
        "BUY",
        description="BUY or SELL (default BUY). Case-insensitive.",
    ),
    product: str = Query(
        "D",
        description="D = intraday, I = delivery (default D). Case-insensitive.",
    ),
    price: float = Query(0.0, ge=0.0, description="Order price (default 0)."),
    verbose: bool = Query(
        False,
        description="Include a sanitised copy of the Upstox response.",
    ),
) -> JSONResponse:
    """Check the Upstox margin for one instrument via query string.

    Example::

        GET /margin/single?instrument_key=NSE_FO%7C47162
        GET /margin/single?instrument_key=NSE_FO%7C47162&lots=2&transaction_type=SELL
    """
    key = (instrument_key or "").strip()
    if not key:
        return JSONResponse(
            content={
                "status": "invalid_input",
                "error": "instrument_key is required",
            },
            status_code=400,
        )
    try:
        req = _from_query(
            instrument_key=key,
            lots=lots,
            transaction_type=transaction_type,
            product=product,
            price=price,
            verbose=verbose,
        )
    except Exception as exc:
        return JSONResponse(
            content={"status": "invalid_input", "error": str(exc)},
            status_code=400,
        )
    return _respond(_run_one(req))


@router.post(
    "/single",
    response_model=MarginCheckResult,
    summary="Check margin for a single instrument (JSON body)",
    description=(
        "Same check as `GET /margin/single`, but takes a JSON body. "
        "The body renderer in Swagger is a single editor; the GET "
        "form uses individual query input boxes."
    ),
    responses={
        400: {"description": "invalid_input"},
        502: {"description": "upstox_error"},
        503: {"description": "no_token"},
    },
)
def check_single_post(payload: MarginCheckRequest) -> JSONResponse:
    """Check the Upstox margin for one instrument via a JSON body."""
    return _respond(_run_one(payload))


@router.post(
    "/bulk",
    response_model=list[MarginCheckResult],
    summary="Check margin for multiple instruments",
    description=(
        "One entry per instrument. The response is a list of "
        "per-instrument results in the **same order** as the request, "
        "each carrying its own `status` field. HTTP status is always "
        "200 for the batch; individual failures live in the list "
        "items. Items are processed sequentially."
    ),
)
def check_bulk(payload: BulkMarginCheckRequest) -> list[dict[str, Any]]:
    """Check margin for many instruments in one request.

    Body::

        {
          "instruments": [
            {"instrument_key": "NSE_FO|47162"},                # BUY × 1 × D
            {"instrument_key": "NSE_FO|47163", "lots": 2},
            {"instrument_key": "NSE_FO|47164", "transaction_type": "SELL"},
            {"instrument_key": "NSE_FO|47165", "verbose": true}
          ]
        }
    """
    return [_run_one(item) for item in payload.instruments]


