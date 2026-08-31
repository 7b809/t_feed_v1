from copy import deepcopy
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.concurrency import run_in_threadpool

from core import config
from core.logger import get_logger
from services.algo_app_service import algo_app_service

logger = get_logger(__file__)

router = APIRouter(
    prefix="/algo-app",
    tags=["Algo App"],
)


def get_delivery_mode() -> str:
    return (
        "background"
        if bool(
            getattr(
                config,
                "ALGO_APP_SEND_IN_BACKGROUND",
                True,
            )
        )
        else "synchronous"
    )


def get_safe_algo_app_config() -> dict:
    auth_type = (
        str(
            getattr(
                config,
                "ALGO_APP_AUTH_TYPE",
                "none",
            )
            or "none"
        )
        .strip()
        .lower()
    )

    if auth_type == "none":
        authentication_configured = True

    elif auth_type == "bearer":
        authentication_configured = bool(
            str(
                getattr(
                    config,
                    "ALGO_APP_AUTH_TOKEN",
                    "",
                )
                or ""
            ).strip()
        )

    elif auth_type == "api_key":
        api_key = str(
            getattr(
                config,
                "ALGO_APP_API_KEY",
                "",
            )
            or ""
        ).strip()

        api_key_header = str(
            getattr(
                config,
                "ALGO_APP_API_KEY_HEADER",
                "X-API-Key",
            )
            or ""
        ).strip()

        authentication_configured = bool(api_key and api_key_header)

    else:
        authentication_configured = False

    return {
        "enabled": bool(
            getattr(
                config,
                "ALGO_APP_ENABLED",
                False,
            )
        ),
        "url_configured": bool(
            str(
                getattr(
                    config,
                    "ALGO_APP_URL",
                    "",
                )
                or ""
            ).strip()
        ),
        "auth_type": auth_type,
        "authentication_configured": (authentication_configured),
        "api_key_header_configured": bool(
            str(
                getattr(
                    config,
                    "ALGO_APP_API_KEY_HEADER",
                    "",
                )
                or ""
            ).strip()
        ),
        "timeout_seconds": float(
            getattr(
                config,
                "ALGO_APP_TIMEOUT_SECONDS",
                10.0,
            )
        ),
        "verify_ssl": bool(
            getattr(
                config,
                "ALGO_APP_VERIFY_SSL",
                True,
            )
        ),
        "max_retries": int(
            getattr(
                config,
                "ALGO_APP_MAX_RETRIES",
                3,
            )
        ),
        "retry_delay_seconds": float(
            getattr(
                config,
                "ALGO_APP_RETRY_DELAY_SECONDS",
                2.0,
            )
        ),
        "send_in_background": bool(
            getattr(
                config,
                "ALGO_APP_SEND_IN_BACKGROUND",
                True,
            )
        ),
        "background_queue_counts_as_accepted": bool(
            getattr(
                config,
                "ALGO_APP_BACKGROUND_QUEUE_COUNTS_AS_ACCEPTED",
                True,
            )
        ),
        "background_max_workers": int(
            getattr(
                config,
                "ALGO_APP_BACKGROUND_MAX_WORKERS",
                2,
            )
        ),
        "include_event_id": bool(
            getattr(
                config,
                "ALGO_APP_INCLUDE_EVENT_ID",
                True,
            )
        ),
        "payload_schema_version": str(
            getattr(
                config,
                "ALGO_APP_PAYLOAD_SCHEMA_VERSION",
                "1.0",
            )
        ),
        "source_name": str(
            getattr(
                config,
                "ALGO_APP_SOURCE_NAME",
                "option_feed_engine",
            )
        ),
        "maximum_response_body_length": int(
            getattr(
                config,
                "ALGO_APP_MAX_RESPONSE_BODY_LENGTH",
                2000,
            )
        ),
        "include_opening_range": bool(
            getattr(
                config,
                "EMA_ALGO_PAYLOAD_INCLUDE_OPENING_RANGE",
                True,
            )
        ),
        "include_ema_values": bool(
            getattr(
                config,
                "EMA_ALGO_PAYLOAD_INCLUDE_EMA_VALUES",
                True,
            )
        ),
        "include_ema_candle": bool(
            getattr(
                config,
                "EMA_ALGO_PAYLOAD_INCLUDE_CANDLE",
                True,
            )
        ),
        "include_nearest_instruments": bool(
            getattr(
                config,
                "EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENTS",
                True,
            )
        ),
        "include_nearest_instrument_candles": bool(
            getattr(
                config,
                "EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENT_CANDLES",
                True,
            )
        ),
        "include_budget_instruments": bool(
            getattr(
                config,
                "EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENTS",
                True,
            )
        ),
        "include_budget_instrument_candles": bool(
            getattr(
                config,
                "EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENT_CANDLES",
                True,
            )
        ),
        "include_delivery_metadata": bool(
            getattr(
                config,
                "EMA_ALGO_PAYLOAD_INCLUDE_DELIVERY_METADATA",
                False,
            )
        ),
        "include_raw_ema_event": bool(
            getattr(
                config,
                "EMA_ALGO_PAYLOAD_INCLUDE_RAW_EMA_EVENT",
                True,
            )
        ),
    }


def sanitize_sensitive_data(
    value: Any,
) -> Any:
    sensitive_fields = {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api_key",
        "apikey",
        "auth_token",
        "access_token",
        "refresh_token",
        "token",
        "secret",
        "password",
        "request_headers",
    }

    if isinstance(value, dict):
        output = {}

        for key, item in value.items():
            normalized_key = str(key).strip().lower()

            if normalized_key in sensitive_fields:
                continue

            output[str(key)] = sanitize_sensitive_data(item)

        return output

    if isinstance(
        value,
        (list, tuple),
    ):
        return [sanitize_sensitive_data(item) for item in value]

    return value


def get_safe_delivery_result(
    delivery_result: Any,
) -> dict | None:
    if not isinstance(
        delivery_result,
        dict,
    ):
        return None

    safe_result = sanitize_sensitive_data(deepcopy(delivery_result))

    return safe_result if isinstance(safe_result, dict) else None


def get_algo_app_status_payload() -> dict:
    service_status = algo_app_service.get_status()

    if not isinstance(
        service_status,
        dict,
    ):
        service_status = {}

    safe_service_status = sanitize_sensitive_data(service_status)

    return {
        "status": "success",
        "service": safe_service_status,
        "config": get_safe_algo_app_config(),
        "delivery_mode": get_delivery_mode(),
        "secrets_exposed": False,
    }


def normalize_request_payload(
    payload: dict,
    default_event_type: str | None = None,
) -> dict:
    normalized_payload = deepcopy(payload)

    if not normalized_payload.get("schema_version"):
        normalized_payload["schema_version"] = str(
            getattr(
                config,
                "ALGO_APP_PAYLOAD_SCHEMA_VERSION",
                "1.0",
            )
        )

    if not normalized_payload.get("source"):
        normalized_payload["source"] = str(
            getattr(
                config,
                "ALGO_APP_SOURCE_NAME",
                "option_feed_engine",
            )
        )

    if default_event_type and not normalized_payload.get("event_type"):
        normalized_payload["event_type"] = default_event_type

    return normalized_payload


def validate_request_body(
    payload: Any,
) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail=("Request body must contain " "a JSON object."),
        )

    if not payload:
        raise HTTPException(
            status_code=400,
            detail=("Request body must contain " "a non-empty JSON object."),
        )

    return payload


def validate_algo_app_configuration() -> None:
    if not bool(
        getattr(
            config,
            "ALGO_APP_ENABLED",
            False,
        )
    ):
        raise HTTPException(
            status_code=503,
            detail=("Algo App delivery is disabled."),
        )

    algo_app_url = str(
        getattr(
            config,
            "ALGO_APP_URL",
            "",
        )
        or ""
    ).strip()

    if not algo_app_url:
        raise HTTPException(
            status_code=503,
            detail=("Algo App delivery URL " "is not configured."),
        )

    auth_type = (
        str(
            getattr(
                config,
                "ALGO_APP_AUTH_TYPE",
                "none",
            )
            or "none"
        )
        .strip()
        .lower()
    )

    if auth_type == "none":
        return

    if auth_type == "bearer":
        auth_token = str(
            getattr(
                config,
                "ALGO_APP_AUTH_TOKEN",
                "",
            )
            or ""
        ).strip()

        if not auth_token:
            raise HTTPException(
                status_code=503,
                detail=("Algo App bearer token " "is not configured."),
            )

        return

    if auth_type == "api_key":
        api_key = str(
            getattr(
                config,
                "ALGO_APP_API_KEY",
                "",
            )
            or ""
        ).strip()

        api_key_header = str(
            getattr(
                config,
                "ALGO_APP_API_KEY_HEADER",
                "",
            )
            or ""
        ).strip()

        if not api_key or not api_key_header:
            raise HTTPException(
                status_code=503,
                detail=("Algo App API-key " "authentication is not " "configured."),
            )

        return

    raise HTTPException(
        status_code=503,
        detail=("Algo App authentication type " "is invalid."),
    )


def validate_numeric_value(
    value: Any,
    field_path: str,
) -> None:
    if value is None:
        return

    if isinstance(value, bool):
        raise HTTPException(
            status_code=422,
            detail=(f"{field_path} must be " "numeric or null."),
        )

    try:
        float(value)
    except (
        TypeError,
        ValueError,
        OverflowError,
    ) as ex:
        raise HTTPException(
            status_code=422,
            detail=(f"{field_path} must be " "numeric or null."),
        ) from ex


def validate_candle(
    candle: Any,
    field_path: str,
) -> None:
    if candle is None:
        return

    if not isinstance(candle, dict):
        raise HTTPException(
            status_code=422,
            detail=(f"{field_path} must be " "a JSON object or null."),
        )

    numeric_fields = (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_minus_low_points",
        "high_minus_low_points",
    )

    for field_name in numeric_fields:
        validate_numeric_value(
            candle.get(field_name),
            f"{field_path}.{field_name}",
        )


def validate_order_instrument(
    instrument: Any,
    field_path: str,
) -> None:
    if not isinstance(instrument, dict):
        raise HTTPException(
            status_code=422,
            detail=(f"{field_path} must be " "a JSON object."),
        )

    validate_numeric_value(
        instrument.get("strike_price"),
        f"{field_path}.strike_price",
    )

    validate_numeric_value(
        instrument.get("live_ltp"),
        f"{field_path}.live_ltp",
    )

    validate_numeric_value(
        instrument.get("minimum_budget_price"),
        (f"{field_path}." "minimum_budget_price"),
    )

    validate_numeric_value(
        instrument.get("maximum_budget_price"),
        (f"{field_path}." "maximum_budget_price"),
    )

    validate_numeric_value(
        instrument.get("distance_from_budget_midpoint"),
        (f"{field_path}." "distance_from_budget_midpoint"),
    )

    validate_numeric_value(
        instrument.get("distance_from_nifty"),
        (f"{field_path}." "distance_from_nifty"),
    )

    validate_candle(
        instrument.get("candle"),
        f"{field_path}.candle",
    )


def validate_ema_alert_payload(
    payload: dict,
) -> None:
    event_type = str(payload.get("event_type") or "").strip()

    if not event_type:
        raise HTTPException(
            status_code=422,
            detail="event_type is required.",
        )

    instrument = payload.get("instrument")

    if not isinstance(instrument, dict):
        raise HTTPException(
            status_code=422,
            detail=("instrument must be " "a JSON object."),
        )

    instrument_key = str(instrument.get("instrument_key") or "").strip()

    if not instrument_key:
        raise HTTPException(
            status_code=422,
            detail=("instrument.instrument_key " "is required."),
        )

    validate_numeric_value(
        instrument.get("strike_price"),
        "instrument.strike_price",
    )

    validate_numeric_value(
        instrument.get("live_ltp"),
        "instrument.live_ltp",
    )

    ema_payload = payload.get("ema")

    if ema_payload is not None and not isinstance(
        ema_payload,
        dict,
    ):
        raise HTTPException(
            status_code=422,
            detail=("ema must be a JSON object."),
        )

    if isinstance(ema_payload, dict):
        validate_candle(
            ema_payload.get("candle"),
            "ema.candle",
        )

    order_suggestion = payload.get("order_suggestion")

    if order_suggestion is None:
        return

    if not isinstance(
        order_suggestion,
        dict,
    ):
        raise HTTPException(
            status_code=422,
            detail=("order_suggestion must be " "a JSON object."),
        )

    nearest_instruments = order_suggestion.get(
        "nearest_instruments",
        [],
    )

    if not isinstance(
        nearest_instruments,
        list,
    ):
        raise HTTPException(
            status_code=422,
            detail=("order_suggestion." "nearest_instruments must " "be a list."),
        )

    for index, item in enumerate(nearest_instruments):
        validate_order_instrument(
            item,
            ("order_suggestion." f"nearest_instruments[{index}]"),
        )

    budget_filter = order_suggestion.get("budget_filter")

    if budget_filter is None:
        return

    if not isinstance(
        budget_filter,
        dict,
    ):
        raise HTTPException(
            status_code=422,
            detail=("order_suggestion.budget_filter " "must be a JSON object."),
        )

    validate_numeric_value(
        budget_filter.get("minimum_price"),
        ("order_suggestion.budget_filter." "minimum_price"),
    )

    validate_numeric_value(
        budget_filter.get("maximum_price"),
        ("order_suggestion.budget_filter." "maximum_price"),
    )

    budget_instruments = budget_filter.get(
        "instruments",
        [],
    )

    if not isinstance(
        budget_instruments,
        list,
    ):
        raise HTTPException(
            status_code=422,
            detail=("order_suggestion.budget_filter." "instruments must be a list."),
        )

    for index, item in enumerate(budget_instruments):
        validate_order_instrument(
            item,
            ("order_suggestion.budget_filter." f"instruments[{index}]"),
        )

    matched_count = budget_filter.get("matched_count")

    if matched_count is None:
        return

    if isinstance(matched_count, bool) or not isinstance(
        matched_count,
        int,
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "order_suggestion.budget_filter." "matched_count must be " "an integer."
            ),
        )

    if matched_count < 0:
        raise HTTPException(
            status_code=422,
            detail=(
                "order_suggestion.budget_filter." "matched_count cannot " "be negative."
            ),
        )

    if matched_count != len(budget_instruments):
        raise HTTPException(
            status_code=422,
            detail=(
                "order_suggestion.budget_filter."
                "matched_count must equal "
                "the number of instruments."
            ),
        )


def get_payload_counts(
    payload: dict,
) -> tuple[int, int]:
    order_suggestion = payload.get(
        "order_suggestion",
        {},
    )

    if not isinstance(
        order_suggestion,
        dict,
    ):
        return 0, 0

    nearest_instruments = order_suggestion.get(
        "nearest_instruments",
        [],
    )

    if not isinstance(
        nearest_instruments,
        list,
    ):
        nearest_instruments = []

    budget_filter = order_suggestion.get(
        "budget_filter",
        {},
    )

    if not isinstance(
        budget_filter,
        dict,
    ):
        budget_filter = {}

    budget_instruments = budget_filter.get(
        "instruments",
        [],
    )

    if not isinstance(
        budget_instruments,
        list,
    ):
        budget_instruments = []

    return (
        len(nearest_instruments),
        len(budget_instruments),
    )


@router.get("/status")
async def get_algo_app_status():
    try:
        return get_algo_app_status_payload()

    except Exception as ex:
        logger.error(
            "Algo App status request failed: " "%s: %s",
            type(ex).__name__,
            ex,
        )

        raise HTTPException(
            status_code=500,
            detail=("Could not retrieve Algo App " "service status."),
        ) from ex


@router.get("/config")
async def get_algo_app_config():
    return {
        "status": "success",
        "config": get_safe_algo_app_config(),
        "secrets_exposed": False,
    }


@router.get("/delivery-status")
async def get_algo_app_delivery_status():
    try:
        service_status = algo_app_service.get_status()

        if not isinstance(
            service_status,
            dict,
        ):
            service_status = {}

        return {
            "status": "success",
            "delivery_mode": (get_delivery_mode()),
            "background_queue_counts_as_accepted": (
                service_status.get(
                    "background_queue_counts_as_accepted",
                    True,
                )
            ),
            "background_max_workers": (
                service_status.get(
                    "background_max_workers",
                    2,
                )
            ),
            "dispatch_count": (
                service_status.get(
                    "dispatch_count",
                    0,
                )
            ),
            "background_dispatch_count": (
                service_status.get(
                    "background_dispatch_count",
                    0,
                )
            ),
            "delivery_attempt_count": (
                service_status.get(
                    "delivery_attempt_count",
                    0,
                )
            ),
            "delivery_success_count": (
                service_status.get(
                    "delivery_success_count",
                    0,
                )
            ),
            "delivery_failed_count": (
                service_status.get(
                    "delivery_failed_count",
                    0,
                )
            ),
            "retry_count": (
                service_status.get(
                    "retry_count",
                    0,
                )
            ),
            "pending_count": (
                service_status.get(
                    "pending_count",
                    0,
                )
            ),
            "last_dispatch_at": (service_status.get("last_dispatch_at")),
            "last_success_at": (service_status.get("last_success_at")),
            "last_failure_at": (service_status.get("last_failure_at")),
            "last_event_id": (service_status.get("last_event_id")),
            "last_status_code": (service_status.get("last_status_code")),
            "last_error": (service_status.get("last_error")),
            "last_response": (
                sanitize_sensitive_data(service_status.get("last_response"))
            ),
            "last_delivery_result": (
                get_safe_delivery_result(service_status.get("last_delivery_result"))
            ),
            "market_time": (service_status.get("market_time")),
            "secrets_exposed": False,
        }

    except Exception as ex:
        logger.error(
            "Algo App delivery status request " "failed: %s: %s",
            type(ex).__name__,
            ex,
        )

        raise HTTPException(
            status_code=500,
            detail=("Could not retrieve Algo App " "delivery status."),
        ) from ex


@router.post("/dispatch")
async def dispatch_algo_app_payload(
    payload: dict = Body(...),
):
    validate_request_body(payload)
    validate_algo_app_configuration()

    normalized_payload = normalize_request_payload(payload)

    validate_ema_alert_payload(normalized_payload)

    event_id = normalized_payload.get("event_id")

    try:
        accepted = await run_in_threadpool(
            algo_app_service.dispatch_ema_alert,
            normalized_payload,
        )

        if not accepted:
            logger.error(
                "Algo App payload dispatch was " "not accepted. event_id=%s",
                event_id,
            )

            raise HTTPException(
                status_code=502,
                detail=("Algo App payload was not " "accepted for delivery."),
            )

        delivery_mode = get_delivery_mode()

        nearest_count, budget_count = get_payload_counts(normalized_payload)

        logger.info(
            "Algo App payload accepted. "
            "event_id=%s, mode=%s, "
            "nearest_count=%s, "
            "budget_count=%s",
            event_id,
            delivery_mode,
            nearest_count,
            budget_count,
        )

        return {
            "status": "accepted",
            "accepted": True,
            "event_id": event_id,
            "delivery_mode": delivery_mode,
            "delivery_completed": (delivery_mode == "synchronous"),
            "nearest_instruments_count": (nearest_count),
            "budget_instruments_count": (budget_count),
            "message": (
                "Payload queued for Algo App " "delivery."
                if delivery_mode == "background"
                else "Payload delivered to Algo App."
            ),
        }

    except HTTPException:
        raise

    except Exception as ex:
        logger.error(
            "Algo App payload dispatch failed. " "event_id=%s, error=%s: %s",
            event_id,
            type(ex).__name__,
            ex,
        )

        raise HTTPException(
            status_code=500,
            detail=("Algo App payload dispatch failed: " f"{type(ex).__name__}: {ex}"),
        ) from ex


@router.post("/send")
async def send_algo_app_payload(
    payload: dict = Body(...),
):
    validate_request_body(payload)
    validate_algo_app_configuration()

    normalized_payload = normalize_request_payload(payload)

    validate_ema_alert_payload(normalized_payload)

    event_id = normalized_payload.get("event_id")

    try:
        result = await run_in_threadpool(
            algo_app_service.send_ema_alert,
            normalized_payload,
        )

        safe_result = get_safe_delivery_result(result)

        if not isinstance(
            safe_result,
            dict,
        ):
            raise HTTPException(
                status_code=502,
                detail=("Algo App delivery returned " "an invalid result."),
            )

        if not bool(safe_result.get("success")):
            logger.error(
                "Synchronous Algo App delivery "
                "failed. event_id=%s, "
                "status_code=%s, error=%s",
                event_id,
                safe_result.get("status_code"),
                safe_result.get("error"),
            )

            raise HTTPException(
                status_code=502,
                detail={
                    "message": ("Algo App delivery failed."),
                    "event_id": event_id,
                    "delivery": safe_result,
                },
            )

        nearest_count, budget_count = get_payload_counts(normalized_payload)

        logger.info(
            "Synchronous Algo App delivery "
            "completed. event_id=%s, "
            "status_code=%s",
            event_id,
            safe_result.get("status_code"),
        )

        return {
            "status": "success",
            "event_id": event_id,
            "delivery_mode": "synchronous",
            "delivery_completed": True,
            "nearest_instruments_count": (nearest_count),
            "budget_instruments_count": (budget_count),
            "delivery": safe_result,
        }

    except HTTPException:
        raise

    except Exception as ex:
        logger.error(
            "Synchronous Algo App delivery " "failed. event_id=%s, error=%s: %s",
            event_id,
            type(ex).__name__,
            ex,
        )

        raise HTTPException(
            status_code=500,
            detail=("Algo App delivery failed: " f"{type(ex).__name__}: {ex}"),
        ) from ex


@router.post("/ema-alert")
async def dispatch_ema_alert_payload(
    payload: dict = Body(...),
):
    validate_request_body(payload)
    validate_algo_app_configuration()

    normalized_payload = normalize_request_payload(
        payload,
        default_event_type=("isolated_instrument_ema_alert"),
    )

    validate_ema_alert_payload(normalized_payload)

    event_id = normalized_payload.get("event_id")

    try:
        accepted = await run_in_threadpool(
            algo_app_service.dispatch_ema_alert,
            normalized_payload,
        )

        if not accepted:
            logger.error(
                "EMA alert payload was not " "accepted. event_id=%s",
                event_id,
            )

            raise HTTPException(
                status_code=502,
                detail=("EMA alert payload was not " "accepted for Algo App delivery."),
            )

        delivery_mode = get_delivery_mode()

        nearest_count, budget_count = get_payload_counts(normalized_payload)

        logger.info(
            "EMA alert accepted for delivery. "
            "event_id=%s, mode=%s, "
            "nearest_count=%s, "
            "budget_count=%s",
            event_id,
            delivery_mode,
            nearest_count,
            budget_count,
        )

        return {
            "status": "accepted",
            "accepted": True,
            "event_id": event_id,
            "event_type": (normalized_payload.get("event_type")),
            "delivery_mode": delivery_mode,
            "delivery_completed": (delivery_mode == "synchronous"),
            "nearest_instruments_count": (nearest_count),
            "budget_instruments_count": (budget_count),
        }

    except HTTPException:
        raise

    except Exception as ex:
        logger.error(
            "EMA alert Algo App dispatch failed. " "event_id=%s, error=%s: %s",
            event_id,
            type(ex).__name__,
            ex,
        )

        raise HTTPException(
            status_code=500,
            detail=("EMA alert dispatch failed: " f"{type(ex).__name__}: {ex}"),
        ) from ex


@router.get("/health")
async def get_algo_app_health():
    try:
        service_status = algo_app_service.get_status()

        if not isinstance(
            service_status,
            dict,
        ):
            service_status = {}

        enabled = bool(service_status.get("enabled"))

        configured = bool(service_status.get("configured"))

        pending_count = int(
            service_status.get(
                "pending_count",
                0,
            )
            or 0
        )

        last_success_at = service_status.get("last_success_at")

        last_failure_at = service_status.get("last_failure_at")

        last_error = service_status.get("last_error")

        if not enabled:
            health_status = "disabled"

        elif not configured:
            health_status = "not_configured"

        elif pending_count > 0:
            health_status = "processing"

        elif last_error and last_failure_at and not last_success_at:
            health_status = "delivery_error"

        elif (
            last_error
            and last_failure_at
            and last_success_at
            and str(last_failure_at) > str(last_success_at)
        ):
            health_status = "delivery_error"

        else:
            health_status = "healthy"

        return {
            "status": health_status,
            "enabled": enabled,
            "configured": configured,
            "url_configured": bool(service_status.get("url_configured")),
            "authentication_configured": bool(
                service_status.get("authentication_configured")
            ),
            "delivery_mode": (get_delivery_mode()),
            "pending_count": pending_count,
            "delivery_success_count": (
                service_status.get(
                    "delivery_success_count",
                    0,
                )
            ),
            "delivery_failed_count": (
                service_status.get(
                    "delivery_failed_count",
                    0,
                )
            ),
            "retry_count": (
                service_status.get(
                    "retry_count",
                    0,
                )
            ),
            "last_success_at": (last_success_at),
            "last_failure_at": (last_failure_at),
            "last_error": last_error,
            "market_time": (service_status.get("market_time")),
            "secrets_exposed": False,
        }

    except Exception as ex:
        logger.error(
            "Algo App health request failed: " "%s: %s",
            type(ex).__name__,
            ex,
        )

        raise HTTPException(
            status_code=500,
            detail=("Could not retrieve Algo App " "health status."),
        ) from ex


__all__ = [
    "router",
    "get_delivery_mode",
    "get_safe_algo_app_config",
    "sanitize_sensitive_data",
    "get_safe_delivery_result",
    "get_algo_app_status_payload",
    "normalize_request_payload",
    "validate_request_body",
    "validate_algo_app_configuration",
    "validate_numeric_value",
    "validate_candle",
    "validate_order_instrument",
    "validate_ema_alert_payload",
    "get_payload_counts",
]
