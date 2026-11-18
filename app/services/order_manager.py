"""Order lifecycle orchestration and dual persistence."""
from __future__ import annotations
import logging
import uuid
from datetime import datetime
from typing import Any
from app.core.config import settings
from app.models.order_models import PlaceOrderRequest
from app.services.order_archive_service import archive_event
from app.services.upstox_order_service import UpstoxOrderService

logger = logging.getLogger(__name__)

def _plain(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dict__"):
        return vars(value)
    return {"response": str(value)}

class OrderManager:
    def __init__(self) -> None:
        self._upstox: UpstoxOrderService | None = None

    def _client(self) -> UpstoxOrderService:
        if self._upstox is None:
            self._upstox = UpstoxOrderService()
        return self._upstox

    def _guard_live(self) -> None:
        if not settings.TRADING_ENABLED:
            raise PermissionError(
                "Live trading is disabled. Set TRADING_ENABLED=true in .env only after "
                "checking credentials, instrument, lot size and risk limits."
            )

    def place_order(self, request: PlaceOrderRequest) -> dict:
        self._guard_live()
        event_id = str(uuid.uuid4())
        base = {
            "event_id": event_id, "event_type": "ORDER_PLACEMENT",
            "request": request.model_dump(), "requested_time": datetime.now().astimezone().isoformat(),
        }
        try:
            response = _plain(self._client().place_order(request))
            data = response.get("data", {}) if isinstance(response, dict) else {}
            # V3 may return multiple order_ids when slicing is requested; V2 returns order_id.
            order_ids = data.get("order_ids") or ([data["order_id"]] if data.get("order_id") else [])
            event = {**base, "status": "placed", "order_ids": order_ids, "response": response}
            event.update(archive_event(event))
            return event
        except Exception as exc:
            event = {
                **base, "status": "exception", "exception_type": type(exc).__name__,
                "exception": str(exc),
            }
            try:
                event.update(archive_event(event))
            except Exception:
                logger.exception("Could not persist failed placement event")
            raise

    def order_book(self) -> Any:
        return _plain(self._client().get_order_book())

    def exit_all(self, segment: str | None = None, tag: str | None = None) -> dict:
        self._guard_live()
        event_id = str(uuid.uuid4())
        request = {"segment": segment, "tag": tag}
        try:
            response = _plain(self._client().exit_all_positions(segment=segment, tag=tag))
            event = {
                "event_id": event_id, "event_type": "EXIT_ALL_POSITIONS",
                "status": "exit_requested", "request": request, "response": response,
            }
            event.update(archive_event(event))
            return event
        except Exception as exc:
            event = {
                "event_id": event_id, "event_type": "EXIT_ALL_POSITIONS", "status": "exception",
                "request": request, "exception_type": type(exc).__name__, "exception": str(exc),
            }
            try:
                event.update(archive_event(event))
            except Exception:
                logger.exception("Could not persist failed exit-all event")
            raise

    def exit_single(self, order_id: str) -> dict:
        """Square off up to the filled quantity of a selected completed order.

        Upstox has an exit-all/by-segment/tag endpoint, not a specific-order close
        endpoint. This uses the selected order and live net position to place the
        opposite-side market order. It must not reverse the current net position.
        """
        self._guard_live()
        event_id = str(uuid.uuid4())
        try:
            book = self.order_book()
            orders = book.get("data", []) if isinstance(book, dict) else []
            selected = next((o for o in orders if str(o.get("order_id")) == order_id), None)
            if selected is None:
                raise LookupError(f"Order {order_id} was not found in today's Upstox order book")
            if str(selected.get("status", "")).lower() != "complete":
                raise ValueError("Only completed orders can be exited")

            filled_qty = int(selected.get("filled_quantity") or 0)
            if filled_qty <= 0:
                raise ValueError("Selected order has no filled_quantity; refusing to guess exit quantity")

            positions_response = _plain(self._client().get_positions())
            positions = positions_response.get("data", []) if isinstance(positions_response, dict) else []
            position = next((
                p for p in positions
                if p.get("instrument_token") == selected.get("instrument_token")
                and p.get("product") == selected.get("product")
            ), None)
            if position is None:
                raise LookupError("No open net position found for this order's instrument/product")

            net_qty = int(position.get("quantity") or 0)
            if net_qty == 0:
                raise LookupError("The selected instrument/product has no open net quantity")
            order_side = str(selected.get("transaction_type", "")).upper()
            if (net_qty > 0 and order_side != "BUY") or (net_qty < 0 and order_side != "SELL"):
                raise ValueError(
                    "The current net position is opposite to the selected order side; "
                    "refusing to create or enlarge a reverse position."
                )

            exit_qty = min(filled_qty, abs(net_qty))
            reverse = PlaceOrderRequest(
                instrument_token=position["instrument_token"],
                transaction_type="SELL" if net_qty > 0 else "BUY",
                quantity=exit_qty, product=position.get("product", "I"),
                order_type="MARKET", validity="DAY", price=0,
                tag=f"exit-{order_id}"[:40],
            )
            response = _plain(self._client().place_order(reverse))
            response_data = response.get("data", {}) if isinstance(response, dict) else {}
            event = {
                "event_id": event_id, "event_type": "EXIT_SINGLE_POSITION",
                "status": "exit_requested", "source_order_id": order_id,
                "exit_quantity": exit_qty, "request": reverse.model_dump(),
                "response": response,
                "exit_order_ids": response_data.get("order_ids") or (
                    [response_data["order_id"]] if response_data.get("order_id") else []
                ),
            }
            event.update(archive_event(event))
            return event
        except Exception as exc:
            event = {
                "event_id": event_id, "event_type": "EXIT_SINGLE_POSITION",
                "status": "exception", "source_order_id": order_id,
                "exception_type": type(exc).__name__, "exception": str(exc),
            }
            try:
                event.update(archive_event(event))
            except Exception:
                logger.exception("Could not persist failed single-exit event")
            raise

order_manager = OrderManager()
