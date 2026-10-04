"""
Upstox order book service.

Wraps the Upstox SDK's OrderApi to fetch the current day's order book,
normalises field names to match what the templates expect, and handles
serialisation of SDK model objects into JSON-safe dicts.

Self-contained:
    - never raises to the caller (always returns a dict with `success`)
    - handles both v1 and v2 SDK method signatures
    - closes the SDK ApiClient if the installed version supports close()
"""

from __future__ import annotations

from typing import Any, Dict, List

from core.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# SDK import helper
# ---------------------------------------------------------------------------
def _import_upstox_sdk():
    """
    Import the SDK lazily so this module can be imported even when the
    package is not installed (web layer stays functional, orders just fail
    gracefully with a clear message).
    """
    import upstox_client  # type: ignore

    try:
        from upstox_client.rest import ApiException  # type: ignore
    except Exception:
        # Fall back to a broad exception when the SDK exposes it differently
        ApiException = Exception  # type: ignore

    return upstox_client, ApiException


class OrderBookService:
    """
    Service responsible for retrieving today's Upstox order book.
    """

    API_VERSION = "2.0"

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
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
            return [OrderBookService._serialize(item) for item in value]

        if hasattr(value, "to_dict"):
            try:
                return OrderBookService._serialize(value.to_dict())
            except Exception:
                pass

        if hasattr(value, "__dict__"):
            return {
                key: OrderBookService._serialize(item)
                for key, item in vars(value).items()
                if not key.startswith("_")
            }

        return str(value)

    # ------------------------------------------------------------------
    # Field normalisation
    # ------------------------------------------------------------------
    @staticmethod
    def _normalise_order(order: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalise SDK / v2 field names to what the orders.html template expects.

        Handles both camelCase and snake_case variants so the template
        doesn't need to change when the SDK shifts naming.
        """
        def pick(*keys):
            for k in keys:
                v = order.get(k)
                if v not in (None, ""):
                    return v
            return None

        return {
            "order_id": pick("order_id", "orderId"),
            "order_timestamp": pick("order_timestamp", "orderTimestamp"),
            "trading_symbol": pick(
                "trading_symbol", "tradingsymbol", "tradingSymbol"
            ),
            "instrument_token": pick("instrument_token", "instrumentToken"),
            "transaction_type": pick("transaction_type", "transactionType"),
            "order_type": pick("order_type", "orderType"),
            "product": pick("product"),
            "quantity": pick("quantity") or 0,
            "filled_quantity": pick("filled_quantity", "filledQuantity") or 0,
            "price": pick("price") or 0,
            "average_price": pick("average_price", "averagePrice") or 0,
            "status": pick("status"),
            "tag": pick("tag"),
            "status_message": pick("status_message", "statusMessage"),
            "status_message_raw": pick(
                "status_message_raw", "statusMessageRaw"
            ),
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_all_orders(self) -> Dict[str, Any]:
        """
        Retrieve the current day's order book.

        Returns:
            {
                "success": True,
                "status": "success",
                "count": N,
                "data": [ ...normalised orders... ]
            }

        On failure:
            {
                "success": False,
                "status": "error",
                "count": 0,
                "data": [],
                "message": "..."
            }
        """
        # ---- Token ----------------------------------------------------
        try:
            from token_tasks.service import token_service  # type: ignore
        except Exception as exc:
            logger.warning("token_service not importable: %s", exc)
            return {
                "success": False,
                "status": "error",
                "count": 0,
                "data": [],
                "message": "Token service unavailable.",
            }

        try:
            access_token = token_service.get_access_token()
        except Exception as exc:
            logger.warning("token_service.get_access_token() raised: %s", exc)
            access_token = None

        if not access_token:
            return {
                "success": False,
                "status": "error",
                "count": 0,
                "data": [],
                "message": "Upstox access token is not available.",
            }

        # ---- SDK import ----------------------------------------------
        try:
            upstox_client, ApiException = _import_upstox_sdk()
        except Exception as exc:
            logger.warning("Upstox SDK not available: %s", exc)
            return {
                "success": False,
                "status": "error",
                "count": 0,
                "data": [],
                "message": f"Upstox SDK not available: {exc}",
            }

        # ---- API call -------------------------------------------------
        configuration = upstox_client.Configuration()
        configuration.access_token = access_token
        api_client = upstox_client.ApiClient(configuration)

        try:
            order_api = upstox_client.OrderApi(api_client)

            try:
                api_response = order_api.get_order_book(self.API_VERSION)
            except TypeError:
                # Some SDK versions don't accept the version argument
                api_response = order_api.get_order_book()

            serialized = self._serialize(api_response)

            # Upstox SDK response shape is normally:
            #   {"status": "success", "data": [...]}
            # but a few SDK versions return the list directly.
            if isinstance(serialized, dict):
                orders = serialized.get("data") or []
                upstream_status = serialized.get("status")
            elif isinstance(serialized, list):
                orders = serialized
                upstream_status = "success"
            else:
                orders = []
                upstream_status = None

            if not isinstance(orders, list):
                orders = []

            normalised: List[Dict[str, Any]] = [
                self._normalise_order(o)
                for o in orders
                if isinstance(o, dict)
            ]

            return {
                "success": True,
                "status": upstream_status or "success",
                "count": len(normalised),
                "data": normalised,
            }

        except ApiException as exc:
            logger.warning("Upstox order book API failed: %s", exc)
            return {
                "success": False,
                "status": "error",
                "count": 0,
                "data": [],
                "message": str(exc),
            }
        except Exception as exc:
            logger.exception("Unexpected error fetching order book")
            return {
                "success": False,
                "status": "error",
                "count": 0,
                "data": [],
                "message": str(exc),
            }
        finally:
            # Close the SDK client if the installed version supports it.
            close_method = getattr(api_client, "close", None)
            if callable(close_method):
                try:
                    close_method()
                except Exception:
                    pass


# Module-level singleton — import this, don't instantiate OrderBookService
order_book_service = OrderBookService()