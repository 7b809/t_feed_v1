from copy import deepcopy
from typing import Any, Callable

from core import config
from core.logger import get_logger
from services.algo_app_service import algo_app_service
from services.isolated_instrument_event_service import (
    isolated_instrument_event_saving_service,
)
from services.telegram_service import telegram_service
from upstox_services.order_saving_service import (
    upstox_order_saving_service,
)
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
                "selected_instrument": None,
                "order_id": None,
                "error": None,
            },
            "order_mongo": {
                "enabled": bool(
                    getattr(
                        config,
                        "UPSTOX_ORDER_ENABLED",
                        True,
                    )
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
                    "Event-saving service returned " "an invalid response."
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
                    "Order-saving service returned " "an invalid response."
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
                "Order result saving failed. " "event_id=%s, error_type=%s",
                payload.get("event_id"),
                type(exc).__name__,
            )

            result["error"] = f"{type(exc).__name__}: {exc}"

        return result

    def _append_state_record(
        self,
        *,
        alert_record: dict[str, Any],
        append_callback: Callable[[dict[str, Any]], Any] | None,
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
    def _get_budget_instruments(
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        order_suggestion = payload.get(
            "order_suggestion",
            {},
        )

        if not isinstance(order_suggestion, dict):
            order_suggestion = {}

        budget_filter = order_suggestion.get(
            "budget_filter",
            {},
        )

        if not isinstance(budget_filter, dict):
            budget_filter = {}

        instruments = budget_filter.get(
            "instruments",
            [],
        )

        if isinstance(instruments, list):
            return instruments

        legacy_keys = (
            "budget_range_available_instruments",
            "budget_range_instruments",
            "available_instruments",
        )

        for key in legacy_keys:
            legacy_instruments = payload.get(key)

            if isinstance(legacy_instruments, list):
                return legacy_instruments

        return []

    @staticmethod
    def _select_lowest_budget_instrument(
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        instruments = EMAEventDeliveryService._get_budget_instruments(payload)

        if not instruments:
            logger.warning(
                "No budget instruments found in EMA payload. "
                "event_id=%s, expected_path=%s",
                payload.get("event_id"),
                ("order_suggestion." "budget_filter.instruments"),
            )
            return None

        valid_instruments: list[dict[str, Any]] = []

        for instrument in instruments:
            if not isinstance(instrument, dict):
                continue

            instrument_key = str(instrument.get("instrument_key") or "").strip()

            trading_symbol = str(instrument.get("trading_symbol") or "").strip()

            if not instrument_key:
                logger.warning(
                    "Skipping budget instrument with " "missing instrument_key."
                )
                continue

            if not trading_symbol:
                logger.warning(
                    "Skipping budget instrument with "
                    "missing trading_symbol. "
                    "instrument_key=%s",
                    instrument_key,
                )
                continue

            raw_price = instrument.get("live_ltp")

            if raw_price is None:
                raw_price = instrument.get("ltp")

            if raw_price is None:
                raw_price = instrument.get("price")

            if raw_price is None:
                raw_price = instrument.get("last_price")

            if raw_price is None:
                market_data = instrument.get(
                    "market_data",
                    {},
                )

                if isinstance(market_data, dict):
                    raw_price = market_data.get("ltp")

            try:
                price = float(raw_price)
            except (
                TypeError,
                ValueError,
                OverflowError,
            ):
                logger.warning(
                    "Skipping budget instrument with "
                    "invalid price. instrument_key=%s, "
                    "price=%r",
                    instrument_key,
                    raw_price,
                )
                continue

            if price <= 0:
                logger.warning(
                    "Skipping budget instrument with "
                    "non-positive price. "
                    "instrument_key=%s, price=%s",
                    instrument_key,
                    price,
                )
                continue

            selected = deepcopy(instrument)
            selected["instrument_key"] = instrument_key
            selected["trading_symbol"] = trading_symbol
            selected["live_ltp"] = price

            valid_instruments.append(selected)

        if not valid_instruments:
            logger.warning(
                "No valid budget instruments available " "for order. event_id=%s",
                payload.get("event_id"),
            )
            return None

        selected = min(
            valid_instruments,
            key=lambda item: float(item["live_ltp"]),
        )

        logger.info(
            "Lowest-LTP budget instrument selected. "
            "event_id=%s, instrument_key=%s, "
            "trading_symbol=%s, live_ltp=%s, "
            "valid_candidates=%s",
            payload.get("event_id"),
            selected.get("instrument_key"),
            selected.get("trading_symbol"),
            selected.get("live_ltp"),
            len(valid_instruments),
        )

        return selected

    @staticmethod
    def _build_order_instrument(
        selected_instrument: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "instrument_key": (selected_instrument.get("instrument_key")),
            "trading_symbol": (selected_instrument.get("trading_symbol")),
            "live_ltp": selected_instrument.get("live_ltp"),
            "lot_size": selected_instrument.get("lot_size"),
        }

    @staticmethod
    def _normalize_order_result(
        raw_result: Any,
        order_instrument: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(raw_result, dict):
            return {
                "success": False,
                "order_status": ("INVALID_ORDER_SERVICE_RESPONSE"),
                "selected_instrument": deepcopy(order_instrument),
                "order_id": None,
                "error": ("Order service returned a " "non-dictionary response."),
            }

        normalized = deepcopy(raw_result)

        raw_status = (
            raw_result.get("order_status")
            or raw_result.get("status")
            or raw_result.get("state")
        )

        raw_success = raw_result.get("success")

        if raw_success is None:
            normalized_status = str(raw_status or "").strip().lower()

            raw_success = normalized_status in {
                "success",
                "successful",
                "placed",
                "accepted",
                "complete",
                "completed",
                "submitted",
                "open",
            }

        normalized["success"] = bool(raw_success)

        normalized["order_status"] = raw_status or (
            "SUCCESS" if normalized["success"] else "FAILED"
        )

        normalized["selected_instrument"] = raw_result.get(
            "selected_instrument"
        ) or deepcopy(order_instrument)

        normalized["order_id"] = (
            raw_result.get("order_id")
            or raw_result.get("orderId")
            or raw_result.get("id")
        )

        normalized["error"] = (
            raw_result.get("error") or raw_result.get("message")
            if not normalized["success"]
            else raw_result.get("error")
        )

        return normalized

    def _place_lowest_budget_instrument(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        selected = self._select_lowest_budget_instrument(payload)

        if not selected:
            return {
                "success": False,
                "order_status": ("NO_VALID_BUDGET_INSTRUMENT"),
                "selected_instrument": None,
                "order_id": None,
                "error": ("No valid lowest budget-range " "instrument was found."),
            }

        order_instrument = self._build_order_instrument(selected)

        if not order_instrument.get("instrument_key"):
            return {
                "success": False,
                "order_status": ("INVALID_ORDER_INSTRUMENT"),
                "selected_instrument": (order_instrument),
                "order_id": None,
                "error": ("Selected instrument is missing " "instrument_key."),
            }

        if not order_instrument.get("trading_symbol"):
            return {
                "success": False,
                "order_status": ("INVALID_ORDER_INSTRUMENT"),
                "selected_instrument": (order_instrument),
                "order_id": None,
                "error": ("Selected instrument is missing " "trading_symbol."),
            }

        logger.info(
            "Submitting sandbox order for lowest "
            "budget-range instrument. "
            "event_id=%s, instrument_key=%s, "
            "trading_symbol=%s, live_ltp=%s, "
            "lot_size=%s",
            payload.get("event_id"),
            order_instrument.get("instrument_key"),
            order_instrument.get("trading_symbol"),
            order_instrument.get("live_ltp"),
            order_instrument.get("lot_size"),
        )

        try:
            raw_result = process_selected_instrument(deepcopy(order_instrument))

            order_result = self._normalize_order_result(
                raw_result=raw_result,
                order_instrument=order_instrument,
            )

            logger.info(
                "Sandbox order workflow completed. "
                "event_id=%s, instrument_key=%s, "
                "success=%s, order_status=%s, "
                "order_id=%s, error=%s",
                payload.get("event_id"),
                order_instrument.get("instrument_key"),
                order_result.get("success"),
                order_result.get("order_status"),
                order_result.get("order_id"),
                order_result.get("error"),
            )

            return order_result

        except Exception as exc:
            logger.exception(
                "Lowest budget-range sandbox order "
                "processing failed. "
                "event_id=%s, instrument_key=%s",
                payload.get("event_id"),
                order_instrument.get("instrument_key"),
            )

            return {
                "success": False,
                "order_status": ("ORDER_PROCESSING_EXCEPTION"),
                "selected_instrument": (order_instrument),
                "order_id": None,
                "error": (f"{type(exc).__name__}: {exc}"),
            }

    @staticmethod
    def _get_simulation_flags(
        payload: dict[str, Any],
    ) -> tuple[bool, bool]:
        simulation_data = payload.get(
            "simulation",
            {},
        )

        if not isinstance(simulation_data, dict):
            simulation_data = {}

        is_simulation = bool(
            payload.get("is_simulation") or simulation_data.get("enabled")
        )

        is_dry_run = bool(simulation_data.get("dry_run"))

        return is_simulation, is_dry_run

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
        append_state_callback: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict) or not payload:
            return {
                "success": False,
                "accepted": False,
                "error": ("EMA payload is empty or invalid."),
            }

        event_id = payload.get("event_id")

        instrument = payload.get("instrument") or {}

        if not isinstance(instrument, dict):
            instrument = {}

        instrument_key = instrument.get("instrument_key")

        if order_enabled is None:
            order_enabled = bool(
                getattr(
                    config,
                    "PLACE_ORDER",
                    False,
                )
            )

        order_enabled = bool(order_enabled)

        is_simulation, is_dry_run = self._get_simulation_flags(payload)

        order_disable_reason = None

        if is_dry_run:
            order_enabled = False
            order_disable_reason = "DRY_RUN"

        elif is_simulation:
            order_enabled = False
            order_disable_reason = "SIMULATION"

        order_saving_enabled = bool(
            getattr(
                config,
                "UPSTOX_ORDER_ENABLED",
                True,
            )
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

        if order_disable_reason:
            result["order"]["order_status"] = f"DISABLED_{order_disable_reason}"
        elif not order_enabled:
            result["order"]["order_status"] = "DISABLED"

        result.update(
            {
                "event_id": event_id,
                "instrument_key": instrument_key,
                "is_simulation": is_simulation,
                "dry_run": is_dry_run,
                "error": None,
            }
        )

        notification_context = (
            f"ema_event" f"|event_id={event_id}" f"|instrument_key={instrument_key}"
        )

        logger.info(
            "EMA event processing started. "
            "event_id=%s, instrument_key=%s, "
            "telegram_enabled=%s, "
            "algo_app_enabled=%s, "
            "order_enabled=%s, save_event=%s, "
            "simulation=%s, dry_run=%s, "
            "budget_instruments=%s",
            event_id,
            instrument_key,
            telegram_enabled,
            algo_app_enabled,
            order_enabled,
            save_event,
            is_simulation,
            is_dry_run,
            len(self._get_budget_instruments(payload)),
        )

        if telegram_enabled:
            try:
                telegram_result = self._send_telegram(
                    title=telegram_title,
                    message=telegram_message,
                    level=telegram_level,
                    notification_context=(notification_context),
                )

                result["telegram"].update(telegram_result)

            except Exception as exc:
                result["telegram"]["error"] = f"{type(exc).__name__}: {exc}"

                result["errors"].append(f"Telegram delivery failed: {exc}")

                logger.exception(
                    "Telegram delivery workflow " "failed. event_id=%s",
                    event_id,
                )

        if algo_app_enabled:
            try:
                algo_result = self._send_algo_app(deepcopy(payload))

                result["algo_app"].update(algo_result)

                if telegram_enabled:
                    self._send_telegram(
                        title=("Algo App Delivery Completed"),
                        message=(
                            "Algo App delivery completed.\n"
                            f"Event ID: {event_id}\n"
                            f"Instrument: "
                            f"{instrument_key}\n"
                            f"Success: "
                            f"{algo_result.get('success', False)}"
                        ),
                        level=("SUCCESS" if algo_result.get("success") else "WARNING"),
                        notification_context=(
                            f"{notification_context}" f"|algo_app_completed"
                        ),
                    )

            except Exception as exc:
                result["algo_app"]["error"] = f"{type(exc).__name__}: {exc}"

                result["errors"].append(f"Algo App delivery failed: {exc}")

                logger.exception(
                    "Algo App delivery workflow " "failed. event_id=%s",
                    event_id,
                )

        if order_enabled:
            result["order"]["attempted"] = True

            try:
                order_result = self._place_lowest_budget_instrument(payload)

                if isinstance(order_result, dict):
                    result["order"].update(order_result)
                else:
                    result["order"].update(
                        {
                            "success": False,
                            "order_status": ("INVALID_ORDER_SERVICE_RESPONSE"),
                            "error": ("Order service returned " "an invalid response."),
                        }
                    )

                if telegram_enabled:
                    self._send_telegram(
                        title=("Order Placement " "Delivery Completed"),
                        message=(
                            "Sandbox order workflow "
                            "completed.\n"
                            f"Event ID: {event_id}\n"
                            f"EMA Instrument: "
                            f"{instrument_key}\n"
                            f"Selected Instrument: "
                            f"{result['order'].get('selected_instrument')}\n"
                            f"Order Status: "
                            f"{result['order'].get('order_status')}\n"
                            f"Order ID: "
                            f"{result['order'].get('order_id')}\n"
                            f"Success: "
                            f"{result['order'].get('success', False)}"
                        ),
                        level=(
                            "SUCCESS" if result["order"].get("success") else "WARNING"
                        ),
                        notification_context=(
                            f"{notification_context}" f"|order_completed"
                        ),
                    )

            except Exception as exc:
                result["order"].update(
                    {
                        "success": False,
                        "order_status": ("ORDER_WORKFLOW_EXCEPTION"),
                        "error": (f"{type(exc).__name__}: " f"{exc}"),
                    }
                )

                result["errors"].append(f"Order placement failed: {exc}")

                logger.exception(
                    "Order placement workflow " "failed. event_id=%s",
                    event_id,
                )

            if order_saving_enabled:
                result["order_mongo"]["attempted"] = True

                try:
                    order_mongo_result = self._save_order_result(
                        payload=payload,
                        order_result=deepcopy(result["order"]),
                    )

                    result["order_mongo"].update(order_mongo_result)

                    if not order_mongo_result.get("saved"):
                        result["warnings"].append(
                            "Order result was not saved " "in the order collection."
                        )

                except Exception as exc:
                    result["order_mongo"]["error"] = f"{type(exc).__name__}: {exc}"

                    result["errors"].append("Order result saving failed: " f"{exc}")

                    logger.exception(
                        "Order result saving workflow " "failed. event_id=%s",
                        event_id,
                    )

        notification_accepted = bool(
            result["telegram"].get("success") or result["algo_app"].get("success")
        )

        order_accepted = bool(result["order"].get("success"))

        delivery_accepted = bool(notification_accepted or order_accepted)

        result["notification_accepted"] = notification_accepted
        result["order_accepted"] = order_accepted
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
                    append_callback=(append_state_callback),
                )

                result["state"].update(state_result)

                if telegram_enabled:
                    self._send_telegram(
                        title=("State Update " "Delivery Completed"),
                        message=(
                            "In-memory state update "
                            "completed.\n"
                            f"Event ID: {event_id}\n"
                            f"Success: "
                            f"{state_result.get('success', False)}"
                        ),
                        level=("SUCCESS" if state_result.get("success") else "WARNING"),
                        notification_context=(
                            f"{notification_context}" f"|state_completed"
                        ),
                    )

            except Exception as exc:
                result["state"]["error"] = f"{type(exc).__name__}: {exc}"

                result["errors"].append(f"State update failed: {exc}")

                logger.exception(
                    "State update workflow failed. " "event_id=%s",
                    event_id,
                )

        if save_event:
            result["mongo"]["attempted"] = True

            try:
                if processing_status is None:
                    processing_status = (
                        "delivery_accepted"
                        if delivery_accepted
                        else "delivery_not_accepted"
                    )

                isolated_processing_result = deepcopy(result)

                isolated_processing_result.pop(
                    "order",
                    None,
                )
                isolated_processing_result.pop(
                    "order_mongo",
                    None,
                )
                isolated_processing_result.pop(
                    "mongo",
                    None,
                )

                mongo_result = self._save_event(
                    payload=payload,
                    processing_result=(isolated_processing_result),
                    processing_status=(processing_status),
                )

                result["mongo"].update(mongo_result)

                if telegram_enabled:
                    self._send_telegram(
                        title=("MongoDB Event " "Saving Completed"),
                        message=(
                            "MongoDB event saving "
                            "completed.\n"
                            f"Event ID: {event_id}\n"
                            f"Saved: "
                            f"{mongo_result.get('saved', False)}\n"
                            f"Success: "
                            f"{mongo_result.get('success', False)}"
                        ),
                        level=("SUCCESS" if mongo_result.get("saved") else "WARNING"),
                        notification_context=(
                            f"{notification_context}" f"|mongo_completed"
                        ),
                    )

                if not mongo_result.get("saved") and not mongo_result.get("skipped"):
                    result["warnings"].append(
                        "Event processing completed, " "but the event was not saved."
                    )

            except Exception as exc:
                result["mongo"]["error"] = f"{type(exc).__name__}: {exc}"

                result["errors"].append(f"MongoDB saving failed: {exc}")

                logger.exception(
                    "MongoDB saving workflow failed. " "event_id=%s",
                    event_id,
                )

        if result["errors"]:
            result["error"] = "; ".join(result["errors"])

        logger.info(
            "EMA event processing completed. "
            "event_id=%s, instrument_key=%s, "
            "accepted=%s, "
            "notification_accepted=%s, "
            "order_accepted=%s, "
            "telegram_enabled=%s, "
            "telegram_attempted=%s, "
            "telegram_success=%s, "
            "algo_app_enabled=%s, "
            "algo_app_attempted=%s, "
            "algo_app_success=%s, "
            "order_enabled=%s, "
            "order_attempted=%s, "
            "order_success=%s, "
            "order_status=%s, "
            "order_id=%s, "
            "order_error=%s, "
            "order_mongo_attempted=%s, "
            "order_mongo_saved=%s, "
            "mongo_attempted=%s, "
            "mongo_saved=%s, "
            "state_updated=%s, "
            "simulation=%s, dry_run=%s, "
            "warnings=%s, errors=%s",
            event_id,
            instrument_key,
            result["accepted"],
            result["notification_accepted"],
            result["order_accepted"],
            result["telegram"]["enabled"],
            result["telegram"]["attempted"],
            result["telegram"]["success"],
            result["algo_app"]["enabled"],
            result["algo_app"]["attempted"],
            result["algo_app"]["success"],
            result["order"]["enabled"],
            result["order"]["attempted"],
            result["order"]["success"],
            result["order"].get("order_status"),
            result["order"].get("order_id"),
            result["order"].get("error"),
            result["order_mongo"]["attempted"],
            result["order_mongo"]["saved"],
            result["mongo"]["attempted"],
            result["mongo"]["saved"],
            result["state"]["success"],
            is_simulation,
            is_dry_run,
            len(result["warnings"]),
            len(result["errors"]),
        )

        return result


ema_event_delivery_service = EMAEventDeliveryService()
