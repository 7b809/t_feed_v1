from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

# ---------------------------------------------------------------------------
# Debug Configuration
# ---------------------------------------------------------------------------

DEBUG_MODE = False


def _debug(message: str, *args: Any) -> None:
    """
    Print debug information only when DEBUG_MODE is enabled.
    """
    if not DEBUG_MODE:
        return

    if args:
        message = message.format(*args)

    print(f"[EMA API DEBUG] {message}")


# ---------------------------------------------------------------------------
# Response Models
# ---------------------------------------------------------------------------

class ApiResponse(BaseModel):
    status: str
    message: str | None = None
    data: Any = None


class HardRefreshRequest(BaseModel):
    force: bool = False
    trigger: str = Field(
        default="api",
        min_length=1,
        max_length=100,
    )


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------

def _get_runtime(request: Request):
    """
    Resolve the EMA runtime from FastAPI application state.
    """
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
        _debug("EMA runtime is not initialized in app.state")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="EMA runtime is not initialized",
        )

    _debug("EMA runtime resolved")

    return runtime


def _get_scheduler(request: Request):
    """
    Resolve the scheduler from FastAPI application state.
    """
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
        _debug("EMA scheduler is not initialized in app.state")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="EMA scheduler is not initialized",
        )

    _debug("EMA scheduler resolved")

    return scheduler


def _get_query_service(request: Request):
    """
    Resolve the read-only EMA query service.
    """
    query_service = getattr(
        request.app.state,
        "ema_query_service",
        None,
    )

    if query_service is None:
        _debug("EMA query service is not initialized in app.state")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="EMA query service is not initialized",
        )

    _debug("EMA query service resolved")

    return query_service


def _get_state_path() -> Any:
    """
    Return the service state JSON path.
    """
    return (
        config.RUNTIME_ROOT
        / "service_state.json"
    )


def _serialize(value: Any) -> Any:
    """
    Convert common Python objects into JSON-compatible values.
    """
    if value is None:
        return None

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, dict):
        return {
            str(key): _serialize(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [
            _serialize(item)
            for item in value
        ]

    if hasattr(value, "model_dump"):
        return _serialize(
            value.model_dump()
        )

    if hasattr(value, "dict"):
        return _serialize(
            value.dict()
        )

    return value


async def _call_service(
    method,
    *args,
    **kwargs,
):
    """
    Execute synchronous repository/service methods in a worker thread.

    Async methods are also supported.
    """
    _debug(
        "Calling service method: {}",
        getattr(method, "__name__", repr(method)),
    )

    result = await asyncio.to_thread(
        method,
        *args,
        **kwargs,
    )

    if hasattr(result, "__await__"):
        result = await result

    return result


def _service_method(
    service,
    *method_names: str,
):
    """
    Return the first available method from a service.
    """
    for method_name in method_names:
        method = getattr(
            service,
            method_name,
            None,
        )

        if callable(method):
            _debug(
                "Resolved service method '{}'",
                method_name,
            )
            return method

    _debug(
        "None of the service methods {} were found",
        method_names,
    )

    return None


def _runtime_contracts(
    runtime,
) -> list[dict]:
    """
    Safely return the currently loaded contracts.
    """
    contracts = getattr(
        runtime,
        "contracts",
        [],
    )

    if not isinstance(contracts, list):
        return []

    return contracts


def _runtime_states(
    runtime,
) -> dict:
    """
    Safely return the currently loaded EMA states.
    """
    states = getattr(
        runtime,
        "states",
        {},
    )

    if not isinstance(states, dict):
        return {}

    return states


# ---------------------------------------------------------------------------
# Router Factory
# ---------------------------------------------------------------------------

def create_ema_router(
    runtime=None,
    scheduler=None,
    query_service=None,
) -> APIRouter:
    """
    Create the EMA REST API router.

    Dependencies may be supplied directly, or resolved through
    request.app.state at request time.
    """
    router = APIRouter(
        prefix=getattr(
            config,
            "EMA_API_PREFIX",
            "/api/ema",
        ),
        tags=["EMA"],
    )

    # -----------------------------------------------------------------------
    # Health
    # -----------------------------------------------------------------------

    @router.get(
        "/health",
        response_model=ApiResponse,
        summary="EMA service health",
    )
    async def ema_health(
        request: Request,
    ):
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

        health_data = {
            "service": "ema",
            "status": (
                "healthy"
                if quote_api_initialized
                else "degraded"
            ),
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
            "total_contracts": len(
                contracts
            ),
            "initialized_states": len(
                states
            ),
            "ema_fast_period": getattr(
                config,
                "EMA_FAST_PERIOD",
                9,
            ),
            "ema_slow_period": getattr(
                config,
                "EMA_SLOW_PERIOD",
                21,
            ),
        }

        _debug("Health check data: {}", health_data)

        return ApiResponse(
            status="success",
            data=health_data,
        )

    # -----------------------------------------------------------------------
    # Runtime State
    # -----------------------------------------------------------------------

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
    ):
        _debug("State request for instrument_key={}", instrument_key)

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
            state = states.get(
                instrument_key
            )

            if state is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        "EMA state not found for "
                        f"instrument: {instrument_key}"
                    ),
                )

            contract = contract_map.get(
                instrument_key,
                {},
            )

            return ApiResponse(
                status="success",
                data={
                    "instrument_key": (
                        instrument_key
                    ),
                    "contract": _serialize(
                        contract
                    ),
                    "state": _serialize(
                        state
                    ),
                },
            )

        instruments = []

        for contract in contracts:
            key = contract.get(
                "instrument_key"
            )

            if not key:
                continue

            instruments.append(
                {
                    "instrument_key": key,
                    "trading_symbol": contract.get(
                        "trading_symbol"
                    ),
                    "exchange": contract.get(
                        "exchange"
                    ),
                    "segment": contract.get(
                        "segment"
                    ),
                    "state": _serialize(
                        states.get(key)
                    ),
                }
            )

        data = {
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
        }

        return ApiResponse(
            status="success",
            data=data,
        )

    # -----------------------------------------------------------------------
    # Contracts
    # -----------------------------------------------------------------------

    @router.get(
        "/contracts",
        response_model=ApiResponse,
        summary="Get selected EMA contracts",
    )
    async def ema_contracts(
        request: Request,
    ):
        active_runtime = (
            runtime
            or _get_runtime(request)
        )

        contracts = _runtime_contracts(
            active_runtime
        )

        _debug("Returning {} contracts", len(contracts))

        return ApiResponse(
            status="success",
            data={
                "total": len(
                    contracts
                ),
                "contracts": _serialize(
                    contracts
                ),
            },
        )

    # -----------------------------------------------------------------------
    # Latest State
    # -----------------------------------------------------------------------

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
    ):
        active_runtime = (
            runtime
            or _get_runtime(request)
        )

        states = _runtime_states(
            active_runtime
        )

        if instrument_key:
            state = states.get(
                instrument_key
            )

            if state is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        "Latest EMA state not found "
                        f"for instrument: {instrument_key}"
                    ),
                )

            return ApiResponse(
                status="success",
                data={
                    "instrument_key": (
                        instrument_key
                    ),
                    "state": _serialize(
                        state
                    ),
                },
            )

        latest = {
            key: _serialize(value)
            for key, value in states.items()
        }

        return ApiResponse(
            status="success",
            data={
                "total": len(
                    latest
                ),
                "states": latest,
            },
        )

    # -----------------------------------------------------------------------
    # Historical EMA Query
    # -----------------------------------------------------------------------

    @router.get(
        "/history",
        response_model=ApiResponse,
        summary="Get historical EMA candles",
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
        limit: int = Query(
            default=100,
            ge=1,
            le=5000,
        ),
    ):
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
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail=(
                    "Historical EMA query is not "
                    "implemented by the query service"
                ),
            )

        kwargs = {
            "instrument_key": instrument_key,
            "limit": limit,
        }

        if trading_date is not None:
            kwargs["trading_date"] = trading_date

        result = await _call_service(
            method,
            **kwargs,
        )

        return ApiResponse(
            status="success",
            data=_serialize(
                result
            ),
        )

    # -----------------------------------------------------------------------
    # Intraday EMA Query
    # -----------------------------------------------------------------------

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
        limit: int = Query(
            default=500,
            ge=1,
            le=5000,
        ),
    ):
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
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail=(
                    "Intraday EMA query is not "
                    "implemented by the query service"
                ),
            )

        kwargs = {
            "instrument_key": instrument_key,
            "limit": limit,
        }

        if trading_date is not None:
            kwargs["trading_date"] = trading_date

        result = await _call_service(
            method,
            **kwargs,
        )

        return ApiResponse(
            status="success",
            data=_serialize(
                result
            ),
        )

    # -----------------------------------------------------------------------
    # Crossovers
    # -----------------------------------------------------------------------

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
        cross_type: str | None = Query(
            default=None,
            description=(
                "BULLISH or BEARISH"
            ),
        ),
        limit: int = Query(
            default=100,
            ge=1,
            le=5000,
        ),
    ):
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
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail=(
                    "Crossover query is not "
                    "implemented by the query service"
                ),
            )

        kwargs = {
            "limit": limit,
        }

        if instrument_key is not None:
            kwargs["instrument_key"] = (
                instrument_key
            )

        if trading_date is not None:
            kwargs["trading_date"] = (
                trading_date
            )

        if cross_type is not None:
            kwargs["cross_type"] = (
                cross_type.upper()
            )

        result = await _call_service(
            method,
            **kwargs,
        )

        return ApiResponse(
            status="success",
            data=_serialize(
                result
            ),
        )

    # -----------------------------------------------------------------------
    # Unified Query
    # -----------------------------------------------------------------------

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
                "state, history, intraday, crossovers"
            ),
        ),
        limit: int = Query(
            default=100,
            ge=1,
            le=5000,
        ),
    ):
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
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail=(
                    "Unified EMA query is not "
                    "implemented by the query service"
                ),
            )

        kwargs = {
            "data_type": data_type,
            "limit": limit,
        }

        if instrument_key is not None:
            kwargs["instrument_key"] = (
                instrument_key
            )

        if trading_date is not None:
            kwargs["trading_date"] = (
                trading_date
            )

        result = await _call_service(
            method,
            **kwargs,
        )

        return ApiResponse(
            status="success",
            data=_serialize(
                result
            ),
        )

    # -----------------------------------------------------------------------
    # Hard Refresh
    # -----------------------------------------------------------------------

    @router.post(
        "/hard-refresh",
        response_model=ApiResponse,
        summary="Trigger EMA hard refresh",
    )
    async def hard_refresh(
        request: Request,
        payload: HardRefreshRequest | None = None,
    ):
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
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail=(
                    "Hard refresh is not "
                    "implemented by the scheduler"
                ),
            )

        now = datetime.now(
            config.MARKET_TIMEZONE
        )

        kwargs = {
            "now": now,
            "force": refresh_payload.force,
            "trigger": refresh_payload.trigger,
        }

        _debug(
            "Hard refresh requested: force={}, trigger={}",
            refresh_payload.force,
            refresh_payload.trigger,
        )

        try:
            result = await _call_service(
                method,
                **kwargs,
            )

            return ApiResponse(
                status="success",
                message=(
                    "EMA hard refresh completed"
                ),
                data=_serialize(
                    result
                ),
            )

        except Exception as ex:
            logger.exception(
                "EMA hard refresh API failed"
            )

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"EMA hard refresh failed: {ex}"
                ),
            ) from ex

    return router


# ---------------------------------------------------------------------------
# Default Router
# ---------------------------------------------------------------------------

router = create_ema_router()