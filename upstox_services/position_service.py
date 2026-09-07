from typing import Any

import upstox_client
from upstox_client.rest import ApiException

from core.logger import get_logger
from services.token_service import token_service

logger = get_logger("position_service")


def _get_order_api() -> upstox_client.OrderApi:
    access_token = token_service.get_access_token()

    if not access_token:
        raise RuntimeError(
            "Upstox access token is not available."
        )

    configuration = upstox_client.Configuration()
    configuration.access_token = access_token

    api_client = upstox_client.ApiClient(
        configuration,
    )

    return upstox_client.OrderApi(
        api_client,
    )


def exit_all_positions() -> dict[str, Any]:
    """
    Exit all open positions.
    """

    try:
        api = _get_order_api()

        response = api.exit_positions()

        logger.info(
            "Successfully exited all positions."
        )

        return {
            "success": True,
            "response": response,
            "error": None,
        }

    except ApiException as exc:
        error_message = (
            exc.body
            if getattr(exc, "body", None)
            else str(exc)
        )

        logger.exception(
            "Upstox exit positions failed."
        )

        return {
            "success": False,
            "response": None,
            "error": error_message,
        }

    except Exception as exc:
        logger.exception(
            "Unexpected error while exiting positions."
        )

        return {
            "success": False,
            "response": None,
            "error": str(exc),
        }