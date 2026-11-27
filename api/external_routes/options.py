"""
External option-chain routes.

    POST /options/contracts   – nearest-expiry CE+PE contracts within a
                                pre-mapped strike range around the underlying
                                spot. Includes LTP, volume, OI, bid/ask,
                                greeks (delta, gamma, theta, vega, iv, pop)
                                and optional contracts metadata.
    POST /options/expiries    – all available expiries for an underlying.
"""
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api.external_routes._common import raise_from_service_result
from core.logger import get_logger
from sandbox_services.option_contract_service import (
    get_available_expiries,
    get_nearest_expiry_option_contracts,
)
from sandbox_services.budget_option_order_service import (
    place_option_orders_by_budget,
)

logger = get_logger(__file__)

router = APIRouter(
    prefix="/options",
    tags=["external-options"],
    responses={500: {"description": "Internal option-chain service error"}},
)


class OptionContractsRequest(BaseModel):
    """Request schema for fetching nearest-expiry option chain contracts."""

    model_config = ConfigDict(extra="allow")  # allows body.api_token

    underlying_key: str = Field(
        ...,
        min_length=1,
        description="Underlying index key, e.g. 'NSE_INDEX|Nifty 50' or URL-encoded.",
    )
    expiry: Optional[str] = Field(
        default=None,
        description=(
            "Upstox expiry keyword or YYYY-MM-DD. "
            "Defaults to auto-discovery (current_week → next_week → …)."
        ),
    )
    offset_override: Optional[float] = Field(
        default=None,
        ge=0,
        description="Override the pre-mapped ±strike range (e.g. 1500 for a wider NIFTY band).",
    )
    include_market_data: bool = Field(
        default=True,
        description="Include ltp / volume / oi / bid/ask / prev_oi per side.",
    )
    include_greeks: bool = Field(
        default=True,
        description="Include vega / theta / gamma / delta / iv / pop per side.",
    )
    include_metadata: bool = Field(
        default=True,
        description="Merge trading_symbol, lot_size, tick_size, weekly, segment, exchange from contracts endpoint.",
    )


class AvailableExpiriesRequest(BaseModel):
    """Request schema for listing all available expiries for an underlying."""

    model_config = ConfigDict(extra="allow")  # allows body.api_token

    underlying_key: str = Field(
        ...,
        min_length=1,
        description="Underlying index key, e.g. 'NSE_INDEX|Nifty 50' or URL-encoded.",
    )


class BudgetOptionOrdersRequest(BaseModel):
    """Request schema for premium-filtered option-order preview/execution."""

    model_config = ConfigDict(extra="allow")  # allows body.api_token

    underlying_key: Optional[str] = Field(
        default=None,
        description="Underlying key or alias (NIFTY, SENSEX, BANKNIFTY, etc.); defaults to NIFTY.",
    )
    budget_min: float = Field(..., gt=0, description="Minimum option premium (inclusive).")
    budget_max: float = Field(..., gt=0, description="Maximum option premium (inclusive).")
    option_type: str = Field(..., description="CE or PE.")
    expiry: Optional[str] = Field(default=None, description="Expiry keyword or YYYY-MM-DD; auto-discovered when omitted.")
    offset_override: Optional[float] = Field(default=None, ge=0, description="Optional underlying strike-range override.")
    execute: bool = Field(default=False, description="False returns a read-only preview without placing orders.")
    max_orders: int = Field(default=1, ge=1, le=10, description="Maximum matching contracts to submit; hard-capped at 10.")
    lot: int = Field(default=1, ge=1, description="Number of lots per selected contract.")
    transaction_type: str = Field(default="BUY", description="BUY or SELL.")
    product: str = Field(default="D", description="D or I.")
    order_type: str = Field(default="MARKET", description="Defaults to MARKET; LIMIT uses selected option-chain LTP as its limit price.")
    validity: str = Field(default="DAY", description="DAY or IOC.")
    is_amo: bool = False
    tag: Optional[str] = Field(default=None, max_length=20)
    slice_order: bool = True
    strict_lot_size: bool = Field(default=True, description="Require a valid lot size; contract metadata is preferred.")


@router.post("/contracts")
def external_option_contracts(body: OptionContractsRequest):
    """
    Fetch nearest-expiry option chain and return CE + PE contracts within a
    pre-mapped strike window around the underlying spot.

    Each entry contains:
        identity      : instrument_key, expiry, strike_price, instrument_type
        relative      : moneyness (ITM/ATM/OTM), strike_offset, strike_pcr
        market_data   : ltp, volume, oi, close_price, bid_price, bid_qty,
                        ask_price, ask_qty, prev_oi       [include_market_data]
        greeks        : vega, theta, gamma, delta, iv, pop  [include_greeks]
        metadata      : segment, exchange, trading_symbol, tick_size, lot_size,
                        freeze_quantity, minimum_lot, weekly  [include_metadata]
    """
    try:
        result = get_nearest_expiry_option_contracts(
            underlying_key=body.underlying_key,
            expiry=body.expiry,
            offset_override=body.offset_override,
            include_market_data=body.include_market_data,
            include_greeks=body.include_greeks,
            include_metadata=body.include_metadata,
        )
        raise_from_service_result(result, action="option-contracts")
        return result
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected option-contract service failure")
        raise HTTPException(
            status_code=500,
            detail={"error": "Unexpected option-contract service error."},
        ) from None


@router.post("/expiries")
def external_option_expiries(body: AvailableExpiriesRequest):
    """
    Return every available expiry for the given underlying, nearest first.
    """
    try:
        result = get_available_expiries(underlying_key=body.underlying_key)
        raise_from_service_result(result, action="option-expiries")
        return result
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected option-expiry service failure")
        raise HTTPException(
            status_code=500,
            detail={"error": "Unexpected option-expiry service error."},
        ) from None

@router.post("/place-by-budget")
def external_place_option_orders_by_budget(body: BudgetOptionOrdersRequest):
    """
    Preview or place option orders selected by contract premium.

    The budget range applies to each option contract's option-chain LTP, not
    the combined lot value or margin. execute=false is a read-only preview.
    """
    try:
        result = place_option_orders_by_budget(
            underlying_key=body.underlying_key,
            budget_min=body.budget_min,
            budget_max=body.budget_max,
            option_type=body.option_type,
            expiry=body.expiry,
            offset_override=body.offset_override,
            execute=body.execute,
            max_orders=body.max_orders,
            lot=body.lot,
            transaction_type=body.transaction_type,
            product=body.product,
            order_type=body.order_type,
            validity=body.validity,
            is_amo=body.is_amo,
            tag=body.tag,
            slice_order=body.slice_order,
            strict_lot_size=body.strict_lot_size,
        )
        raise_from_service_result(
            result,
            action="budget-option-orders",
            accepted_statuses=("PREVIEW", "NO_MATCHES"),
        )
        if result.get("status") == "PARTIAL_SUCCESS":
            return JSONResponse(status_code=207, content=result)
        return result
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected budget-filtered option-order failure")
        raise HTTPException(
            status_code=500,
            detail={"error": "Unexpected budget-filtered option-order error."},
        ) from None
