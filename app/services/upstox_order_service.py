"""Adapter for the official Upstox Python SDK order APIs."""
from __future__ import annotations
import logging
from typing import Any
import upstox_client
from upstox_client.rest import ApiException
from app.core.config import settings
from app.models.order_models import PlaceOrderRequest

logger = logging.getLogger(__name__)

class UpstoxOrderService:
    def __init__(self) -> None:
        if not settings.UPSTOX_ACCESS_TOKEN:
            raise RuntimeError("UPSTOX_ACCESS_TOKEN is not configured")
        config = upstox_client.Configuration()
        config.access_token = settings.UPSTOX_ACCESS_TOKEN
        self.client = upstox_client.ApiClient(config)
        self.api_v2 = upstox_client.OrderApi(self.client)
        self.api_v3 = upstox_client.OrderApiV3(self.client)

    def place_order(self, request: PlaceOrderRequest) -> Any:
        data = request.model_dump()
        try:
            if settings.UPSTOX_ORDER_API_VERSION.lower() == "v2":
                body = upstox_client.PlaceOrderRequest(
                    quantity=data["quantity"], product=data["product"], validity=data["validity"],
                    price=data["price"], tag=data["tag"], instrument_token=data["instrument_token"],
                    order_type=data["order_type"], transaction_type=data["transaction_type"],
                    disclosed_quantity=data["disclosed_quantity"], trigger_price=data["trigger_price"],
                    is_amo=data["is_amo"], market_protection=data["market_protection"],
                )
                return self.api_v2.place_order1(body, settings.UPSTOX_API_VERSION)

            # Official SDK V3 signature: OrderApiV3.place_order(PlaceOrderV3Request).
            body = upstox_client.PlaceOrderV3Request(
                quantity=data["quantity"], product=data["product"], validity=data["validity"],
                price=data["price"], tag=data["tag"], instrument_token=data["instrument_token"],
                order_type=data["order_type"], transaction_type=data["transaction_type"],
                disclosed_quantity=data["disclosed_quantity"], trigger_price=data["trigger_price"],
                is_amo=data["is_amo"], slice=data["slice"],
            )
            return self.api_v3.place_order(body)
        except ApiException:
            logger.exception("Upstox rejected/failed order placement")
            raise

    def get_order_book(self) -> Any:
        return self.api_v2.get_order_book(settings.UPSTOX_API_VERSION)

    def exit_all_positions(self, segment: str | None = None, tag: str | None = None) -> Any:
        return self.api_v2.exit_positions(tag=tag, segment=segment)

    def get_positions(self) -> Any:
        portfolio_api = upstox_client.PortfolioApi(self.client)
        return portfolio_api.get_positions(settings.UPSTOX_API_VERSION)
