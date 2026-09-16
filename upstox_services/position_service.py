from typing import Any

import upstox_client
from upstox_client.rest import ApiException

from core.logger import get_logger
from services.token_service import token_service

logger = get_logger(__file__)


def _get_order_api() -> upstox_client.OrderApi:
    access_token = token_service.get_access_token()

    if not access_token:
        raise RuntimeError("Upstox access token is not available.")

    configuration = upstox_client.Configuration()
    configuration.access_token = str(access_token).strip()

    api_client = upstox_client.ApiClient(configuration)

    return upstox_client.OrderApi(api_client)


def _get_api_error_message(
    exception: ApiException,
) -> str:
    response_body = getattr(
        exception,
        "body",
        None,
    )

    if response_body:
        return str(response_body)

    reason = getattr(
        exception,
        "reason",
        None,
    )

    if reason:
        return str(reason)

    return str(exception)


def exit_all_positions() -> dict[str, Any]:
    """
    Exit all open positions through the Upstox Order API.
    """

    logger.info("Exit-all-positions request started.")

    try:
        order_api = _get_order_api()

        response = order_api.exit_positions()

        logger.info(
            "Exit-all-positions request completed successfully. " "response_type=%s",
            type(response).__name__,
        )

        return {
            "success": True,
            "response": response,
            "error": None,
        }

    except ApiException as exc:
        error_message = _get_api_error_message(exc)

        status_code = getattr(
            exc,
            "status",
            None,
        )

        logger.exception(
            "Upstox exit-all-positions request failed. " "status_code=%s, error=%s",
            status_code,
            error_message,
        )

        return {
            "success": False,
            "response": None,
            "error": error_message,
        }

    except RuntimeError as exc:
        logger.error(
            "Exit-all-positions request could not start. " "error=%s",
            exc,
        )

        return {
            "success": False,
            "response": None,
            "error": str(exc),
        }

    except Exception as exc:
        logger.exception(
            "Unexpected error while exiting all positions. "
            "exception_type=%s, error=%s",
            type(exc).__name__,
            exc,
        )

        return {
            "success": False,
            "response": None,
            "error": str(exc),
        }
