from typing import Any

from core.config import settings
from core.logger import get_logger

from upstox_services.place_order import (
    place_selected_instrument,
)
from upstox_services.position_service import (
    exit_all_positions,
)

logger = get_logger("process_order")


def process_selected_instrument(
    selected_instrument: dict[str, Any] | None,
) -> dict[str, Any]:

    if not selected_instrument:
        return {
            "success": False,
            "error": "No instrument selected.",
        }

    trading_symbol = selected_instrument.get("trading_symbol")

    instrument_key = selected_instrument.get("instrument_key")

    live_ltp = selected_instrument.get("live_ltp")

    lot_size = selected_instrument.get("lot_size")

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

    if not settings.PLACE_ORDER:

        logger.info("Order execution skipped. PLACE_ORDER=false")

        return {
            "success": True,
            "order_status": "IGNORED",
            "reason": "PLACE_ORDER=false",
            "selected_instrument": {
                "instrument_key": instrument_key,
                "trading_symbol": trading_symbol,
                "live_ltp": live_ltp,
                "lot_size": lot_size,
            },
            "exit_result": None,
            "place_order_result": None,
            "error": None,
        }

    logger.info("PLACE_ORDER=true")

    logger.info("Step 1 : Exit all positions")

    exit_result = exit_all_positions()

    if not exit_result.get("success"):

        logger.error(
            "Exit position failed: %s",
            exit_result.get("error"),
        )

        return {
            "success": False,
            "order_status": "EXIT_FAILED",
            "selected_instrument": {
                "instrument_key": instrument_key,
                "trading_symbol": trading_symbol,
                "live_ltp": live_ltp,
                "lot_size": lot_size,
            },
            "exit_result": exit_result,
            "place_order_result": None,
            "error": "Position exit failed.",
        }

    logger.info("Step 2 : Place order")

    place_order_result = place_selected_instrument(
        selected_instrument,
    )

    if not place_order_result.get("success"):

        logger.error(
            "Place order failed: %s",
            place_order_result.get("error"),
        )

        return {
            "success": False,
            "order_status": "PLACE_ORDER_FAILED",
            "selected_instrument": {
                "instrument_key": instrument_key,
                "trading_symbol": trading_symbol,
                "live_ltp": live_ltp,
                "lot_size": lot_size,
            },
            "exit_result": exit_result,
            "place_order_result": place_order_result,
            "error": "Place order failed.",
        }

    logger.info("Order workflow completed successfully.")

    return {
        "success": True,
        "order_status": "ORDER_PLACED",
        "selected_instrument": {
            "instrument_key": instrument_key,
            "trading_symbol": trading_symbol,
            "live_ltp": live_ltp,
            "lot_size": lot_size,
        },
        "exit_result": exit_result,
        "place_order_result": place_order_result,
        "error": None,
    }
