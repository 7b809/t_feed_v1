import logging
from typing import Any, Dict, Optional
import upstox_client
from upstox_client.rest import ApiException

from core import config
from services.token_service import token_service

logger = logging.getLogger(__name__)


def place_order(
    instrument_key: str,
    stop_loss: float,
    quantity: int = 65,
    order_type: str = "SL-M",
    transaction_type: str = "BUY",
    product: str = "I",
    validity: str = "DAY",
    price: float = 0.0,
    tag: str = "autotrader",
    disclosed_quantity: int = 0,
    is_amo: bool = False,
    access_token: Optional[str] = None,
    api_version: str = "2.0",
) -> Dict[str, Any]:
    """Places an order using the Upstox API and returns a unified status dict."""
    try:
        # 1. Resolve Access Token
        token = access_token or token_service.get_access_token()
        if not token:
            logger.error("Order placement failed: Missing access token.")
            return {
                "status": "error",
                "message": "Missing access token. Please authenticate first.",
                "exception_type": "AuthenticationError",
            }

        # 2. Configure Client
        configuration = upstox_client.Configuration()
        configuration.access_token = token
        api_instance = upstox_client.OrderApi(
            upstox_client.ApiClient(configuration)
        )

        # 3. Construct Order Body
        body = upstox_client.PlaceOrderRequest(
            quantity=quantity,
            product=product,
            validity=validity,
            price=price,
            tag=tag,
            instrument_token=instrument_key,
            order_type=order_type,
            transaction_type=transaction_type,
            disclosed_quantity=disclosed_quantity,
            trigger_price=float(stop_loss),
            is_amo=is_amo,
        )

        # 4. Execute Order
        api_response = api_instance.place_order(body, api_version)
        logger.info(
            f"Order placed successfully | Instrument: {instrument_key} | "
            f"Qty: {quantity} | SL Trigger: {stop_loss}"
        )

        # Convert SDK response object to dict if available, else pass raw
        response_data = (
            api_response.to_dict()
            if hasattr(api_response, "to_dict")
            else api_response
        )

        return {
            "status": "success",
            "api_response": response_data,
        }

    except ApiException as e:
        logger.error(f"ApiException in place_order: {e}")
        return {
            "status": "error",
            "message": f"Upstox API Error: {getattr(e, 'reason', str(e))}",
            "exception_type": "ApiException",
            "status_code": getattr(e, "status", None),
            "details": getattr(e, "body", str(e)),
        }

    except Exception as ex:
        logger.error(
            f"Unexpected exception in place_order: {type(ex).__name__} - {ex}"
        )
        return {
            "status": "error",
            "message": str(ex),
            "exception_type": type(ex).__name__,
        }


# if __name__ == "__main__":
#     test_response = place_order(
#         instrument_key="NSE_EQ|INE528G01035",
#         stop_loss=21.5,
#         quantity=65,
#     )
#     print(test_response)