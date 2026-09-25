from typing import Any
from uuid import uuid4

from core import config
from core.logger import get_logger
from services.telegram_service import telegram_service
from upstox_services.margin_service import margin_service
from upstox_services.place_order import place_selected_instrument
from upstox_services.position_service import exit_all_positions

logger = get_logger(__file__)


def _is_dummy_orders_enabled() -> bool:
    """
    Returns True when dummy order mode is enabled.
    Supports both lowercase and uppercase configuration names.
    """
    return bool(
        getattr(config, "dummy_orders", False) or getattr(config, "DUMMY_ORDERS", False)
    )


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
                "Telegram message was not sent. title=%s, context=%s",
                title,
                notification_context,
            )

    except Exception:
        logger.exception(
            "Unexpected error while sending Telegram message. title=%s, context=%s",
            title,
            notification_context,
        )


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_int(value: Any) -> int | None:
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
    margin_result: Any = None,
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
        "margin_result": margin_result,
        "place_order_result": place_order_result,
        "error": error,
    }


def _build_skipped_result(
    order_status: str,
    instrument_details: dict[str, Any] | None,
    reason: str,
    margin_result: Any = None,
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
        "margin_result": margin_result,
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


def _calculate_margin(
    instrument_details: dict[str, Any],
    *,
    transaction_type: str = "BUY",
) -> dict[str, Any]:
    """
    Calculates margin for the given instrument using the margin service.

    This is a best-effort step: any failure is logged and notified via
    Telegram, but it does NOT block the order workflow.

    Returns the normalized margin result dictionary produced by
    margin_service.calculate_margin().
    """
    instrument_key = instrument_details.get("instrument_key")
    lot_size = instrument_details.get("lot_size")

    quantity = lot_size if lot_size and lot_size > 0 else margin_service.DEFAULT_QUANTITY

    logger.info(
        "Step 2.5 started. Calculating margin. "
        "instrument_key=%s, quantity=%s, transaction_type=%s",
        instrument_key,
        quantity,
        transaction_type,
    )

    _send_telegram_message(
        title="Calculating Margin",
        message=(
            f"Instrument: {instrument_key}\n"
            f"Quantity: {quantity}\n"
            f"Product: {margin_service.DEFAULT_PRODUCT}\n"
            f"Transaction Type: {transaction_type}"
        ),
        level="INFO",
        notification_context=(
            f"process_order|margin_calculation_started|instrument_key={instrument_key}"
        ),
    )

    try:
        margin_result = margin_service.calculate_margin(
            instrument_key=instrument_key or "",
            quantity=quantity,
            product=margin_service.DEFAULT_PRODUCT,
            transaction_type=transaction_type,
        )

    except Exception as exc:
        logger.exception(
            "Unexpected exception while calculating margin. "
            "instrument_key=%s, exception_type=%s, error=%s",
            instrument_key,
            type(exc).__name__,
            exc,
        )

        _send_telegram_message(
            title="Margin Calculation Exception",
            message=(
                f"Instrument: {instrument_key}\n"
                f"Error Type: {type(exc).__name__}\n"
                f"Error: {exc}\n"
                "Continuing with order workflow."
            ),
            level="ERROR",
            notification_context=(
                f"process_order|margin_exception|instrument_key={instrument_key}"
            ),
        )

        return {
            "success": False,
            "instrument_key": instrument_key,
            "quantity": quantity,
            "product": margin_service.DEFAULT_PRODUCT,
            "transaction_type": transaction_type,
            "margin": None,
            "required_margin": None,
            "available_margin": None,
            "raw_response": None,
            "error": f"{type(exc).__name__}: {exc}",
        }

    if not isinstance(margin_result, dict):
        logger.error(
            "Margin service returned an invalid response. "
            "response_type=%s, response=%r",
            type(margin_result).__name__,
            margin_result,
        )

        _send_telegram_message(
            title="Invalid Margin Response",
            message=(
                f"Instrument: {instrument_key}\n"
                f"Response Type: {type(margin_result).__name__}\n"
                "Continuing with order workflow."
            ),
            level="ERROR",
            notification_context=(
                f"process_order|invalid_margin_response|instrument_key={instrument_key}"
            ),
        )

        return {
            "success": False,
            "instrument_key": instrument_key,
            "quantity": quantity,
            "product": margin_service.DEFAULT_PRODUCT,
            "transaction_type": transaction_type,
            "margin": None,
            "required_margin": None,
            "available_margin": None,
            "raw_response": None,
            "error": "Invalid margin service response.",
        }

    if margin_result.get("success") is True:
        logger.info(
            "Margin calculated successfully. "
            "instrument_key=%s, required_margin=%s, available_margin=%s",
            instrument_key,
            margin_result.get("required_margin"),
            margin_result.get("available_margin"),
        )

        _send_telegram_message(
            title="Margin Calculated",
            message=(
                f"Instrument: {instrument_key}\n"
                f"Quantity: {quantity}\n"
                f"Required Margin: {margin_result.get('required_margin')}\n"
                f"Available Margin: {margin_result.get('available_margin')}"
            ),
            level="SUCCESS",
            notification_context=(
                f"process_order|margin_success|instrument_key={instrument_key}"
            ),
        )

    else:
        logger.warning(
            "Margin calculation failed. "
            "instrument_key=%s, error=%s",
            instrument_key,
            margin_result.get("error"),
        )

        _send_telegram_message(
            title="Margin Calculation Failed",
            message=(
                f"Instrument: {instrument_key}\n"
                f"Error: {margin_result.get('error')}\n"
                "Continuing with order workflow."
            ),
            level="WARNING",
            notification_context=(
                f"process_order|margin_failed|instrument_key={instrument_key}"
            ),
        )

    return margin_result


def _build_dummy_place_order_result(
    normalized_instrument: dict[str, Any],
    instrument_details: dict[str, Any],
) -> dict[str, Any]:
    """
    Builds a simulated (dummy) order placement response.

    No real order is sent to Upstox when dummy order mode is enabled.
    """
    trading_symbol = instrument_details.get("trading_symbol")
    instrument_key = instrument_details.get("instrument_key")
    live_ltp = instrument_details.get("live_ltp")
    lot_size = instrument_details.get("lot_size")

    dummy_order_id = f"DUMMY-{uuid4().hex[:12].upper()}"

    logger.info(
        "Dummy order mode enabled. Skipping real order placement. "
        "trading_symbol=%s, instrument_key=%s, live_ltp=%s, "
        "lot_size=%s, dummy_order_id=%s",
        trading_symbol,
        instrument_key,
        live_ltp,
        lot_size,
        dummy_order_id,
    )

    _send_telegram_message(
        title="Dummy Order Simulated",
        message=(
            "Dummy order mode is ENABLED.\n"
            "No real order was sent to Upstox.\n"
            f"Symbol: {trading_symbol}\n"
            f"Instrument: {instrument_key}\n"
            f"Live LTP: {live_ltp}\n"
            f"Lot Size: {lot_size}\n"
            f"Dummy Order ID: {dummy_order_id}"
        ),
        level="INFO",
        notification_context=(
            f"process_order|dummy_placement|instrument_key={instrument_key}"
        ),
    )

    return {
        "success": True,
        "dummy": True,
        "order_id": dummy_order_id,
        "message": "Dummy order simulated. No real order was sent to Upstox.",
        "instrument": normalized_instrument,
    }


def _safe_place_order(
    normalized_instrument: dict[str, Any],
    instrument_details: dict[str, Any],
    exit_result: Any,
    margin_result: Any = None,
) -> dict[str, Any]:
    """
    Wraps place_selected_instrument so that exceptions are logged,
    notified via Telegram, and converted to a structured failure result.

    When dummy order mode is enabled (config.dummy_orders = True or config.DUMMY_ORDERS = True), the real
    place_selected_instrument() call is skipped and a simulated success
    result is returned instead.
    """
    trading_symbol = instrument_details.get("trading_symbol")
    instrument_key = instrument_details.get("instrument_key")
    live_ltp = instrument_details.get("live_ltp")
    lot_size = instrument_details.get("lot_size")

    dummy_orders_enabled = _is_dummy_orders_enabled()

    _send_telegram_message(
        title="Placing New Order",
        message=(
            "Sending the order to Upstox.\n"
            f"Symbol: {trading_symbol}\n"
            f"Instrument: {instrument_key}\n"
            f"Live LTP: {live_ltp}\n"
            f"Lot Size: {lot_size}\n"
            f"Mode: {'DUMMY (simulated)' if dummy_orders_enabled else 'REAL'}"
        ),
        level="INFO",
        notification_context=(
            f"process_order|placement_started|instrument_key={instrument_key}"
        ),
    )

    # ------------------------------------------------------------------
    # DUMMY ORDER MODE: skip the real order placement entirely.
    # ------------------------------------------------------------------
    if dummy_orders_enabled:
        place_order_result = _build_dummy_place_order_result(
            normalized_instrument=normalized_instrument,
            instrument_details=instrument_details,
        )

        order_id = _extract_order_id(place_order_result)

        logger.info(
            "Dummy order workflow completed successfully. "
            "trading_symbol=%s, instrument_key=%s, order_id=%s",
            trading_symbol,
            instrument_key,
            order_id,
        )

        _send_telegram_message(
            title="Dummy Order Placed Successfully",
            message=(
                f"Symbol: {trading_symbol}\n"
                f"Instrument: {instrument_key}\n"
                f"Live LTP: {live_ltp}\n"
                f"Lot Size: {lot_size}\n"
                f"Dummy Order ID: {order_id or 'N/A'}\n\n"
                "Dummy mode: exit step simulated.\n"
                "Dummy mode: order placement simulated.\n"
                "No real Upstox calls were made."
            ),
            level="SUCCESS",
            notification_context=(
                f"process_order|dummy_completed|instrument_key={instrument_key}"
            ),
        )

        return {
            "success": True,
            "executed": True,
            "skipped": False,
            "dummy": True,
            "order_status": "DUMMY_ORDER_PLACED",
            "order_id": order_id,
            "selected_instrument": normalized_instrument,
            "exit_result": exit_result,
            "margin_result": margin_result,
            "place_order_result": place_order_result,
            "error": None,
        }

    # ------------------------------------------------------------------
    # REAL ORDER MODE: existing behaviour.
    # ------------------------------------------------------------------
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
                f"process_order|placement_exception|instrument_key={instrument_key}"
            ),
        )

        return _build_failure_result(
            order_status="PLACE_ORDER_FAILED",
            instrument_details=normalized_instrument,
            exit_result=exit_result,
            margin_result=margin_result,
            error=f"Order placement raised an exception: {type(exc).__name__}: {exc}",
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
                f"Response Type: {type(place_order_result).__name__}\n"
                "The order service returned an invalid response."
            ),
            level="ERROR",
            notification_context=(
                f"process_order|invalid_placement_response|instrument_key={instrument_key}"
            ),
        )

        return _build_failure_result(
            order_status="PLACE_ORDER_FAILED",
            instrument_details=normalized_instrument,
            exit_result=exit_result,
            margin_result=margin_result,
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
            "Order placement failed. " "trading_symbol=%s, instrument_key=%s, error=%s",
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
                f"process_order|placement_failed|instrument_key={instrument_key}"
            ),
        )

        return _build_failure_result(
            order_status="PLACE_ORDER_FAILED",
            instrument_details=normalized_instrument,
            exit_result=exit_result,
            margin_result=margin_result,
            place_order_result=place_order_result,
            error=str(order_error),
            executed=True,
        )

    order_id = _extract_order_id(place_order_result)

    logger.info(
        "Order workflow completed successfully. "
        "trading_symbol=%s, instrument_key=%s, order_id=%s",
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
            f"process_order|completed|instrument_key={instrument_key}"
        ),
    )

    return {
        "success": True,
        "executed": True,
        "skipped": False,
        "order_status": "ORDER_PLACED",
        "order_id": order_id,
        "selected_instrument": normalized_instrument,
        "exit_result": exit_result,
        "margin_result": margin_result,
        "place_order_result": place_order_result,
        "error": None,
    }


def _run_exit_step(
    instrument_details: dict[str, Any],
) -> Any:
    """
    Runs the exit-all-positions step.

    During the current testing phase, the exit step is assumed to behave
    as intended when the token is valid. Any exception or non-success
    response from the exit service is logged and notified via Telegram,
    but the workflow continues to place the new order.

    When dummy order mode is enabled (config.dummy_orders = True or config.DUMMY_ORDERS = True), the real
    exit_all_positions() call is skipped and a simulated success result is
    returned instead.

    Returns the (possibly best-effort) exit_result for downstream
    reporting purposes.
    """
    trading_symbol = instrument_details.get("trading_symbol")
    instrument_key = instrument_details.get("instrument_key")
    live_ltp = instrument_details.get("live_ltp")

    dummy_orders_enabled = _is_dummy_orders_enabled()

    logger.info(
        "Step 1 started. Exiting all existing positions. instrument_key=%s, dummy=%s",
        instrument_key,
        dummy_orders_enabled,
    )

    _send_telegram_message(
        title="Exiting Existing Positions",
        message=(
            "Checking and exiting all existing positions.\n"
            f"New Symbol: {trading_symbol}\n"
            f"Target LTP: {live_ltp}\n"
            f"Mode: {'DUMMY (simulated)' if dummy_orders_enabled else 'REAL'}"
        ),
        level="REFRESH",
        notification_context=(
            f"process_order|exit_started|instrument_key={instrument_key}"
        ),
    )

    # ------------------------------------------------------------------
    # DUMMY ORDER MODE: skip the real exit_all_positions() call.
    # ------------------------------------------------------------------
    if dummy_orders_enabled:
        logger.info(
            "Dummy order mode enabled. Skipping real exit_all_positions() call. "
            "trading_symbol=%s, instrument_key=%s",
            trading_symbol,
            instrument_key,
        )

        _send_telegram_message(
            title="Dummy Exit Simulated",
            message=(
                "Dummy order mode is ENABLED.\n"
                "No real exit_all_positions() call was made.\n"
                f"New Symbol: {trading_symbol}\n"
                f"Instrument: {instrument_key}\n"
                f"Live LTP: {live_ltp}\n"
                "Proceeding to simulate the new order placement."
            ),
            level="INFO",
            notification_context=(
                f"process_order|dummy_exit|instrument_key={instrument_key}"
            ),
        )

        return {
            "success": True,
            "dummy": True,
            "assumed": True,
            "positions_exited": 0,
            "message": ("Dummy mode: real exit_all_positions() call was skipped."),
            "error": None,
        }

    # ------------------------------------------------------------------
    # REAL MODE: existing behaviour.
    # ------------------------------------------------------------------
    exit_result: Any = None

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
                "Continuing with order placement (test mode: exit step assumed OK)."
            ),
            level="ERROR",
            notification_context=(
                f"process_order|exit_exception|instrument_key={instrument_key}"
            ),
        )

        return {
            "success": True,
            "assumed": True,
            "error": None,
            "exception": f"{type(exc).__name__}: {exc}",
        }

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
                f"Response Type: {type(exit_result).__name__}\n"
                "Continuing with order placement (test mode: exit step assumed OK)."
            ),
            level="ERROR",
            notification_context=(
                f"process_order|invalid_exit_response|instrument_key={instrument_key}"
            ),
        )

        return {
            "success": True,
            "assumed": True,
            "error": None,
            "invalid_response": repr(exit_result),
        }

    if exit_result.get("success") is not True:
        exit_error = (
            exit_result.get("error")
            or exit_result.get("message")
            or "Unknown position exit error."
        )

        logger.error(
            "Position exit reported failure. "
            "trading_symbol=%s, instrument_key=%s, error=%s",
            trading_symbol,
            instrument_key,
            exit_error,
        )

        _send_telegram_message(
            title="Exit Positions Reported Failure",
            message=(
                f"Symbol: {trading_symbol}\n"
                f"Instrument: {instrument_key}\n"
                f"Error: {exit_error}\n"
                "Continuing with order placement (test mode: exit step assumed OK)."
            ),
            level="WARNING",
            notification_context=(
                f"process_order|exit_reported_failure|instrument_key={instrument_key}"
            ),
        )

        return exit_result

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
            f"process_order|exit_success|instrument_key={instrument_key}"
        ),
    )

    return exit_result


def process_selected_instrument(
    selected_instrument: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(selected_instrument, dict) or not selected_instrument:
        logger.warning("Order workflow stopped. No instrument selected.")

        _send_telegram_message(
            title="Order Workflow Stopped",
            message="No instrument was selected.",
            level="WARNING",
            notification_context="process_order|no_instrument",
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

    dummy_orders_enabled = _is_dummy_orders_enabled()

    logger.info(
        "Order workflow started. "
        "trading_symbol=%s, instrument_key=%s, "
        "live_ltp=%s, lot_size=%s, place_order_enabled=%s, dummy_orders_enabled=%s",
        trading_symbol,
        instrument_key,
        live_ltp,
        lot_size,
        place_order_enabled,
        dummy_orders_enabled,
    )

    _send_telegram_message(
        title="Order Workflow Started",
        message=(
            f"Symbol: {trading_symbol}\n"
            f"Instrument: {instrument_key}\n"
            f"Live LTP: {live_ltp}\n"
            f"Lot Size: {lot_size}\n"
            f"Place Order: {'ENABLED' if place_order_enabled else 'DISABLED'}\n"
            f"Order Mode: {'DUMMY' if dummy_orders_enabled else 'REAL'}"
        ),
        level="STARTUP",
        notification_context=(f"process_order|started|instrument_key={instrument_key}"),
    )

    if not place_order_enabled:
        logger.info(
            "Order execution skipped. "
            "PLACE_ORDER=false, trading_symbol=%s, instrument_key=%s",
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
                f"process_order|disabled|instrument_key={instrument_key}"
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

    # live_ltp is optional for MARKET orders.
    # No LTP validation is required for MARKET order placement.

    if lot_size is None or lot_size <= 0:
        validation_errors.append("lot_size must be a positive integer")

    if validation_errors:
        validation_error = "; ".join(validation_errors)

        logger.error(
            "Order workflow stopped. Invalid instrument details. "
            "trading_symbol=%s, instrument_key=%s, "
            "live_ltp=%s, lot_size=%s, validation_error=%s",
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
                f"process_order|invalid_instrument|instrument_key={instrument_key}"
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

    exit_result = _run_exit_step(normalized_instrument)

    logger.info(
        "Step 2 started. Placing new order. trading_symbol=%s, instrument_key=%s",
        trading_symbol,
        instrument_key,
    )

    # ------------------------------------------------------------------
    # Step 2.5: Calculate margin (best-effort, does not block order).
    # ------------------------------------------------------------------
    margin_result = _calculate_margin(
        instrument_details=normalized_instrument,
        transaction_type="BUY",
    )

    return _safe_place_order(
        normalized_instrument=normalized_instrument,
        instrument_details=instrument_details,
        exit_result=exit_result,
        margin_result=margin_result,
    ) 