from __future__ import annotations

import asyncio
import inspect
from datetime import date, datetime
from typing import Any, Callable

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    status,
)
from pydantic import BaseModel, Field

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

DEBUG_MODE = False


def _debug(
    message: str,
    *args: Any,
) -> None:
    if not DEBUG_MODE:
        return

    if args:
        message = message.format(*args)

    print(f"[EMA API DEBUG] {message}")


class ApiResponse(BaseModel):
    status: str
    message: str | None = None
    data: Any = None


class HardRefreshRequest(BaseModel):
    force: bool = True

    trigger: str = Field(
        default="ema_api",
        min_length=1,
        max_length=100,
    )


def _get_runtime(
    request: Request,
):
    runtime = getattr(
        request.app.state,
        "ema_runtime",
        None,
    )

    if runtime is None:
        runtime = getattr(
            request.app.state,
            "runtime",
            None,
        )

    if runtime is None:
        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            detail=(
                "EMA runtime is not initialized"
            ),
        )

    return runtime


def _get_scheduler(
    request: Request,
):
    scheduler = getattr(
        request.app.state,
        "ema_scheduler",
        None,
    )

    if scheduler is None:
        scheduler = getattr(
            request.app.state,
            "scheduler",
            None,
        )

    if scheduler is None:
        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            detail=(
                "EMA scheduler is not initialized"
            ),
        )

    return scheduler


def _get_query_service(
    request: Request,
):
    query_service = getattr(
        request.app.state,
        "ema_query_service",
        None,
    )

    if query_service is None:
        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            detail=(
                "EMA query service is not initialized"
            ),
        )

    return query_service


def _serialize(
    value: Any,
) -> Any:
    if value is None:
        return None

    if isinstance(
        value,
        (datetime, date),
    ):
        return value.isoformat()

    if isinstance(value, dict):
        return {
            str(key): _serialize(item)
            for key, item in value.items()
        }

    if isinstance(
        value,
        (list, tuple, set),
    ):
        return [
            _serialize(item)
            for item in value
        ]

    if hasattr(value, "model_dump"):
        try:
            return _serialize(
                value.model_dump(
                    mode="json"
                )
            )
        except TypeError:
            return _serialize(
                value.model_dump()
            )

    if hasattr(value, "dict"):
        return _serialize(
            value.dict()
        )

    if hasattr(value, "to_dict"):
        return _serialize(
            value.to_dict()
        )

    return value


async def _call_service(
    method: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    _debug(
        "Calling service method: {}",
        getattr(
            method,
            "__name__",
            repr(method),
        ),
    )

    if inspect.iscoroutinefunction(method):
        return await method(
            *args,
            **kwargs,
        )

    result = await asyncio.to_thread(
        method,
        *args,
        **kwargs,
    )

    if inspect.isawaitable(result):
        return await result

    return result


def _service_method(
    service: Any,
    *method_names: str,
):
    for method_name in method_names:
        method = getattr(
            service,
            method_name,
            None,
        )

        if callable(method):
            return method

    return None


def _runtime_contracts(
    runtime: Any,
) -> list:
    contracts = getattr(
        runtime,
        "contracts",
        [],
    )

    if not isinstance(contracts, list):
        return []

    return [
        contract
        for contract in contracts
        if isinstance(contract, dict)
    ]


def _runtime_states(
    runtime: Any,
) -> dict:
    states = getattr(
        runtime,
        "states",
        {},
    )

    if not isinstance(states, dict):
        return {}

    return states


def _normalize_cross_scope(
    value: str,
) -> str:
    normalized = str(
        value or "all"
    ).strip().lower()

    aliases = {
        "historical": "historical",
        "historic": "historical",
        "history": "historical",
        "intraday": "intraday",
        "intra_day": "intraday",
        "today": "intraday",
        "live": "intraday",
        "all": "all",
        "both": "all",
    }

    result = aliases.get(normalized)

    if result is None:
        raise HTTPException(
            status_code=(
                status.HTTP_400_BAD_REQUEST
            ),
            detail=(
                "cross_scope must be "
                "historical, intraday, or all"
            ),
        )

    return result


def _normalize_cross_type(
    value: str | None,
) -> str | None:
    if value is None:
        return None

    normalized = str(value).strip().lower()

    if not normalized:
        return None

    if normalized not in {
        "bullish",
        "bearish",
    }:
        raise HTTPException(
            status_code=(
                status.HTTP_400_BAD_REQUEST
            ),
            detail=(
                "cross_type must be "
                "bullish or bearish"
            ),
        )

    return normalized


def _validate_time_range(
    start_time: datetime | None,
    end_time: datetime | None,
) -> None:
    if (
        start_time is not None
        and end_time is not None
        and start_time > end_time
    ):
        raise HTTPException(
            status_code=(
                status.HTTP_400_BAD_REQUEST
            ),
            detail=(
                "start_time cannot be later "
                "than end_time"
            ),
        )


def _raise_service_error(
    operation: str,
    exception: Exception,
) -> None:
    if isinstance(exception, HTTPException):
        raise exception

    if isinstance(exception, ValueError):
        raise HTTPException(
            status_code=(
                status.HTTP_400_BAD_REQUEST
            ),
            detail=str(exception),
        ) from exception

    logger.exception(
        "%s failed",
        operation,
    )

    raise HTTPException(
        status_code=(
            status.HTTP_500_INTERNAL_SERVER_ERROR
        ),
        detail=f"{operation} failed: {exception}",
    ) from exception


def create_ema_router(
    runtime: Any = None,
    scheduler: Any = None,
    query_service: Any = None,
) -> APIRouter:
    router = APIRouter(
        prefix=getattr(
            config,
            "EMA_API_PREFIX",
            "/ema/apis",
        ),
        tags=["EMA"],
    )

    api_default_limit = getattr(
        config,
        "EMA_API_DEFAULT_LIMIT",
        100,
    )

    api_max_limit = getattr(
        config,
        "EMA_API_MAX_LIMIT",
        1000,
    )

    @router.get(
        "/health",
        response_model=ApiResponse,
        summary="EMA service health",
    )
    async def ema_health(
        request: Request,
    ) -> ApiResponse:
        active_runtime = (
            runtime
            or _get_runtime(request)
        )

        active_scheduler = (
            scheduler
            or _get_scheduler(request)
        )

        contracts = _runtime_contracts(
            active_runtime
        )

        states = _runtime_states(
            active_runtime
        )

        stop_event = getattr(
            active_scheduler,
            "stop_event",
            None,
        )

        scheduler_running = (
            not stop_event.is_set()
            if stop_event is not None
            else None
        )

        quote_api_initialized = (
            getattr(
                active_runtime,
                "quote_api",
                None,
            )
            is not None
        )

        initialized_states = len(states)
        total_contracts = len(contracts)

        if (
            quote_api_initialized
            and scheduler_running is not False
            and (
                total_contracts == 0
                or initialized_states
                == total_contracts
            )
        ):
            service_status = "healthy"
        else:
            service_status = "degraded"

        health_data = {
            "service": "ema",
            "status": service_status,
            "timestamp": datetime.now(
                config.MARKET_TIMEZONE
            ).isoformat(),
            "market_timezone": str(
                config.MARKET_TIMEZONE
            ),
            "scheduler_running": (
                scheduler_running
            ),
            "quote_api_initialized": (
                quote_api_initialized
            ),
            "total_contracts": (
                total_contracts
            ),
            "initialized_states": (
                initialized_states
            ),
            "ema_fast_period": (
                config.EMA_FAST_PERIOD
            ),
            "ema_slow_period": (
                config.EMA_SLOW_PERIOD
            ),
            "historical_crosses_enabled": (
                getattr(
                    config,
                    "SAVE_HISTORICAL_CROSSES",
                    True,
                )
            ),
            "intraday_crosses_enabled": (
                getattr(
                    config,
                    "SAVE_INTRADAY_CROSSES",
                    True,
                )
            ),
            "crosses_root": str(
                getattr(
                    config,
                    "EMA_CROSS_ROOT",
                    "",
                )
            ),
        }

        return ApiResponse(
            status="success",
            data=health_data,
        )

    @router.get(
        "/state",
        response_model=ApiResponse,
        summary="Get current EMA runtime state",
    )
    async def ema_state(
        request: Request,
        instrument_key: str | None = Query(
            default=None,
            description=(
                "Optional instrument key"
            ),
        ),
    ) -> ApiResponse:
        active_runtime = (
            runtime
            or _get_runtime(request)
        )

        contracts = _runtime_contracts(
            active_runtime
        )

        states = _runtime_states(
            active_runtime
        )

        contract_map = {
            contract.get(
                "instrument_key"
            ): contract
            for contract in contracts
            if contract.get(
                "instrument_key"
            )
        }

        if instrument_key:
            state_value = states.get(
                instrument_key
            )

            if state_value is None:
                raise HTTPException(
                    status_code=(
                        status.HTTP_404_NOT_FOUND
                    ),
                    detail=(
                        "EMA state not found for "
                        f"instrument: {instrument_key}"
                    ),
                )

            return ApiResponse(
                status="success",
                data={
                    "instrument_key": (
                        instrument_key
                    ),
                    "contract": _serialize(
                        contract_map.get(
                            instrument_key,
                            {},
                        )
                    ),
                    "state": _serialize(
                        state_value
                    ),
                },
            )

        instruments: list[dict] = []

        for contract in contracts:
            key = contract.get(
                "instrument_key"
            )

            if not key:
                continue

            instruments.append(
                {
                    "instrument_key": key,
                    "trading_symbol": (
                        contract.get(
                            "trading_symbol"
                        )
                    ),
                    "exchange": contract.get(
                        "exchange"
                    ),
                    "segment": contract.get(
                        "segment"
                    ),
                    "strike_price": (
                        contract.get(
                            "strike_price"
                        )
                    ),
                    "option_type": (
                        contract.get(
                            "option_type"
                        )
                    ),
                    "state": _serialize(
                        states.get(key)
                    ),
                }
            )

        return ApiResponse(
            status="success",
            data={
                "timestamp": datetime.now(
                    config.MARKET_TIMEZONE
                ).isoformat(),
                "total_contracts": len(
                    contracts
                ),
                "initialized_states": len(
                    states
                ),
                "instruments": instruments,
            },
        )

    @router.get(
        "/contracts",
        response_model=ApiResponse,
        summary="Get selected EMA contracts",
    )
    async def ema_contracts(
        request: Request,
    ) -> ApiResponse:
        active_runtime = (
            runtime
            or _get_runtime(request)
        )

        contracts = _runtime_contracts(
            active_runtime
        )

        return ApiResponse(
            status="success",
            data={
                "total": len(contracts),
                "contracts": _serialize(
                    contracts
                ),
            },
        )

    @router.get(
        "/latest",
        response_model=ApiResponse,
        summary="Get latest EMA state",
    )
    async def latest_ema_state(
        request: Request,
        instrument_key: str | None = Query(
            default=None,
        ),
    ) -> ApiResponse:
        active_query_service = (
            query_service
            or _get_query_service(request)
        )

        try:
            if instrument_key:
                method = _service_method(
                    active_query_service,
                    "get_latest_state",
                )

                if method is None:
                    raise HTTPException(
                        status_code=(
                            status.HTTP_501_NOT_IMPLEMENTED
                        ),
                        detail=(
                            "Latest EMA state query "
                            "is not implemented"
                        ),
                    )

                result = await _call_service(
                    method,
                    instrument_key,
                )

                if result is None:
                    raise HTTPException(
                        status_code=(
                            status.HTTP_404_NOT_FOUND
                        ),
                        detail=(
                            "Latest EMA state not found "
                            f"for instrument: "
                            f"{instrument_key}"
                        ),
                    )

            else:
                method = _service_method(
                    active_query_service,
                    "get_current_state",
                )

                if method is None:
                    raise HTTPException(
                        status_code=(
                            status.HTTP_501_NOT_IMPLEMENTED
                        ),
                        detail=(
                            "Current EMA state query "
                            "is not implemented"
                        ),
                    )

                result = await _call_service(
                    method
                )

            return ApiResponse(
                status="success",
                data=_serialize(result),
            )

        except Exception as ex:
            _raise_service_error(
                "Latest EMA state query",
                ex,
            )

    @router.get(
        "/history",
        response_model=ApiResponse,
        summary="Get historical EMA data",
    )
    async def ema_history(
        request: Request,
        instrument_key: str = Query(
            ...,
            min_length=1,
        ),
        trading_date: date | None = Query(
            default=None,
        ),
        start_time: datetime | None = Query(
            default=None,
        ),
        end_time: datetime | None = Query(
            default=None,
        ),
        limit: int = Query(
            default=api_default_limit,
            ge=1,
            le=api_max_limit,
        ),
        offset: int = Query(
            default=0,
            ge=0,
        ),
    ) -> ApiResponse:
        _validate_time_range(
            start_time,
            end_time,
        )

        active_query_service = (
            query_service
            or _get_query_service(request)
        )

        method = _service_method(
            active_query_service,
            "get_historical_ema",
            "historical_ema",
            "get_ema_history",
            "get_history",
        )

        if method is None:
            raise HTTPException(
                status_code=(
                    status.HTTP_501_NOT_IMPLEMENTED
                ),
                detail=(
                    "Historical EMA query is "
                    "not implemented"
                ),
            )

        try:
            result = await _call_service(
                method,
                instrument_key=instrument_key,
                trading_date=trading_date,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                offset=offset,
            )

            return ApiResponse(
                status="success",
                data=_serialize(result),
            )

        except Exception as ex:
            _raise_service_error(
                "Historical EMA query",
                ex,
            )

    @router.get(
        "/intraday",
        response_model=ApiResponse,
        summary="Get intraday EMA data",
    )
    async def intraday_ema(
        request: Request,
        instrument_key: str = Query(
            ...,
            min_length=1,
        ),
        trading_date: date | None = Query(
            default=None,
        ),
        start_time: datetime | None = Query(
            default=None,
        ),
        end_time: datetime | None = Query(
            default=None,
        ),
        limit: int = Query(
            default=api_default_limit,
            ge=1,
            le=api_max_limit,
        ),
        offset: int = Query(
            default=0,
            ge=0,
        ),
    ) -> ApiResponse:
        _validate_time_range(
            start_time,
            end_time,
        )

        active_query_service = (
            query_service
            or _get_query_service(request)
        )

        method = _service_method(
            active_query_service,
            "get_intraday_ema",
            "intraday_ema",
            "get_ema_history",
            "get_history",
        )

        if method is None:
            raise HTTPException(
                status_code=(
                    status.HTTP_501_NOT_IMPLEMENTED
                ),
                detail=(
                    "Intraday EMA query is "
                    "not implemented"
                ),
            )

        try:
            result = await _call_service(
                method,
                instrument_key=instrument_key,
                trading_date=trading_date,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                offset=offset,
            )

            return ApiResponse(
                status="success",
                data=_serialize(result),
            )

        except Exception as ex:
            _raise_service_error(
                "Intraday EMA query",
                ex,
            )

    @router.get(
        "/crossovers",
        response_model=ApiResponse,
        summary="Get EMA crossover records",
    )
    async def ema_crossovers(
        request: Request,
        instrument_key: str | None = Query(
            default=None,
        ),
        trading_date: date | None = Query(
            default=None,
        ),
        cross_scope: str = Query(
            default="all",
            description=(
                "historical, intraday, or all"
            ),
        ),
        cross_type: str | None = Query(
            default=None,
            description=(
                "bullish or bearish"
            ),
        ),
        start_time: datetime | None = Query(
            default=None,
        ),
        end_time: datetime | None = Query(
            default=None,
        ),
        limit: int = Query(
            default=api_default_limit,
            ge=1,
            le=api_max_limit,
        ),
        offset: int = Query(
            default=0,
            ge=0,
        ),
    ) -> ApiResponse:
        _validate_time_range(
            start_time,
            end_time,
        )

        normalized_scope = (
            _normalize_cross_scope(
                cross_scope
            )
        )

        normalized_cross_type = (
            _normalize_cross_type(
                cross_type
            )
        )

        active_query_service = (
            query_service
            or _get_query_service(request)
        )

        method = _service_method(
            active_query_service,
            "get_crossovers",
            "crossovers",
            "get_ema_crossovers",
        )

        if method is None:
            raise HTTPException(
                status_code=(
                    status.HTTP_501_NOT_IMPLEMENTED
                ),
                detail=(
                    "Crossover query is "
                    "not implemented"
                ),
            )

        try:
            result = await _call_service(
                method,
                instrument_key=instrument_key,
                trading_date=trading_date,
                cross_scope=normalized_scope,
                cross_type=normalized_cross_type,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                offset=offset,
            )

            return ApiResponse(
                status="success",
                data=_serialize(result),
            )

        except Exception as ex:
            _raise_service_error(
                "EMA crossover query",
                ex,
            )

    @router.get(
        "/query",
        response_model=ApiResponse,
        summary="Unified EMA query",
    )
    async def unified_ema_query(
        request: Request,
        instrument_key: str | None = Query(
            default=None,
        ),
        trading_date: date | None = Query(
            default=None,
        ),
        data_type: str = Query(
            default="state",
            description=(
                "state, history, intraday, "
                "or crossovers"
            ),
        ),
        cross_scope: str = Query(
            default="all",
            description=(
                "historical, intraday, or all"
            ),
        ),
        cross_type: str | None = Query(
            default=None,
            description=(
                "bullish or bearish"
            ),
        ),
        start_time: datetime | None = Query(
            default=None,
        ),
        end_time: datetime | None = Query(
            default=None,
        ),
        limit: int = Query(
            default=api_default_limit,
            ge=1,
            le=api_max_limit,
        ),
        offset: int = Query(
            default=0,
            ge=0,
        ),
    ) -> ApiResponse:
        _validate_time_range(
            start_time,
            end_time,
        )

        normalized_data_type = str(
            data_type
        ).strip().lower()

        allowed_data_types = {
            "state",
            "current",
            "latest",
            "live",
            "history",
            "historical",
            "intraday",
            "intra_day",
            "crossovers",
            "crosses",
            "signals",
        }

        if (
            normalized_data_type
            not in allowed_data_types
        ):
            raise HTTPException(
                status_code=(
                    status.HTTP_400_BAD_REQUEST
                ),
                detail=(
                    "data_type must be state, "
                    "history, intraday, or "
                    "crossovers"
                ),
            )

        normalized_scope = (
            _normalize_cross_scope(
                cross_scope
            )
        )

        normalized_cross_type = (
            _normalize_cross_type(
                cross_type
            )
        )

        active_query_service = (
            query_service
            or _get_query_service(request)
        )

        method = _service_method(
            active_query_service,
            "query",
            "execute_query",
        )

        if method is None:
            raise HTTPException(
                status_code=(
                    status.HTTP_501_NOT_IMPLEMENTED
                ),
                detail=(
                    "Unified EMA query is "
                    "not implemented"
                ),
            )

        try:
            result = await _call_service(
                method,
                data_type=normalized_data_type,
                instrument_key=instrument_key,
                trading_date=trading_date,
                cross_scope=normalized_scope,
                cross_type=normalized_cross_type,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                offset=offset,
            )

            return ApiResponse(
                status="success",
                data=_serialize(result),
            )

        except Exception as ex:
            _raise_service_error(
                "Unified EMA query",
                ex,
            )

    @router.post(
        "/hard-refresh",
        response_model=ApiResponse,
        summary="Trigger EMA hard refresh",
    )
    async def hard_refresh(
        request: Request,
        payload: (
            HardRefreshRequest
            | None
        ) = None,
    ) -> ApiResponse:
        active_scheduler = (
            scheduler
            or _get_scheduler(request)
        )

        refresh_payload = (
            payload
            or HardRefreshRequest()
        )

        method = _service_method(
            active_scheduler,
            "refresh_day",
            "hard_refresh",
            "refresh",
        )

        if method is None:
            raise HTTPException(
                status_code=(
                    status.HTTP_501_NOT_IMPLEMENTED
                ),
                detail=(
                    "Hard refresh is not "
                    "implemented"
                ),
            )

        now = datetime.now(
            config.MARKET_TIMEZONE
        )

        try:
            result = await _call_service(
                method,
                now=now,
                force=refresh_payload.force,
                trigger=(
                    refresh_payload.trigger
                ),
            )

            result_status = (
                result.get("status")
                if isinstance(result, dict)
                else None
            )

            if result_status == "already_running":
                raise HTTPException(
                    status_code=(
                        status.HTTP_409_CONFLICT
                    ),
                    detail=(
                        "A refresh is already "
                        "in progress"
                    ),
                )

            return ApiResponse(
                status="success",
                message=(
                    "EMA hard refresh completed"
                ),
                data=_serialize(result),
            )

        except Exception as ex:
            _raise_service_error(
                "EMA hard refresh",
                ex,
            )

    return router


router = create_ema_router()