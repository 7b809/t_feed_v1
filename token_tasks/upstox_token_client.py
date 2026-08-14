import json

import upstox_client
from upstox_client.rest import ApiException

from core.logger import get_logger

logger = get_logger(__file__)


def response_to_dict(api_response) -> dict:
    """
    Converts Upstox SDK response object into a plain dictionary.

    Supports:
        - SDK objects with to_dict()
        - dict responses
        - JSON-like string responses
        - fallback raw string response
    """

    if api_response is None:
        return {}

    if hasattr(api_response, "to_dict"):
        try:
            return api_response.to_dict()
        except Exception:
            pass

    if isinstance(api_response, dict):
        return api_response

    try:
        return dict(api_response)
    except Exception:
        pass

    try:
        return json.loads(str(api_response))
    except Exception:
        return {
            "raw": str(api_response),
        }


def api_exception_to_message(exception: ApiException) -> str:
    """
    Converts Upstox ApiException into readable error text.
    """

    error_body = getattr(exception, "body", None)

    if error_body:
        return str(error_body)

    reason = getattr(exception, "reason", None)

    if reason:
        return str(reason)

    return str(exception)


class UpstoxTokenClient:
    """
    Upstox User API helper for token validation, profile, and funds.

    Used by:
        - Background token monitor
        - Telegram /token-status command
        - Telegram /profile command
        - Telegram /funds command
        - Telegram /save-token validation flow

    Token validation logic:
        - Call get_profile(api_version="2.0")
        - If response status is success, token is valid
        - If API throws ApiException or any exception, token is invalid
    """

    api_version = "2.0"

    def _get_user_api(self, access_token: str):
        """
        Creates Upstox UserApi instance using the provided access token.
        """

        access_token = str(access_token or "").strip()

        if not access_token:
            raise ValueError("access_token is empty.")

        configuration = upstox_client.Configuration()
        configuration.access_token = access_token

        return upstox_client.UserApi(upstox_client.ApiClient(configuration))

    def get_profile(self, access_token: str) -> dict:
        """
        Calls Upstox get_profile API using the provided access token.

        Returns:
            Plain dictionary response.

        Expected success example:
            {
                "status": "success",
                "data": {
                    "broker": "UPSTOX",
                    "email": "...",
                    "exchanges": [...],
                    "is_active": true,
                    "order_types": [...],
                    "poa": false,
                    "products": [...],
                    "user_id": "...",
                    "user_name": "...",
                    "user_type": "individual"
                }
            }
        """

        api_instance = self._get_user_api(access_token)

        api_response = api_instance.get_profile(self.api_version)

        return response_to_dict(api_response)

    def get_funds(self, access_token: str) -> dict:
        """
        Calls Upstox get_user_fund_margin API using the provided access token.

        Returns:
            Plain dictionary response.

        This raw response is intended to be returned to Telegram for /funds.
        """

        api_instance = self._get_user_api(access_token)

        api_response = api_instance.get_user_fund_margin(self.api_version)

        return response_to_dict(api_response)

    def validate_token(
        self,
        access_token: str,
    ) -> tuple[bool, dict | None, str | None]:
        """
        Validates token by calling Upstox profile API.

        Returns:
            tuple:
                (
                    is_valid,
                    profile_response,
                    error_message
                )

        Valid token:
            (True, profile_response, None)

        Invalid/expired/corrupted token:
            (False, None or response, error_message)
        """

        access_token = str(access_token or "").strip()

        if not access_token:
            return False, None, "access_token is empty."

        try:
            profile = self.get_profile(access_token)

            status = str(profile.get("status", "")).strip().lower()

            if status == "success":
                logger.info("Upstox token validation successful using profile API.")
                return True, profile, None

            error_message = (
                "Profile API response did not return success status. "
                f"response={profile}"
            )

            logger.warning(error_message)

            return False, profile, error_message

        except ApiException as ex:
            error_message = api_exception_to_message(ex)

            logger.warning(
                f"Upstox token validation failed with ApiException: {error_message}"
            )

            return False, None, error_message

        except Exception as ex:
            error_message = f"{type(ex).__name__}: {ex}"

            logger.warning(
                f"Upstox token validation failed with exception: {error_message}"
            )

            return False, None, error_message

    def get_profile_safe(
        self,
        access_token: str,
    ) -> dict:
        """
        Safe wrapper for profile API.

        Returns success/error payload instead of throwing.
        Useful for Telegram /profile command.
        """

        try:
            response = self.get_profile(access_token)

            return {
                "status": "success",
                "api": "get_profile",
                "response": response,
            }

        except ApiException as ex:
            return {
                "status": "failed",
                "api": "get_profile",
                "error_type": "ApiException",
                "error": api_exception_to_message(ex),
            }

        except Exception as ex:
            return {
                "status": "failed",
                "api": "get_profile",
                "error_type": type(ex).__name__,
                "error": str(ex),
            }

    def get_funds_safe(
        self,
        access_token: str,
    ) -> dict:
        """
        Safe wrapper for funds API.

        Returns success/error payload instead of throwing.
        Useful for Telegram /funds command.
        """

        try:
            response = self.get_funds(access_token)

            return {
                "status": "success",
                "api": "get_user_fund_margin",
                "response": response,
            }

        except ApiException as ex:
            return {
                "status": "failed",
                "api": "get_user_fund_margin",
                "error_type": "ApiException",
                "error": api_exception_to_message(ex),
            }

        except Exception as ex:
            return {
                "status": "failed",
                "api": "get_user_fund_margin",
                "error_type": type(ex).__name__,
                "error": str(ex),
            }


upstox_token_client = UpstoxTokenClient()
