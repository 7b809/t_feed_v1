from copy import deepcopy
from typing import Any, Callable

from core import config
from core.logger import get_logger
from services.algo_app_service import algo_app_service
from services.isolated_instrument_event_service import (
    isolated_instrument_event_saving_service,
)
from services.telegram_service import telegram_service
from upstox_services.order_saving_service  import upstox_order_saving_service
from upstox_services.process_order import (
    process_selected_instrument,
)

logger = get_logger(__file__)


class EMAEventDeliveryService:
    def _build_delivery_result(
        self,
        *,
        telegram_enabled: bool,
        algo_app_enabled: bool,
    ) -> dict[str, Any]:
        return {
            "success": False,
            "accepted": False,
            "telegram": {
                "enabled": telegram_enabled,
                "attempted": False,
                "success": False,
                "error": None,
            },
            "algo_app": {
                "enabled": algo_app_enabled,
                "attempted": False,
                "success": False,
                "error": None,
                "delivery_mode": None,
            },
            "mongo": {
                "enabled": bool(
                    getattr(
                        config,
                        "ISOLATED_INSTRUMENT_EVENT_ENABLED",
                        True,
                    )
                ),
                "attempted": False,
                "success": False,
                "saved": False,
                "skipped": False,
                "document_id": None,
                "date": None,
                "time_key": None,
                "error": None,
            },
            "order": {
                "enabled": False,
                "attempted": False,
                "success": False,
                "order_status": None,
                "error": None,
            },
            "order_mongo": {
                "enabled": bool(
                    getattr(config, "UPSTOX_ORDER_ENABLED", True)
                ),
                "attempted": False,
                "success": False,
                "saved": False,
                "document_id": None,
                "error": None,
            },
            "state": {
                "attempted": False,
                "success": False,
                "error": None,
            },
            "warnings": [],
            "errors": [],
        }

    def _send_telegram(
        self,
        *,
        title: str,
        message: str,
        level: str,
        notification_context: str,
    ) -> dict[str, Any]:
        result = {
            "attempted": True,
            "success": False,
            "error": None,
        }

        try:
            result["success"] = bool(
                telegram_service.send_message(
                    title=title,
                    message=message,
                    level=level,
                    notification_context=notification_context,
                )
            )

            if not result["success"]:
                result["error"] = "Telegram service did not accept the message."

        except Exception as exc:
            logger.exception(
                "EMA Telegram delivery failed. " "title=%s, context=%s, error_type=%s",
                title,
                notification_context,
                type(exc).__name__,
            )

            result["error"] = f"{type(exc).__name__}: {exc}"

        return result

    def _send_algo_app(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        result = {
            "attempted": True,
            "success": False,
            "error": None,
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

        try:
            result["success"] = bool(
                algo_app_service.dispatch_ema_alert(deepcopy(payload))
            )

            if not result["success"]:
                result["error"] = "Algo App did not accept the EMA payload."

        except Exception as exc:
            logger.exception(
                "EMA Algo App delivery failed. " "event_id=%s, error_type=%s",
                payload.get("event_id"),
                type(exc).__name__,
            )

            result["error"] = f"{type(exc).__name__}: {exc}"

        return result

    def _save_event(
        self,
        *,
        payload: dict[str, Any],
        processing_result: dict[str, Any],
        processing_status: str,
    ) -> dict[str, Any]:
        result = {
            "attempted": True,
            "success": False,
            "saved": False,
            "skipped": False,
            "document_id": None,
            "date": None,
            "time_key": None,
            "error": None,
        }

        try:
            save_result = (
                isolated_instrument_event_saving_service.save_processing_event(
                    payload=deepcopy(payload),
                    processing_result=deepcopy(processing_result),
                    processing_status=processing_status,
                )
            )

            if not isinstance(save_result, dict):
                result["error"] = (
                    "Event-saving service returned an " "invalid response."
                )
                return result

            result.update(
                {
                    "success": bool(save_result.get("success")),
                    "saved": bool(save_result.get("saved")),
                    "skipped": bool(save_result.get("skipped")),
                    "document_id": save_result.get("document_id"),
                    "date": save_result.get("date"),
                    "time_key": save_result.get("time_key"),
                    "error": save_result.get("error"),
                }
            )

        except Exception as exc:
            logger.exception(
                "EMA event saving failed. "
                "event_id=%s, processing_status=%s, "
                "error_type=%s",
                payload.get("event_id"),
                processing_status,
                type(exc).__name__,
            )

            result["error"] = f"{type(exc).__name__}: {exc}"

        return result

    def _save_order_result(
        self,
        *,
        payload: dict[str, Any],
        order_result: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Save the complete order workflow result in the dedicated
        UPSTOX_ORDER_COLLECTION MongoDB collection.

        This is intentionally independent of isolated instrument event
        saving. A failure here must not stop the remaining deliveries.
        """
        result = {
            "attempted": True,
            "success": False,
            "saved": False,
            "document_id": None,
            "error": None,
        }

        try:
            save_result = upstox_order_saving_service.save_order_result(
                payload=deepcopy(payload),
                order_result=deepcopy(order_result),
            )

            if not isinstance(save_result, dict):
                result["error"] = (
                    "Order-saving service returned an invalid response."
                )
                return result

            result.update(
                {
                    "success": bool(save_result.get("success")),
                    "saved": bool(save_result.get("saved")),
                    "document_id": save_result.get("document_id"),
                    "error": save_result.get("error"),
                }
            )

        except Exception as exc:
            logger.exception(
                "Order result saving failed. event_id=%s, error_type=%s",
                payload.get("event_id"),
                type(exc).__name__,
            )
            result["error"] = f"{type(exc).__name__}: {exc}"

        return result

    def _append_state_record(
        self,
        *,
        alert_record: dict[str, Any],
        append_callback: (
            Callable[
                [dict[str, Any]],
                Any,
            ]
            | None
        ),
    ) -> dict[str, Any]:
        result = {
            "attempted": False,
            "success": False,
            "error": None,
        }

        if append_callback is None:
            return result

        result["attempted"] = True

        try:
            append_callback(deepcopy(alert_record))
            result["success"] = True

        except Exception as exc:
            logger.exception(
                "EMA in-memory state update failed. " "event_id=%s, error_type=%s",
                alert_record.get("event_id"),
                type(exc).__name__,
            )

            result["error"] = f"{type(exc).__name__}: {exc}"

        return result

    @staticmethod
    def _select_lowest_budget_instrument(
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """
        Select the lowest-LTP instrument from the budget-range instruments.

        Supported payload keys:
        - budget_range_available_instruments
        - budget_range_instruments
        - available_instruments

        The selected instrument must contain instrument_key. LTP is read
        from live_ltp, ltp, price, or last_price.
        """
        candidate_keys = (
            "budget_range_available_instruments",
            "budget_range_instruments",
            "available_instruments",
        )

        instruments: Any = None
        for key in candidate_keys:
            value = payload.get(key)
            if isinstance(value, list):
                instruments = value
                break

        if not instruments:
            logger.warning(
                "No budget-range instrument list found in EMA payload. "
                "Expected one of keys=%s",
                candidate_keys,
            )
            return None

        valid_instruments: list[dict[str, Any]] = []

        for instrument in instruments:
            if not isinstance(instrument, dict):
                continue

            instrument_key = instrument.get("instrument_key")
            if not instrument_key:
                continue

            raw_price = (
                instrument.get("live_ltp")
                if instrument.get("live_ltp") is not None
                else instrument.get("ltp")
            )
            if raw_price is None:
                raw_price = (
                    instrument.get("price")
                    if instrument.get("price") is not None
                    else instrument.get("last_price")
                )

            try:
                price = float(raw_price)
            except (TypeError, ValueError):
                logger.warning(
                    "Skipping instrument with invalid price. "
                    "instrument_key=%s, price=%r",
                    instrument_key,
                    raw_price,
                )
                continue

            if price <= 0:
                continue

            selected = deepcopy(instrument)
            selected["live_ltp"] = price
            valid_instruments.append(selected)

        if not valid_instruments:
            logger.warning("No valid budget-range instruments available for order.")
            return None

        return min(
            valid_instruments,
            key=lambda item: float(item.get("live_ltp", 0)),
        )

    @staticmethod
    def _build_order_instrument(
        selected_instrument: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Keep only the instrument details required by process_order_service.
        """
        return {
            "instrument_key": selected_instrument.get("instrument_key"),
            "trading_symbol": selected_instrument.get("trading_symbol"),
            "live_ltp": selected_instrument.get("live_ltp"),
            "lot_size": selected_instrument.get("lot_size"),
        }

    def _place_lowest_budget_instrument(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        selected = self._select_lowest_budget_instrument(payload)

        if not selected:
            return {
                "success": False,
                "order_status": "NO_VALID_BUDGET_INSTRUMENT",
                "error": ("No valid lowest budget-range instrument was found."),
            }

        order_instrument = self._build_order_instrument(selected)

        if not order_instrument.get("trading_symbol"):
            return {
                "success": False,
                "order_status": "INVALID_ORDER_INSTRUMENT",
                "selected_instrument": order_instrument,
                "error": ("Selected instrument is missing trading_symbol."),
            }

        logger.info(
            "Placing order for lowest budget-range instrument. "
            "instrument_key=%s, trading_symbol=%s, live_ltp=%s",
            order_instrument.get("instrument_key"),
            order_instrument.get("trading_symbol"),
            order_instrument.get("live_ltp"),
        )

        try:
            return process_selected_instrument(order_instrument)
        except Exception as exc:
            logger.exception(
                "Lowest budget-range order placement failed. " "instrument_key=%s",
                order_instrument.get("instrument_key"),
            )
            return {
                "success": False,
                "order_status": "ORDER_PROCESSING_EXCEPTION",
                "selected_instrument": order_instrument,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def process(
        self,
        *,
        payload: dict[str, Any],
        telegram_title: str,
        telegram_message: str,
        telegram_level: str = "EMA",
        telegram_enabled: bool = True,
        algo_app_enabled: bool = True,
        order_enabled: bool | None = None,
        save_event: bool = True,
        processing_status: str | None = None,
        alert_record: dict[str, Any] | None = None,
        append_state_callback: (
            Callable[
                [dict[str, Any]],
                Any,
            ]
            | None
        ) = None,
    ) -> dict[str, Any]:
        """
        Execute every enabled delivery independently.

        A failure in one delivery must never stop the remaining deliveries.
        Each enabled delivery sends its own completion Telegram message.
        """

        if not isinstance(payload, dict) or not payload:
            return {
                "success": False,
                "accepted": False,
                "error": "EMA payload is empty or invalid.",
            }

        event_id = payload.get("event_id")
        instrument = payload.get("instrument") or {}
        if not isinstance(instrument, dict):
            instrument = {}

        instrument_key = instrument.get("instrument_key")

        if order_enabled is None:
            order_enabled = bool(getattr(config, "PLACE_ORDER", False))

        order_saving_enabled = bool(
            getattr(config, "UPSTOX_ORDER_ENABLED", True)
        )

        result = self._build_delivery_result(
            telegram_enabled=telegram_enabled,
            algo_app_enabled=algo_app_enabled,
        )

        result["order"]["enabled"] = order_enabled
        result["order_mongo"]["enabled"] = order_saving_enabled
        result["mongo"]["enabled"] = bool(
            getattr(
                config,
                "ISOLATED_INSTRUMENT_EVENT_ENABLED",
                True,
            )
        )

        result.update(
            {
                "event_id": event_id,
                "instrument_key": instrument_key,
                "error": None,
            }
        )

        notification_context = (
            "ema_event" f"|event_id={event_id}" f"|instrument_key={instrument_key}"
        )

        # Delivery 1: Telegram
        if telegram_enabled:
            try:
                telegram_result = self._send_telegram(
                    title=telegram_title,
                    message=telegram_message,
                    level=telegram_level,
                    notification_context=notification_context,
                )
                result["telegram"].update(telegram_result)

                self._send_telegram(
                    title="Telegram Delivery Completed",
                    message=(
                        "Telegram delivery completed.\n"
                        f"Event ID: {event_id}\n"
                        f"Instrument: {instrument_key}\n"
                        f"Success: {telegram_result.get('success', False)}"
                    ),
                    level=("SUCCESS" if telegram_result.get("success") else "WARNING"),
                    notification_context=(f"{notification_context}|telegram_completed"),
                )
            except Exception as exc:
                result["telegram"]["error"] = f"{type(exc).__name__}: {exc}"
                result["errors"].append(f"Telegram delivery failed: {exc}")
                logger.exception(
                    "Telegram delivery workflow failed. event_id=%s",
                    event_id,
                )

        # Delivery 2: Algo App
        if algo_app_enabled:
            try:
                algo_result = self._send_algo_app(deepcopy(payload))
                result["algo_app"].update(algo_result)

                self._send_telegram(
                    title="Algo App Delivery Completed",
                    message=(
                        "Algo App delivery completed.\n"
                        f"Event ID: {event_id}\n"
                        f"Instrument: {instrument_key}\n"
                        f"Success: {algo_result.get('success', False)}"
                    ),
                    level=("SUCCESS" if algo_result.get("success") else "WARNING"),
                    notification_context=(f"{notification_context}|algo_app_completed"),
                )
            except Exception as exc:
                result["algo_app"]["error"] = f"{type(exc).__name__}: {exc}"
                result["errors"].append(f"Algo App delivery failed: {exc}")
                logger.exception(
                    "Algo App delivery workflow failed. event_id=%s",
                    event_id,
                )

        # Delivery 3: Order placement
        if order_enabled:
            try:
                order_result = self._place_lowest_budget_instrument(payload)
                if isinstance(order_result, dict):
                    result["order"].update(order_result)
                else:
                    result["order"].update(
                        {
                            "success": False,
                            "error": ("Order service returned an invalid response."),
                        }
                    )

                self._send_telegram(
                    title="Order Placement Delivery Completed",
                    message=(
                        "Order placement delivery completed.\n"
                        f"Event ID: {event_id}\n"
                        f"Instrument: {instrument_key}\n"
                        f"Order Status: "
                        f"{result['order'].get('order_status')}\n"
                        f"Success: {result['order'].get('success', False)}"
                    ),
                    level=("SUCCESS" if result["order"].get("success") else "WARNING"),
                    notification_context=(f"{notification_context}|order_completed"),
                )
            except Exception as exc:
                result["order"].update(
                    {
                        "success": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                result["errors"].append(f"Order placement failed: {exc}")
                logger.exception(
                    "Order placement workflow failed. event_id=%s",
                    event_id,
                )

            # Save order details separately from the isolated event document.
            if order_saving_enabled:
                try:
                    order_mongo_result = self._save_order_result(
                        payload=payload,
                        order_result=deepcopy(result["order"]),
                    )
                    result["order_mongo"].update(order_mongo_result)

                    if not order_mongo_result.get("saved"):
                        result["warnings"].append(
                            "Order result was not saved in the order collection."
                        )

                except Exception as exc:
                    result["order_mongo"]["error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
                    result["errors"].append(
                        f"Order result saving failed: {exc}"
                    )
                    logger.exception(
                        "Order result saving workflow failed. event_id=%s",
                        event_id,
                    )

        # Delivery 4: In-memory state update
        delivery_accepted = bool(
            result["telegram"].get("success")
            or result["algo_app"].get("success")
            or result["order"].get("success")
        )

        result["accepted"] = delivery_accepted
        result["success"] = delivery_accepted

        if (
            delivery_accepted
            and isinstance(alert_record, dict)
            and append_state_callback is not None
        ):
            try:
                state_result = self._append_state_record(
                    alert_record=alert_record,
                    append_callback=append_state_callback,
                )
                result["state"].update(state_result)

                self._send_telegram(
                    title="State Update Delivery Completed",
                    message=(
                        "In-memory state update completed.\n"
                        f"Event ID: {event_id}\n"
                        f"Success: {state_result.get('success', False)}"
                    ),
                    level=("SUCCESS" if state_result.get("success") else "WARNING"),
                    notification_context=(f"{notification_context}|state_completed"),
                )
            except Exception as exc:
                result["state"]["error"] = f"{type(exc).__name__}: {exc}"
                result["errors"].append(f"State update failed: {exc}")
                logger.exception(
                    "State update workflow failed. event_id=%s",
                    event_id,
                )

        # Delivery 5: MongoDB event saving
        if save_event:
            try:
                if processing_status is None:
                    processing_status = (
                        "delivery_accepted"
                        if delivery_accepted
                        else "delivery_not_accepted"
                    )

                isolated_processing_result = deepcopy(result)
                isolated_processing_result.pop("order", None)
                isolated_processing_result.pop("order_mongo", None)

                mongo_result = self._save_event(
                    payload=payload,
                    processing_result=isolated_processing_result,
                    processing_status=processing_status,
                )
                result["mongo"].update(mongo_result)

                self._send_telegram(
                    title="MongoDB Event Saving Completed",
                    message=(
                        "MongoDB event saving completed.\n"
                        f"Event ID: {event_id}\n"
                        f"Saved: {mongo_result.get('saved', False)}\n"
                        f"Success: {mongo_result.get('success', False)}"
                    ),
                    level=("SUCCESS" if mongo_result.get("saved") else "WARNING"),
                    notification_context=(f"{notification_context}|mongo_completed"),
                )

                if not mongo_result.get("saved") and not mongo_result.get("skipped"):
                    result["warnings"].append(
                        "Event processing completed, but the event was not saved."
                    )
            except Exception as exc:
                result["mongo"]["error"] = f"{type(exc).__name__}: {exc}"
                result["errors"].append(f"MongoDB saving failed: {exc}")
                logger.exception(
                    "MongoDB saving workflow failed. event_id=%s",
                    event_id,
                )

        if result["errors"]:
            result["error"] = "; ".join(result["errors"])

        logger.info(
            "EMA event processing completed. "
            "event_id=%s, instrument_key=%s, accepted=%s, "
            "telegram=%s, algo_app=%s, order=%s, order_mongo_saved=%s, "
            "mongo_saved=%s, state_updated=%s, errors=%s",
            event_id,
            instrument_key,
            result["accepted"],
            result["telegram"]["success"],
            result["algo_app"]["success"],
            result["order"]["success"],
            result["order_mongo"]["saved"],
            result["mongo"]["saved"],
            result["state"]["success"],
            len(result["errors"]),
        )

        return result


ema_event_delivery_service = EMAEventDeliveryService()
