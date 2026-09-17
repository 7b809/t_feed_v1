from typing import Any

from core import config
from core.logger import get_logger
from services.telegram_service import telegram_service
from upstox_services.place_order import place_selected_instrument
from upstox_services.position_service import exit_all_positions

logger = get_logger(__file__)


def _send_telegram_message(
    title: str,
    message: str,
    level: str = "INFO",
    notification_context: str = "process_order",
) -> None:
    try:
        success = telegram_service.send_message(
            title=title,
            message=message,
            level=level,
            notification_context=notification_context,
        )

        if not success:
            logger.warning(
                "Telegram message was not sent. " "title=%s, context=%s",
                title,
                notification_context,
            )

    except Exception:
        logger.exception(
            "Unexpected error while sending Telegram message. " "title=%s, context=%s",
            title,
            notification_context,
        )


def _safe_float(
    value: Any,
) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_int(
    value: Any,
) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _build_instrument_details(
    selected_instrument: dict[str, Any],
) -> dict[str, Any]:
    live_ltp = _safe_float(selected_instrument.get("live_ltp"))

    if live_ltp is None:
        live_ltp = _safe_float(selected_instrument.get("ltp"))

    lot_size = _safe_int(selected_instrument.get("lot_size"))

    return {
        "instrument_key": str(selected_instrument.get("instrument_key") or "").strip()
        or None,
        "trading_symbol": str(selected_instrument.get("trading_symbol") or "").strip()
        or None,
        "live_ltp": live_ltp,
        "lot_size": lot_size,
    }


def _build_failure_result(
    order_status: str,
    instrument_details: dict[str, Any] | None,
    error: str,
    exit_result: Any = None,
    place_order_result: Any = None,
    executed: bool = False,
) -> dict[str, Any]:
    return {
        "success": False,
        "executed": bool(executed),
        "skipped": False,
        "order_status": order_status,
        "order_id": None,
        "selected_instrument": instrument_details,
        "exit_result": exit_result,
        "place_order_result": place_order_result,
        "error": error,
    }


def _build_skipped_result(
    order_status: str,
    instrument_details: dict[str, Any] | None,
    reason: str,
) -> dict[str, Any]:
    return {
        "success": False,
        "executed": False,
        "skipped": True,
        "order_status": order_status,
        "order_id": None,
        "reason": reason,
        "selected_instrument": instrument_details,
        "exit_result": None,
        "place_order_result": None,
        "error": None,
    }


def _extract_order_id(
    place_order_result: dict[str, Any] | None,
) -> str | None:
    if not isinstance(place_order_result, dict):
        return None

    direct_order_id = (
        place_order_result.get("order_id")
        or place_order_result.get("orderId")
        or place_order_result.get("id")
    )

    if direct_order_id is not None:
        normalized_order_id = str(direct_order_id).strip()

        if normalized_order_id:
            return normalized_order_id

    data = place_order_result.get("data")

    if isinstance(data, dict):
        data_order_id = data.get("order_id") or data.get("orderId") or data.get("id")

        if data_order_id is not None:
            normalized_order_id = str(data_order_id).strip()

            if normalized_order_id:
                return normalized_order_id

    response = place_order_result.get("response")

    if isinstance(response, dict):
        response_order_id = (
            response.get("order_id") or response.get("orderId") or response.get("id")
        )

        if response_order_id is not None:
            normalized_order_id = str(response_order_id).strip()

            if normalized_order_id:
                return normalized_order_id

        response_data = response.get("data")

        if isinstance(response_data, dict):
            response_data_order_id = (
                response_data.get("order_id")
                or response_data.get("orderId")
                or response_data.get("id")
            )

            if response_data_order_id is not None:
                normalized_order_id = str(response_data_order_id).strip()

                if normalized_order_id:
                    return normalized_order_id

    return None


def process_selected_instrument(
    selected_instrument: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(selected_instrument, dict) or not selected_instrument:
        logger.warning("Order workflow stopped. No instrument selected.")

        _send_telegram_message(
            title="Order Workflow Stopped",
            message="No instrument was selected.",
            level="WARNING",
            notification_context=("process_order|no_instrument"),
        )

        return _build_failure_result(
            order_status="NO_INSTRUMENT",
            instrument_details=None,
            error="No instrument selected.",
            executed=False,
        )

    instrument_details = _build_instrument_details(selected_instrument)

    trading_symbol = instrument_details.get("trading_symbol")
    instrument_key = instrument_details.get("instrument_key")
    live_ltp = instrument_details.get("live_ltp")
    lot_size = instrument_details.get("lot_size")

    place_order_enabled = bool(
        getattr(
            config,
            "PLACE_ORDER",
            False,
        )
    )

    logger.info(
        "Order workflow started. "
        "trading_symbol=%s, instrument_key=%s, "
        "live_ltp=%s, lot_size=%s, "
        "place_order_enabled=%s",
        trading_symbol,
        instrument_key,
        live_ltp,
        lot_size,
        place_order_enabled,
    )

    _send_telegram_message(
        title="Order Workflow Started",
        message=(
            f"Symbol: {trading_symbol}\n"
            f"Instrument: {instrument_key}\n"
            f"Live LTP: {live_ltp}\n"
            f"Lot Size: {lot_size}\n"
            f"Place Order: "
            f"{'ENABLED' if place_order_enabled else 'DISABLED'}"
        ),
        level="STARTUP",
        notification_context=(
            "process_order|started|" f"instrument_key={instrument_key}"
        ),
    )

    if not place_order_enabled:
        logger.info(
            "Order execution skipped. "
            "PLACE_ORDER=false, trading_symbol=%s, "
            "instrument_key=%s",
            trading_symbol,
            instrument_key,
        )

        _send_telegram_message(
            title="Order Execution Skipped",
            message=(
                f"Symbol: {trading_symbol}\n"
                f"Instrument: {instrument_key}\n"
                f"Live LTP: {live_ltp}\n"
                f"Lot Size: {lot_size}\n"
                "PLACE_ORDER=false"
            ),
            level="INFO",
            notification_context=(
                "process_order|disabled|" f"instrument_key={instrument_key}"
            ),
        )

        return _build_skipped_result(
            order_status="DISABLED",
            instrument_details=instrument_details,
            reason="PLACE_ORDER=false",
        )

    validation_errors = []

    if not trading_symbol:
        validation_errors.append("trading_symbol is missing")

    if not instrument_key:
        validation_errors.append("instrument_key is missing")

    if live_ltp is None or live_ltp <= 0:
        validation_errors.append("live_ltp must be a positive number")

    if lot_size is None or lot_size <= 0:
        validation_errors.append("lot_size must be a positive integer")

    if validation_errors:
        validation_error = "; ".join(validation_errors)

        logger.error(
            "Order workflow stopped. Invalid instrument "
            "details. trading_symbol=%s, "
            "instrument_key=%s, live_ltp=%s, "
            "lot_size=%s, validation_error=%s",
            trading_symbol,
            instrument_key,
            live_ltp,
            lot_size,
            validation_error,
        )

        _send_telegram_message(
            title="Invalid Selected Instrument",
            message=(
                "Required instrument details are invalid.\n"
                f"Symbol: {trading_symbol}\n"
                f"Instrument Key: {instrument_key}\n"
                f"Live LTP: {live_ltp}\n"
                f"Lot Size: {lot_size}\n"
                f"Validation Error: {validation_error}\n"
                "The order workflow was stopped."
            ),
            level="ERROR",
            notification_context=(
                "process_order|invalid_instrument|" f"instrument_key={instrument_key}"
            ),
        )

        return _build_failure_result(
            order_status="INVALID_INSTRUMENT",
            instrument_details=instrument_details,
            error=validation_error,
            executed=False,
        )

    normalized_instrument = {
        "instrument_key": instrument_key,
        "trading_symbol": trading_symbol,
        "live_ltp": live_ltp,
        "lot_size": lot_size,
    }

    logger.info(
        "Step 1 started. Exiting all existing positions. " "instrument_key=%s",
        instrument_key,
    )

    _send_telegram_message(
        title="Exiting Existing Positions",
        message=(
            "Checking and exiting all existing positions.\n"
            f"New Symbol: {trading_symbol}\n"
            f"Target LTP: {live_ltp}"
        ),
        level="REFRESH",
        notification_context=(
            "process_order|exit_started|" f"instrument_key={instrument_key}"
        ),
    )

    try:
        exit_result = exit_all_positions()

    except Exception as exc:
        logger.exception(
            "Exception while exiting positions. "
            "trading_symbol=%s, instrument_key=%s, "
            "exception_type=%s, error=%s",
            trading_symbol,
            instrument_key,
            type(exc).__name__,
            exc,
        )

        _send_telegram_message(
            title="Exit Positions Exception",
            message=(
                f"Symbol: {trading_symbol}\n"
                f"Instrument: {instrument_key}\n"
                f"Error Type: {type(exc).__name__}\n"
                f"Error: {exc}\n"
                "The new order will not be placed."
            ),
            level="ERROR",
            notification_context=(
                "process_order|exit_exception|" f"instrument_key={instrument_key}"
            ),
        )

        return _build_failure_result(
            order_status="EXIT_FAILED",
            instrument_details=(normalized_instrument),
            error=(
                f"Position exit raised an exception: " f"{type(exc).__name__}: {exc}"
            ),
            executed=True,
        )

    if not isinstance(exit_result, dict):
        logger.error(
            "Position exit returned an invalid response. "
            "response_type=%s, response=%r",
            type(exit_result).__name__,
            exit_result,
        )

        _send_telegram_message(
            title="Invalid Position Exit Response",
            message=(
                f"Symbol: {trading_symbol}\n"
                f"Instrument: {instrument_key}\n"
                f"Response Type: "
                f"{type(exit_result).__name__}\n"
                "The position service returned an invalid "
                "response. The new order will not be placed."
            ),
            level="ERROR",
            notification_context=(
                "process_order|invalid_exit_response|"
                f"instrument_key={instrument_key}"
            ),
        )

        return _build_failure_result(
            order_status="EXIT_FAILED",
            instrument_details=(normalized_instrument),
            exit_result=exit_result,
            error="Invalid position exit response.",
            executed=True,
        )

    if exit_result.get("success") is not True:
        exit_error = (
            exit_result.get("error")
            or exit_result.get("message")
            or "Unknown position exit error."
        )

        logger.error(
            "Position exit failed. "
            "trading_symbol=%s, instrument_key=%s, "
            "error=%s",
            trading_symbol,
            instrument_key,
            exit_error,
        )

        _send_telegram_message(
            title="Exit Positions Failed",
            message=(
                f"Symbol: {trading_symbol}\n"
                f"Instrument: {instrument_key}\n"
                f"Error: {exit_error}\n"
                "The new order will not be placed."
            ),
            level="ERROR",
            notification_context=(
                "process_order|exit_failed|" f"instrument_key={instrument_key}"
            ),
        )

        return _build_failure_result(
            order_status="EXIT_FAILED",
            instrument_details=(normalized_instrument),
            exit_result=exit_result,
            error=str(exit_error),
            executed=True,
        )

    logger.info(
        "All existing positions exited successfully. "
        "trading_symbol=%s, instrument_key=%s",
        trading_symbol,
        instrument_key,
    )

    _send_telegram_message(
        title="Positions Exited Successfully",
        message=(
            "All existing positions were exited successfully.\n"
            f"Next Symbol: {trading_symbol}\n"
            f"Live LTP: {live_ltp}\n"
            "Proceeding to place the new order."
        ),
        level="SUCCESS",
        notification_context=(
            "process_order|exit_success|" f"instrument_key={instrument_key}"
        ),
    )

    logger.info(
        "Step 2 started. Placing new order. " "trading_symbol=%s, instrument_key=%s",
        trading_symbol,
        instrument_key,
    )

    _send_telegram_message(
        title="Placing New Order",
        message=(
            "Sending the order to Upstox.\n"
            f"Symbol: {trading_symbol}\n"
            f"Instrument: {instrument_key}\n"
            f"Live LTP: {live_ltp}\n"
            f"Lot Size: {lot_size}"
        ),
        level="INFO",
        notification_context=(
            "process_order|placement_started|" f"instrument_key={instrument_key}"
        ),
    )

    try:
        place_order_result = place_selected_instrument(normalized_instrument)

    except Exception as exc:
        logger.exception(
            "Exception while placing order. "
            "trading_symbol=%s, instrument_key=%s, "
            "exception_type=%s, error=%s",
            trading_symbol,
            instrument_key,
            type(exc).__name__,
            exc,
        )

        _send_telegram_message(
            title="Order Placement Exception",
            message=(
                f"Symbol: {trading_symbol}\n"
                f"Instrument: {instrument_key}\n"
                f"Error Type: {type(exc).__name__}\n"
                f"Error: {exc}\n"
                "Order placement encountered an exception."
            ),
            level="ERROR",
            notification_context=(
                "process_order|placement_exception|" f"instrument_key={instrument_key}"
            ),
        )

        return _build_failure_result(
            order_status="PLACE_ORDER_FAILED",
            instrument_details=(normalized_instrument),
            exit_result=exit_result,
            error=(
                "Order placement raised an exception: " f"{type(exc).__name__}: {exc}"
            ),
            executed=True,
        )

    if not isinstance(place_order_result, dict):
        logger.error(
            "Order placement returned an invalid response. "
            "response_type=%s, response=%r",
            type(place_order_result).__name__,
            place_order_result,
        )

        _send_telegram_message(
            title="Invalid Order Placement Response",
            message=(
                f"Symbol: {trading_symbol}\n"
                f"Instrument: {instrument_key}\n"
                f"Response Type: "
                f"{type(place_order_result).__name__}\n"
                "The order service returned an invalid response."
            ),
            level="ERROR",
            notification_context=(
                "process_order|invalid_placement_response|"
                f"instrument_key={instrument_key}"
            ),
        )

        return _build_failure_result(
            order_status="PLACE_ORDER_FAILED",
            instrument_details=(normalized_instrument),
            exit_result=exit_result,
            place_order_result=place_order_result,
            error="Invalid order placement response.",
            executed=True,
        )

    if place_order_result.get("success") is not True:
        order_error = (
            place_order_result.get("error")
            or place_order_result.get("message")
            or "Unknown order placement error."
        )

        logger.error(
            "Order placement failed. "
            "trading_symbol=%s, instrument_key=%s, "
            "error=%s",
            trading_symbol,
            instrument_key,
            order_error,
        )

        _send_telegram_message(
            title="Order Placement Failed",
            message=(
                f"Symbol: {trading_symbol}\n"
                f"Instrument: {instrument_key}\n"
                f"Live LTP: {live_ltp}\n"
                f"Lot Size: {lot_size}\n"
                f"Error: {order_error}"
            ),
            level="ERROR",
            notification_context=(
                "process_order|placement_failed|" f"instrument_key={instrument_key}"
            ),
        )

        return _build_failure_result(
            order_status="PLACE_ORDER_FAILED",
            instrument_details=(normalized_instrument),
            exit_result=exit_result,
            place_order_result=place_order_result,
            error=str(order_error),
            executed=True,
        )

    order_id = _extract_order_id(place_order_result)

    logger.info(
        "Order workflow completed successfully. "
        "trading_symbol=%s, instrument_key=%s, "
        "order_id=%s",
        trading_symbol,
        instrument_key,
        order_id,
    )

    _send_telegram_message(
        title="Order Placed Successfully",
        message=(
            f"Symbol: {trading_symbol}\n"
            f"Instrument: {instrument_key}\n"
            f"Live LTP: {live_ltp}\n"
            f"Lot Size: {lot_size}\n"
            f"Order ID: {order_id or 'N/A'}\n\n"
            "Existing positions exited successfully.\n"
            "New order placed successfully.\n"
            "Order workflow completed."
        ),
        level="SUCCESS",
        notification_context=(
            "process_order|completed|" f"instrument_key={instrument_key}"
        ),
    )

    return {
        "success": True,
        "executed": True,
        "skipped": False,
        "order_status": "ORDER_PLACED",
        "order_id": order_id,
        "selected_instrument": (normalized_instrument),
        "exit_result": exit_result,
        "place_order_result": (place_order_result),
        "error": None,
    }
