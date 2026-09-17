from copy import deepcopy
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from core.logger import get_logger
from services.option_service import options_cache
from upstox_services.process_order import (
    process_selected_instrument,
)

logger = get_logger(__file__)

router = APIRouter(
    prefix="/api/order",
    tags=["Sandbox Order"],
)


def normalize_option_type(
    value: Any,
) -> str | None:
    if value is None:
        return None

    normalized = str(value).strip().upper()

    if normalized in {
        "CE",
        "CALL",
        "C",
    }:
        return "CE"

    if normalized in {
        "PE",
        "PUT",
        "P",
    }:
        return "PE"

    return None


def safe_float(
    value: Any,
) -> float | None:
    try:
        if value is None:
            return None

        return float(value)

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return None


def get_loaded_instruments() -> list:
    instruments = options_cache.get(
        "data",
        [],
    )

    if not isinstance(
        instruments,
        list,
    ):
        return []

    return instruments


def find_instrument_by_key(
    instrument_key: str,
) -> dict[str, Any] | None:
    instruments = get_loaded_instruments()

    normalized_key = str(instrument_key or "").strip()

    for instrument in instruments:
        if not isinstance(
            instrument,
            dict,
        ):
            continue

        current_key = str(instrument.get("instrument_key") or "").strip()

        if current_key == normalized_key:
            return deepcopy(instrument)

    return None


def find_instrument_by_strike(
    strike: float,
    striketype: str,
) -> dict[str, Any] | None:
    instruments = get_loaded_instruments()

    target_type = normalize_option_type(striketype)

    if not target_type:
        raise HTTPException(
            status_code=422,
            detail=("Invalid striketype. " "Allowed values: CE, PE."),
        )

    nearest_expiry = options_cache.get("nearest_expiry")

    matches = []

    for instrument in instruments:
        if not isinstance(
            instrument,
            dict,
        ):
            continue

        strike_price = safe_float(instrument.get("strike_price"))

        option_type = normalize_option_type(
            instrument.get("instrument_type") or instrument.get("option_type")
        )

        if strike_price == float(strike) and option_type == target_type:
            matches.append(deepcopy(instrument))

    if not matches:
        return None

    if nearest_expiry:
        for instrument in matches:
            if (
                str(instrument.get("expiry") or "").strip()
                == str(nearest_expiry).strip()
            ):
                return instrument

    return matches[0]


def build_selected_instrument(
    instrument: dict[str, Any],
) -> dict[str, Any]:
    return {
        "instrument_key": instrument.get("instrument_key"),
        "trading_symbol": instrument.get("trading_symbol"),
        "live_ltp": (
            instrument.get("live_ltp")
            if instrument.get("live_ltp") is not None
            else instrument.get("ltp")
        ),
        "lot_size": instrument.get("lot_size"),
    }


@router.get("/place")
def place_order_get(
    instrument_key: str | None = Query(
        default=None,
    ),
    strike: float | None = Query(
        default=None,
    ),
    striketype: str | None = Query(
        default=None,
    ),
):
    return _place_order(
        instrument_key=instrument_key,
        strike=strike,
        striketype=striketype,
    )


@router.post("/place")
def place_order_post(
    instrument_key: str | None = Query(
        default=None,
    ),
    strike: float | None = Query(
        default=None,
    ),
    striketype: str | None = Query(
        default=None,
    ),
):
    return _place_order(
        instrument_key=instrument_key,
        strike=strike,
        striketype=striketype,
    )


def _place_order(
    *,
    instrument_key: str | None,
    strike: float | None,
    striketype: str | None,
):
    try:
        instruments = get_loaded_instruments()

        if not instruments:
            raise HTTPException(
                status_code=503,
                detail=("Options cache is empty. " "Refresh instruments first."),
            )

        instrument = None

        if instrument_key:
            instrument = find_instrument_by_key(instrument_key)

        elif strike is not None and striketype:
            instrument = find_instrument_by_strike(
                strike=strike,
                striketype=striketype,
            )

        else:
            raise HTTPException(
                status_code=400,
                detail=("Provide either instrument_key " "or strike + striketype."),
            )

        if not instrument:
            logger.warning(
                "Manual sandbox order lookup failed. "
                "instrument_key=%s, strike=%s, "
                "striketype=%s",
                instrument_key,
                strike,
                striketype,
            )

            raise HTTPException(
                status_code=404,
                detail={
                    "success": False,
                    "error_code": "INSTRUMENT_NOT_FOUND",
                    "instrument_key": instrument_key,
                    "strike": strike,
                    "striketype": striketype,
                    "message": ("Matching instrument " "was not found."),
                },
            )

        selected_instrument = build_selected_instrument(instrument)

        logger.info(
            "Manual sandbox order API request. " "instrument_key=%s, trading_symbol=%s",
            selected_instrument.get("instrument_key"),
            selected_instrument.get("trading_symbol"),
        )

        try:
            workflow_result = process_selected_instrument(selected_instrument)

        except Exception as exc:
            logger.exception(
                "Manual sandbox order workflow failed. " "instrument_key=%s",
                selected_instrument.get("instrument_key"),
            )

            raise HTTPException(
                status_code=500,
                detail={
                    "success": False,
                    "error_code": ("ORDER_WORKFLOW_EXCEPTION"),
                    "message": str(exc),
                },
            )

        workflow_success = bool(
            isinstance(
                workflow_result,
                dict,
            )
            and workflow_result.get("success")
        )

        return {
            "success": workflow_success,
            "resolved_instrument": instrument,
            "selected_instrument": (selected_instrument),
            "order_workflow": (workflow_result),
        }

    except HTTPException:
        raise

    except Exception as exc:
        logger.exception(
            "Unexpected sandbox order API failure. "
            "instrument_key=%s, strike=%s, "
            "striketype=%s",
            instrument_key,
            strike,
            striketype,
        )

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "error_code": ("UNEXPECTED_ORDER_API_EXCEPTION"),
                "message": str(exc),
            },
        )
