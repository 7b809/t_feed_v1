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


# ============================================================
# Response Helpers
# ============================================================


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

    authentication_configured = auth_type == "none"

    if auth_type == "bearer":
        authentication_configured = bool(
            getattr(
                config,
                "ALGO_APP_AUTH_TOKEN",
                "",
            )
        )

    if auth_type == "api_key":
        authentication_configured = bool(
            getattr(
                config,
                "ALGO_APP_API_KEY",
                "",
            )
        )

    return {
        "enabled": bool(
            getattr(
                config,
                "ALGO_APP_ENABLED",
                False,
            )
        ),
        "url_configured": bool(
            getattr(
                config,
                "ALGO_APP_URL",
                "",
            )
        ),
        "auth_type": auth_type,
        "authentication_configured": (authentication_configured),
        "api_key_header_configured": bool(
            getattr(
                config,
                "ALGO_APP_API_KEY_HEADER",
                "",
            )
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
    }


def get_safe_delivery_result(
    delivery_result: Any,
) -> dict | None:
    if not isinstance(
        delivery_result,
        dict,
    ):
        return None

    safe_result = deepcopy(delivery_result)

    safe_result.pop(
        "request_headers",
        None,
    )

    safe_result.pop(
        "authorization",
        None,
    )

    safe_result.pop(
        "api_key",
        None,
    )

    safe_result.pop(
        "auth_token",
        None,
    )

    return safe_result


def get_algo_app_status_payload() -> dict:
    service_status = algo_app_service.get_status()

    if not isinstance(
        service_status,
        dict,
    ):
        service_status = {}

    return {
        "status": "success",
        "service": service_status,
        "config": get_safe_algo_app_config(),
        "delivery_mode": (
            "background"
            if bool(
                getattr(
                    config,
                    "ALGO_APP_SEND_IN_BACKGROUND",
                    True,
                )
            )
            else "synchronous"
        ),
        "secrets_exposed": False,
    }


def normalize_request_payload(
    payload: dict,
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

    return normalized_payload


# ============================================================
# Status Routes
# ============================================================


@router.get("/status")
async def get_algo_app_status():
    try:
        return get_algo_app_status_payload()

    except Exception as ex:
        logger.error(
            "Algo App status request failed: %s: %s",
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

        return {
            "status": "success",
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
            "retry_count": service_status.get(
                "retry_count",
                0,
            ),
            "pending_count": service_status.get(
                "pending_count",
                0,
            ),
            "last_dispatch_at": (service_status.get("last_dispatch_at")),
            "last_success_at": (service_status.get("last_success_at")),
            "last_failure_at": (service_status.get("last_failure_at")),
            "last_event_id": (service_status.get("last_event_id")),
            "last_status_code": (service_status.get("last_status_code")),
            "last_error": service_status.get("last_error"),
            "last_response": service_status.get("last_response"),
            "market_time": service_status.get("market_time"),
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


# ============================================================
# Background Dispatch Route
# ============================================================


@router.post("/dispatch")
async def dispatch_algo_app_payload(
    payload: dict = Body(...),
):
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(
            status_code=400,
            detail=("Request body must contain a " "non-empty JSON object."),
        )

    if not bool(
        getattr(
            config,
            "ALGO_APP_ENABLED",
            False,
        )
    ):
        raise HTTPException(
            status_code=503,
            detail="Algo App delivery is disabled.",
        )

    if not bool(
        getattr(
            config,
            "ALGO_APP_URL",
            "",
        )
    ):
        raise HTTPException(
            status_code=503,
            detail=("Algo App delivery URL is not " "configured."),
        )

    normalized_payload = normalize_request_payload(payload)

    event_id = normalized_payload.get("event_id")

    try:
        accepted = await run_in_threadpool(
            algo_app_service.dispatch_ema_alert,
            normalized_payload,
        )

        if not accepted:
            logger.error(
                "Algo App payload dispatch was not " "accepted. event_id=%s",
                event_id,
            )

            raise HTTPException(
                status_code=502,
                detail=("Algo App payload was not accepted " "for delivery."),
            )

        logger.info(
            "Algo App payload accepted for dispatch. " "event_id=%s",
            event_id,
        )

        return {
            "status": "accepted",
            "accepted": True,
            "event_id": event_id,
            "delivery_mode": (
                "background"
                if bool(
                    getattr(
                        config,
                        "ALGO_APP_SEND_IN_BACKGROUND",
                        True,
                    )
                )
                else "synchronous"
            ),
            "message": ("Payload accepted for Algo App " "delivery."),
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


# ============================================================
# Synchronous Delivery Route
# ============================================================


@router.post("/send")
async def send_algo_app_payload(
    payload: dict = Body(...),
):
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(
            status_code=400,
            detail=("Request body must contain a " "non-empty JSON object."),
        )

    if not bool(
        getattr(
            config,
            "ALGO_APP_ENABLED",
            False,
        )
    ):
        raise HTTPException(
            status_code=503,
            detail="Algo App delivery is disabled.",
        )

    if not bool(
        getattr(
            config,
            "ALGO_APP_URL",
            "",
        )
    ):
        raise HTTPException(
            status_code=503,
            detail=("Algo App delivery URL is not " "configured."),
        )

    normalized_payload = normalize_request_payload(payload)

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
                detail=("Algo App delivery returned an " "invalid result."),
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


# ============================================================
# EMA Alert Dispatch Route
# ============================================================


@router.post("/ema-alert")
async def dispatch_ema_alert_payload(
    payload: dict = Body(...),
):
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(
            status_code=400,
            detail=("EMA alert payload must be a " "non-empty JSON object."),
        )

    normalized_payload = normalize_request_payload(payload)

    if not normalized_payload.get("event_type"):
        normalized_payload["event_type"] = "isolated_instrument_ema_alert"

    event_id = normalized_payload.get("event_id")

    try:
        accepted = await run_in_threadpool(
            algo_app_service.dispatch_ema_alert,
            normalized_payload,
        )

        if not accepted:
            raise HTTPException(
                status_code=502,
                detail=("EMA alert payload was not " "accepted for Algo App delivery."),
            )

        logger.info(
            "EMA alert payload accepted for " "Algo App delivery. event_id=%s",
            event_id,
        )

        return {
            "status": "accepted",
            "accepted": True,
            "event_id": event_id,
            "event_type": (normalized_payload.get("event_type")),
            "delivery_mode": (
                "background"
                if bool(
                    getattr(
                        config,
                        "ALGO_APP_SEND_IN_BACKGROUND",
                        True,
                    )
                )
                else "synchronous"
            ),
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


# ============================================================
# Health Route
# ============================================================


@router.get("/health")
async def get_algo_app_health():
    try:
        service_status = algo_app_service.get_status()

        enabled = bool(service_status.get("enabled"))

        configured = bool(service_status.get("configured"))

        if not enabled:
            health_status = "disabled"
        elif not configured:
            health_status = "not_configured"
        elif (
            service_status.get(
                "pending_count",
                0,
            )
            > 0
        ):
            health_status = "processing"
        elif service_status.get("last_error") and not service_status.get(
            "last_success_at"
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
            "pending_count": (
                service_status.get(
                    "pending_count",
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
            "last_success_at": (service_status.get("last_success_at")),
            "last_failure_at": (service_status.get("last_failure_at")),
            "market_time": (service_status.get("market_time")),
            "secrets_exposed": False,
        }

    except Exception as ex:
        logger.error(
            "Algo App health request failed: %s: %s",
            type(ex).__name__,
            ex,
        )

        raise HTTPException(
            status_code=500,
            detail=("Could not retrieve Algo App " "health status."),
        ) from ex


__all__ = [
    "router",
    "get_safe_algo_app_config",
    "get_safe_delivery_result",
    "get_algo_app_status_payload",
    "normalize_request_payload",
]
