from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.logger import get_logger
from services.ema_alert_simulation_service import (
    EmaAlertSimulationError,
    ema_alert_simulation_service,
)

logger = get_logger(__file__)

router = APIRouter(
    prefix="/api/ema-alerts",
    tags=["EMA Alert Simulation"],
)


class EmaAlertSimulationRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    instrument_key: str = Field(
        min_length=1,
        max_length=250,
    )
    cross_type: str
    price: float | None = Field(
        default=None,
        gt=0,
    )
    candle: dict[str, Any] | None = None
    dry_run: bool = True
    send_telegram: bool = False
    send_algo_app: bool = False
    requested_by: str = Field(
        default="ema_simulation_api",
        min_length=1,
        max_length=100,
    )

    @field_validator("instrument_key")
    @classmethod
    def validate_instrument_key(cls, value: str) -> str:
        normalized_value = str(value or "").strip()

        if not normalized_value:
            raise ValueError("instrument_key is required.")

        return normalized_value

    @field_validator("cross_type")
    @classmethod
    def validate_cross_type(cls, value: str) -> str:
        normalized_value = str(value or "").strip().lower()

        cross_type_mapping = {
            "bullish": "bullish_cross",
            "bullish_cross": "bullish_cross",
            "buy": "bullish_cross",
            "long": "bullish_cross",
            "up": "bullish_cross",
            "bearish": "bearish_cross",
            "bearish_cross": "bearish_cross",
            "sell": "bearish_cross",
            "short": "bearish_cross",
            "down": "bearish_cross",
        }

        normalized_cross_type = cross_type_mapping.get(normalized_value)

        if normalized_cross_type is None:
            raise ValueError(
                "cross_type must be bullish_cross or bearish_cross."
            )

        return normalized_cross_type

    @field_validator("candle")
    @classmethod
    def validate_candle(
        cls,
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if value is None:
            return None

        allowed_fields = {
            "timestamp",
            "timestamp_ms",
            "open",
            "high",
            "low",
            "close",
            "volume",
        }

        unsupported_fields = set(value) - allowed_fields

        if unsupported_fields:
            unsupported_text = ", ".join(
                sorted(unsupported_fields)
            )
            raise ValueError(
                f"Unsupported candle fields: {unsupported_text}."
            )

        normalized_candle = dict(value)

        for field_name in (
            "open",
            "high",
            "low",
            "close",
            "volume",
        ):
            field_value = normalized_candle.get(field_name)

            if field_value is None:
                continue

            try:
                numeric_value = float(field_value)
            except (TypeError, ValueError, OverflowError) as ex:
                raise ValueError(
                    f"candle.{field_name} must be numeric."
                ) from ex

            if field_name == "volume":
                if numeric_value < 0:
                    raise ValueError(
                        "candle.volume must be greater than or equal to zero."
                    )
            elif numeric_value <= 0:
                raise ValueError(
                    f"candle.{field_name} must be greater than zero."
                )

            normalized_candle[field_name] = numeric_value

        open_price = normalized_candle.get("open")
        high_price = normalized_candle.get("high")
        low_price = normalized_candle.get("low")
        close_price = normalized_candle.get("close")

        if (
            high_price is not None
            and low_price is not None
            and high_price < low_price
        ):
            raise ValueError(
                "candle.high cannot be lower than candle.low."
            )

        comparable_prices = [
            item
            for item in (open_price, close_price)
            if item is not None
        ]

        if (
            high_price is not None
            and comparable_prices
            and high_price < max(comparable_prices)
        ):
            raise ValueError(
                "candle.high cannot be lower than "
                "candle.open or candle.close."
            )

        if (
            low_price is not None
            and comparable_prices
            and low_price > min(comparable_prices)
        ):
            raise ValueError(
                "candle.low cannot be higher than "
                "candle.open or candle.close."
            )

        return normalized_candle

    @field_validator("requested_by")
    @classmethod
    def validate_requested_by(cls, value: str) -> str:
        normalized_value = str(value or "").strip()

        if not normalized_value:
            raise ValueError("requested_by cannot be empty.")

        return normalized_value


class EmaAlertSimulationResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    success: bool
    simulation: bool = True
    dry_run: bool
    instrument_key: str
    cross_type: str
    message: str | None = None
    ema_event: dict[str, Any] | None = None
    result: dict[str, Any] | None = None


def build_error_detail(
    request: EmaAlertSimulationRequest,
    error: str,
) -> dict:
    return {
        "success": False,
        "simulation": True,
        "dry_run": request.dry_run,
        "instrument_key": request.instrument_key,
        "cross_type": request.cross_type,
        "error": error,
    }


@router.post(
    "/simulate",
    response_model=EmaAlertSimulationResponse,
    status_code=status.HTTP_200_OK,
    summary="Simulate an EMA crossover alert",
)
def simulate_ema_alert(
    request: EmaAlertSimulationRequest,
) -> dict:
    logger.info(
        "EMA alert simulation request received. "
        "instrument_key=%s, cross_type=%s, price=%s, "
        "dry_run=%s, send_telegram=%s, send_algo_app=%s, "
        "requested_by=%s",
        request.instrument_key,
        request.cross_type,
        request.price,
        request.dry_run,
        request.send_telegram,
        request.send_algo_app,
        request.requested_by,
    )

    if request.dry_run and (
        request.send_telegram or request.send_algo_app
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=build_error_detail(
                request=request,
                error=(
                    "send_telegram and send_algo_app must be false "
                    "when dry_run is true."
                ),
            ),
        )

    if not request.dry_run and not (
        request.send_telegram or request.send_algo_app
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=build_error_detail(
                request=request,
                error=(
                    "At least one delivery option must be enabled "
                    "when dry_run is false."
                ),
            ),
        )

    try:
        result = ema_alert_simulation_service.simulate(
            instrument_key=request.instrument_key,
            cross_type=request.cross_type,
            price=request.price,
            candle=request.candle,
            dry_run=request.dry_run,
            send_telegram=request.send_telegram,
            send_algo_app=request.send_algo_app,
            requested_by=request.requested_by,
        )

        if not isinstance(result, dict):
            logger.error(
                "EMA simulation service returned an invalid result. "
                "instrument_key=%s, result_type=%s",
                request.instrument_key,
                type(result).__name__,
            )

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=build_error_detail(
                    request=request,
                    error=(
                        "EMA simulation service returned "
                        "an invalid result."
                    ),
                ),
            )

        response = dict(result)

        response.setdefault("success", False)
        response.setdefault("simulation", True)
        response.setdefault("dry_run", request.dry_run)
        response.setdefault(
            "instrument_key",
            request.instrument_key,
        )
        response.setdefault(
            "cross_type",
            request.cross_type,
        )

        if response.get("success"):
            if request.dry_run:
                response.setdefault(
                    "message",
                    "EMA alert simulation dry-run completed successfully.",
                )
            else:
                response.setdefault(
                    "message",
                    "EMA alert simulation completed successfully.",
                )
        else:
            response.setdefault(
                "message",
                "EMA alert simulation was not accepted.",
            )

        processing_result = response.get("result")
        event_id = None

        if isinstance(processing_result, dict):
            event_id = processing_result.get("event_id")

        logger.info(
            "EMA alert simulation request completed. "
            "instrument_key=%s, cross_type=%s, success=%s, "
            "dry_run=%s, event_id=%s",
            request.instrument_key,
            request.cross_type,
            response.get("success"),
            request.dry_run,
            event_id,
        )

        return response

    except EmaAlertSimulationError as ex:
        logger.warning(
            "EMA alert simulation request rejected. "
            "instrument_key=%s, cross_type=%s, error=%s",
            request.instrument_key,
            request.cross_type,
            ex,
        )

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=build_error_detail(
                request=request,
                error=str(ex),
            ),
        ) from ex

    except HTTPException:
        raise

    except Exception as ex:
        logger.exception(
            "Unexpected EMA alert simulation failure. "
            "instrument_key=%s, cross_type=%s, error=%s: %s",
            request.instrument_key,
            request.cross_type,
            type(ex).__name__,
            ex,
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=build_error_detail(
                request=request,
                error="Unexpected EMA alert simulation failure.",
            ),
        ) from ex