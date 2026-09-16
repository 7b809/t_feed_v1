import hmac
import threading

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse

from core import config
from core.logger import get_logger
from services.service_control_service import (
    ServiceActionResult,
    service_control_service,
)

logger = get_logger(__file__)

router = APIRouter(
    prefix="/admin/service",
    tags=["Service Control"],
)


def verify_service_control_token(
    x_service_control_token: str | None = Header(
        default=None,
        alias="X-Service-Control-Token",
    ),
) -> None:
    if not config.SERVICE_CONTROL_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service control is disabled.",
        )

    expected_token = config.SERVICE_CONTROL_TOKEN

    if not expected_token:
        logger.error("Service control is enabled without a configured token.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service control token is not configured.",
        )

    supplied_token = x_service_control_token or ""

    if not hmac.compare_digest(
        supplied_token,
        expected_token,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service-control token.",
        )


def result_response(
    result: ServiceActionResult,
) -> JSONResponse:
    return JSONResponse(
        status_code=(
            status.HTTP_200_OK
            if result.success
            else status.HTTP_500_INTERNAL_SERVER_ERROR
        ),
        content=result.to_dict(),
    )


def execute_delayed_action(action, delay_seconds: float = 1.0):
    """
    Execute disruptive actions after the HTTP response has had time
    to leave the application.
    """

    def delayed_runner():
        import time

        time.sleep(delay_seconds)

        try:
            action()
        except Exception:
            logger.exception("Delayed service-control action failed.")

    thread = threading.Thread(
        target=delayed_runner,
        name="service-control-action",
        daemon=True,
    )
    thread.start()


@router.get(
    "/status",
    dependencies=[Depends(verify_service_control_token)],
)
def service_status():
    return service_control_service.get_status()


@router.post(
    "/streamer/restart",
    dependencies=[Depends(verify_service_control_token)],
)
def restart_streamer():
    result = service_control_service.restart_streamer()
    return result_response(result)


@router.post(
    "/start",
    dependencies=[Depends(verify_service_control_token)],
)
def start_service():
    result = service_control_service.start_service()
    return result_response(result)


@router.post(
    "/stop",
    dependencies=[Depends(verify_service_control_token)],
    status_code=status.HTTP_202_ACCEPTED,
)
def stop_service():
    execute_delayed_action(service_control_service.stop_service)

    return {
        "success": True,
        "action": "stop",
        "status": "accepted",
        "message": (
            "Service stop was accepted. The API may become " "unavailable immediately."
        ),
    }


@router.post(
    "/restart",
    dependencies=[Depends(verify_service_control_token)],
    status_code=status.HTTP_202_ACCEPTED,
)
def restart_service():
    execute_delayed_action(service_control_service.restart_service)

    return {
        "success": True,
        "action": "restart",
        "status": "accepted",
        "message": (
            "Service restart was accepted. Temporary API " "unavailability is expected."
        ),
    }


@router.post(
    "/redeploy",
    dependencies=[Depends(verify_service_control_token)],
    status_code=status.HTTP_202_ACCEPTED,
)
def redeploy_service():
    execute_delayed_action(service_control_service.redeploy_service)

    return {
        "success": True,
        "action": "redeploy",
        "status": "accepted",
        "message": (
            "Redeployment was accepted. Temporary API " "unavailability may occur."
        ),
    }
