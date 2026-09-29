# upstox_services/order_book_service.py

from typing import Any, Dict

import upstox_client
from upstox_client.rest import ApiException

from services.token_service import token_service


class OrderBookService:
    """
    Service responsible for retrieving today's Upstox order book.
    """

    API_VERSION = "2.0"

    @staticmethod
    def _serialize(value: Any) -> Any:
        """
        Recursively convert Upstox SDK model objects into JSON-safe values.
        """

        if value is None:
            return None

        if isinstance(value, (str, int, float, bool)):
            return value

        if isinstance(value, dict):
            return {
                key: OrderBookService._serialize(item)
                for key, item in value.items()
            }

        if isinstance(value, (list, tuple)):
            return [
                OrderBookService._serialize(item)
                for item in value
            ]

        if hasattr(value, "to_dict"):
            return OrderBookService._serialize(value.to_dict())

        if hasattr(value, "__dict__"):
            return {
                key: OrderBookService._serialize(item)
                for key, item in vars(value).items()
                if not key.startswith("_")
            }

        return str(value)

    def get_all_orders(self) -> Dict[str, Any]:
        """
        Retrieve the current day's order book.

        Returns:
            {
                "success": True,
                "count": 2,
                "data": [...]
            }

        Raises:
            ValueError: When the access token is unavailable.
            ApiException: When the Upstox API request fails.
        """

        access_token = token_service.get_access_token()

        if not access_token:
            raise ValueError("Upstox access token is not available")

        configuration = upstox_client.Configuration()
        configuration.access_token = access_token

        api_client = upstox_client.ApiClient(configuration)

        try:
            order_api = upstox_client.OrderApi(api_client)

            api_response = order_api.get_order_book(
                self.API_VERSION
            )

            serialized_response = self._serialize(api_response)

            # Upstox SDK response normally contains:
            # {
            #     "status": "success",
            #     "data": [...]
            # }
            if isinstance(serialized_response, dict):
                orders = serialized_response.get("data") or []
                upstream_status = serialized_response.get("status")
            elif isinstance(serialized_response, list):
                orders = serialized_response
                upstream_status = "success"
            else:
                orders = []
                upstream_status = None

            return {
                "success": True,
                "status": upstream_status,
                "count": len(orders),
                "data": orders,
            }

        except ApiException:
            # Preserve the original SDK exception for the API route.
            raise

        finally:
            # Close the SDK client if the installed SDK supports close().
            close_method = getattr(api_client, "close", None)

            if callable(close_method):
                close_method()


order_book_service = OrderBookService()