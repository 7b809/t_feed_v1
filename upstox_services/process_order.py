from typing import Any

from core.config import settings
from core.logger import get_logger

from services.telegram_bot_service import (
    telegram_bot_service,
)

from upstox_services.place_order import (
    place_selected_instrument,
)
from upstox_services.position_service import (
    exit_all_positions,
)


logger = get_logger("process_order")


# ------------------------------------------------------------------
# Telegram helper
# ------------------------------------------------------------------

def _send_telegram_message(
    message: str,
) -> None:
    """
    Send a Telegram message using the existing Telegram bot service.

    Telegram failures must never stop the actual order workflow.
    """

    if not settings.tele_flg:
        logger.debug(
            "Telegram message skipped because TELE_FLG=false."
        )
        return

    try:
        telegram_bot_service._send_direct_message(
            message
        )

    except Exception:
        logger.exception(
            "Unexpected error while sending Telegram "
            "process-order message."
        )


# ------------------------------------------------------------------
# Selected instrument helper
# ------------------------------------------------------------------

def _build_instrument_details(
    selected_instrument: dict[str, Any],
) -> dict[str, Any]:
    return {
        "instrument_key": selected_instrument.get(
            "instrument_key"
        ),
        "trading_symbol": selected_instrument.get(
            "trading_symbol"
        ),
        "live_ltp": selected_instrument.get(
            "live_ltp"
        ),
        "lot_size": selected_instrument.get(
            "lot_size"
        ),
    }


# ------------------------------------------------------------------
# Main order workflow
# ------------------------------------------------------------------

def process_selected_instrument(
    selected_instrument: dict[str, Any] | None,
) -> dict[str, Any]:

    # --------------------------------------------------------------
    # No instrument
    # --------------------------------------------------------------

    if not selected_instrument:
        logger.warning(
            "Order workflow stopped. No instrument selected."
        )

        _send_telegram_message(
            "⚠️ <b>Order Workflow Stopped</b>\n\n"
            "❌ No instrument was selected."
        )

        return {
            "success": False,
            "error": "No instrument selected.",
        }

    # --------------------------------------------------------------
    # Instrument details
    # --------------------------------------------------------------

    trading_symbol = selected_instrument.get(
        "trading_symbol"
    )

    instrument_key = selected_instrument.get(
        "instrument_key"
    )

    live_ltp = selected_instrument.get(
        "live_ltp"
    )

    lot_size = selected_instrument.get(
        "lot_size"
    )

    instrument_details = (
        _build_instrument_details(
            selected_instrument
        )
    )

    # --------------------------------------------------------------
    # Log selected instrument
    # --------------------------------------------------------------

    logger.info(
        "Selected Instrument=%s",
        trading_symbol,
    )

    logger.info(
        "Instrument Key=%s",
        instrument_key,
    )

    logger.info(
        "Live LTP=%s",
        live_ltp,
    )

    logger.info(
        "Lot Size=%s",
        lot_size,
    )

    # --------------------------------------------------------------
    # Telegram: workflow started
    # --------------------------------------------------------------

    _send_telegram_message(
        "🚀 <b>Order Workflow Started</b>\n\n"
        f"📌 Symbol: <b>{trading_symbol}</b>\n"
        f"🔑 Instrument: <code>{instrument_key}</code>\n"
        f"💰 Live LTP: <b>{live_ltp}</b>\n"
        f"📦 Lot Size: <b>{lot_size}</b>\n\n"
        f"🛒 Place Order: "
        f"<b>{'ENABLED' if settings.PLACE_ORDER else 'DISABLED'}</b>"
    )

    # --------------------------------------------------------------
    # PLACE_ORDER disabled
    # --------------------------------------------------------------

    if not settings.PLACE_ORDER:

        logger.info(
            "Order execution skipped. PLACE_ORDER=false"
        )

        _send_telegram_message(
            "ℹ️ <b>Order Execution Skipped</b>\n\n"
            f"📌 Symbol: <b>{trading_symbol}</b>\n"
            f"💰 Live LTP: <b>{live_ltp}</b>\n"
            f"📦 Lot Size: <b>{lot_size}</b>\n\n"
            "⚙️ PLACE_ORDER=<b>false</b>"
        )

        return {
            "success": True,
            "order_status": "IGNORED",
            "reason": "PLACE_ORDER=false",
            "selected_instrument": instrument_details,
            "exit_result": None,
            "place_order_result": None,
            "error": None,
        }

    # --------------------------------------------------------------
    # PLACE_ORDER enabled
    # --------------------------------------------------------------

    logger.info(
        "PLACE_ORDER=true"
    )

    # --------------------------------------------------------------
    # Step 1: Exit existing positions
    # --------------------------------------------------------------

    logger.info(
        "Step 1 : Exit all positions"
    )

    _send_telegram_message(
        "🔄 <b>Step 1: Exiting Existing Positions</b>\n\n"
        f"📌 New Symbol: <b>{trading_symbol}</b>\n"
        f"💰 Target LTP: <b>{live_ltp}</b>\n\n"
        "⏳ Checking and exiting all existing positions..."
    )

    try:
        exit_result = exit_all_positions()

    except Exception as exc:
        logger.exception(
            "Exception while exiting positions: %s",
            exc,
        )

        _send_telegram_message(
            "❌ <b>Exit Positions Exception</b>\n\n"
            f"📌 Symbol: <b>{trading_symbol}</b>\n"
            f"⚠️ Error: <code>{exc}</code>\n\n"
            "🚫 New order will NOT be placed."
        )

        return {
            "success": False,
            "order_status": "EXIT_FAILED",
            "selected_instrument": instrument_details,
            "exit_result": None,
            "place_order_result": None,
            "error": "Position exit failed.",
        }

    # --------------------------------------------------------------
    # Exit failed
    # --------------------------------------------------------------

    if not exit_result.get("success"):

        exit_error = exit_result.get(
            "error"
        )

        logger.error(
            "Exit position failed: %s",
            exit_error,
        )

        _send_telegram_message(
            "❌ <b>Exit Positions Failed</b>\n\n"
            f"📌 Symbol: <b>{trading_symbol}</b>\n"
            f"⚠️ Error: <code>{exit_error}</code>\n\n"
            "🚫 <b>New order NOT placed.</b>"
        )

        return {
            "success": False,
            "order_status": "EXIT_FAILED",
            "selected_instrument": instrument_details,
            "exit_result": exit_result,
            "place_order_result": None,
            "error": "Position exit failed.",
        }

    # --------------------------------------------------------------
    # Exit successful
    # --------------------------------------------------------------

    logger.info(
        "All existing positions exited successfully."
    )

    _send_telegram_message(
        "✅ <b>Step 1 Completed: Positions Exited</b>\n\n"
        f"📌 Next Symbol: <b>{trading_symbol}</b>\n"
        f"💰 Live LTP: <b>{live_ltp}</b>\n\n"
        "➡️ Proceeding to place new order..."
    )

    # --------------------------------------------------------------
    # Step 2: Place order
    # --------------------------------------------------------------

    logger.info(
        "Step 2 : Place order"
    )

    _send_telegram_message(
        "📤 <b>Step 2: Placing Order</b>\n\n"
        f"📌 Symbol: <b>{trading_symbol}</b>\n"
        f"🔑 Instrument: <code>{instrument_key}</code>\n"
        f"💰 Live LTP: <b>{live_ltp}</b>\n"
        f"📦 Lot Size: <b>{lot_size}</b>\n\n"
        "⏳ Sending order to Upstox..."
    )

    # --------------------------------------------------------------
    # Place order
    # --------------------------------------------------------------

    try:
        place_order_result = (
            place_selected_instrument(
                selected_instrument,
            )
        )

    except Exception as exc:
        logger.exception(
            "Exception while placing order: %s",
            exc,
        )

        _send_telegram_message(
            "❌ <b>Order Placement Exception</b>\n\n"
            f"📌 Symbol: <b>{trading_symbol}</b>\n"
            f"⚠️ Error: <code>{exc}</code>\n\n"
            "🚨 Order placement encountered an exception."
        )

        return {
            "success": False,
            "order_status": "PLACE_ORDER_FAILED",
            "selected_instrument": instrument_details,
            "exit_result": exit_result,
            "place_order_result": None,
            "error": "Place order failed.",
        }

    # --------------------------------------------------------------
    # Order placement failed
    # --------------------------------------------------------------

    if not place_order_result.get("success"):

        order_error = place_order_result.get(
            "error"
        )

        logger.error(
            "Place order failed: %s",
            order_error,
        )

        _send_telegram_message(
            "❌ <b>Order Placement Failed</b>\n\n"
            f"📌 Symbol: <b>{trading_symbol}</b>\n"
            f"🔑 Instrument: <code>{instrument_key}</code>\n"
            f"💰 Live LTP: <b>{live_ltp}</b>\n"
            f"📦 Lot Size: <b>{lot_size}</b>\n\n"
            f"⚠️ Error: <code>{order_error}</code>"
        )

        return {
            "success": False,
            "order_status": "PLACE_ORDER_FAILED",
            "selected_instrument": instrument_details,
            "exit_result": exit_result,
            "place_order_result": place_order_result,
            "error": "Place order failed.",
        }

    # --------------------------------------------------------------
    # Order placement successful
    # --------------------------------------------------------------

    logger.info(
        "Order workflow completed successfully."
    )

    _send_telegram_message(
        "🎯 <b>ORDER PLACED SUCCESSFULLY</b>\n\n"
        f"📌 Symbol: <b>{trading_symbol}</b>\n"
        f"🔑 Instrument: <code>{instrument_key}</code>\n"
        f"💰 Live LTP: <b>{live_ltp}</b>\n"
        f"📦 Lot Size: <b>{lot_size}</b>\n\n"
        "✅ Existing positions exited\n"
        "✅ New order placed\n"
        "🏁 <b>Order workflow completed</b>"
    )

    # --------------------------------------------------------------
    # Final result
    # --------------------------------------------------------------

    return {
        "success": True,
        "order_status": "ORDER_PLACED",
        "selected_instrument": instrument_details,
        "exit_result": exit_result,
        "place_order_result": place_order_result,
        "error": None,
    }
