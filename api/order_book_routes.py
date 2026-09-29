import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from upstox_client.rest import ApiException

from core.logger import get_logger
from upstox_services.order_book_service import order_book_service
from services.daily_order_archive_service import (
    daily_order_archive_service,
)

logger = get_logger(__file__)

router = APIRouter(
    prefix="/api/orders",
    tags=["Order Book"],
)


def extract_upstox_error(
    error: ApiException,
) -> tuple[int, str | None, object]:
    status_code = (
        getattr(
            error,
            "status",
            None,
        )
        or 502
    )

    reason = getattr(
        error,
        "reason",
        None,
    )

    body = getattr(
        error,
        "body",
        None,
    )

    parsed_body = None

    if body:
        try:
            parsed_body = json.loads(body)
        except (
            TypeError,
            json.JSONDecodeError,
        ):
            parsed_body = str(body)

    if not isinstance(status_code, int) or status_code < 400 or status_code > 599:
        status_code = 502

    return (
        status_code,
        reason,
        parsed_body,
    )


def normalize_value(
    value: object,
) -> str:
    return str(value if value is not None else "").strip()


def filter_orders(
    orders: list,
    status: str | None = None,
    transaction_type: str | None = None,
    tag: str | None = None,
    instrument_token: str | None = None,
    trading_symbol: str | None = None,
) -> list:
    filtered_orders = [order for order in orders if isinstance(order, dict)]

    if status:
        expected_status = normalize_value(status).lower()

        filtered_orders = [
            order
            for order in filtered_orders
            if normalize_value(order.get("status")).lower() == expected_status
        ]

    if transaction_type:
        expected_transaction_type = normalize_value(transaction_type).upper()

        filtered_orders = [
            order
            for order in filtered_orders
            if normalize_value(order.get("transaction_type")).upper()
            == expected_transaction_type
        ]

    if tag:
        expected_tag = normalize_value(tag).lower()

        filtered_orders = [
            order
            for order in filtered_orders
            if normalize_value(order.get("tag")).lower() == expected_tag
        ]

    if instrument_token:
        expected_instrument_token = normalize_value(instrument_token)

        filtered_orders = [
            order
            for order in filtered_orders
            if normalize_value(
                order.get("instrument_token") or order.get("instrument_key")
            )
            == expected_instrument_token
        ]

    if trading_symbol:
        expected_trading_symbol = normalize_value(trading_symbol).upper()

        filtered_orders = [
            order
            for order in filtered_orders
            if normalize_value(
                order.get("trading_symbol") or order.get("tradingsymbol")
            ).upper()
            == expected_trading_symbol
        ]

    return filtered_orders


@router.get("")
@router.get("/")
async def get_all_orders(
    status: str | None = Query(
        default=None,
        description=(
            "Filter by order status, for example "
            "complete, rejected, cancelled, or open."
        ),
    ),
    transaction_type: str | None = Query(
        default=None,
        description="Filter by BUY or SELL.",
    ),
    tag: str | None = Query(
        default=None,
        description="Filter by order tag.",
    ),
    instrument_token: str | None = Query(
        default=None,
        description=("Filter by Upstox instrument token/key."),
    ),
    trading_symbol: str | None = Query(
        default=None,
        alias="trading_symbol",
        description="Filter by trading symbol.",
    ),
    tradingsymbol: str | None = Query(
        default=None,
        description=("Alternative query parameter for " "trading_symbol."),
    ),
):
    try:
        result = await run_in_threadpool(order_book_service.get_all_orders)

        if not isinstance(result, dict):
            raise RuntimeError("Order book service returned an " "invalid response.")

        orders = result.get("data") or []

        if not isinstance(orders, list):
            orders = []

        selected_trading_symbol = trading_symbol or tradingsymbol

        filtered_orders = filter_orders(
            orders=orders,
            status=status,
            transaction_type=transaction_type,
            tag=tag,
            instrument_token=instrument_token,
            trading_symbol=selected_trading_symbol,
        )

        return {
            "success": True,
            "status": result.get("status"),
            "source": "upstox_order_book",
            "total_count": len(orders),
            "count": len(filtered_orders),
            "filters": {
                "status": status,
                "transaction_type": (transaction_type),
                "tag": tag,
                "instrument_token": (instrument_token),
                "trading_symbol": (selected_trading_symbol),
            },
            "data": filtered_orders,
        }

    except ValueError as error:
        logger.warning(
            "Cannot retrieve order book: %s",
            error,
        )

        raise HTTPException(
            status_code=401,
            detail={
                "success": False,
                "error": ("ACCESS_TOKEN_NOT_AVAILABLE"),
                "message": str(error),
                "data": [],
            },
        ) from error

    except ApiException as error:
        (
            status_code,
            reason,
            error_body,
        ) = extract_upstox_error(error)

        logger.exception(
            "Upstox order book API failed. " "status=%s, reason=%s",
            status_code,
            reason,
        )

        raise HTTPException(
            status_code=status_code,
            detail={
                "success": False,
                "error": "UPSTOX_API_ERROR",
                "message": (reason or ("Unable to retrieve the " "Upstox order book.")),
                "details": error_body,
                "data": [],
            },
        ) from error

    except HTTPException:
        raise

    except Exception as error:
        logger.exception("Unexpected error while retrieving " "the Upstox order book.")

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "error": ("INTERNAL_SERVER_ERROR"),
                "message": (f"{type(error).__name__}: " f"{error}"),
                "data": [],
            },
        ) from error


@router.post("/archive")
async def archive_all_today_orders():
    """
    Fetches the complete current-day Upstox order book and saves it
    to the configured MongoDB daily order archive collection.

    Repeated calls on the same market date update the same document.
    """

    logger.info("Manual daily order book archive requested.")

    try:
        result = await run_in_threadpool(
            daily_order_archive_service.archive_today_orders,
            source="manual_api",
        )

        if not isinstance(result, dict):
            raise RuntimeError(
                "Daily order archive service returned an " "invalid response."
            )

        if result.get("success") is not True:
            status = str(result.get("status") or "failed").strip().lower()

            if status == "disabled":
                raise HTTPException(
                    status_code=503,
                    detail={
                        "success": False,
                        "error": "ORDER_ARCHIVE_DISABLED",
                        "message": result.get(
                            "message",
                            "Daily order archive service is disabled.",
                        ),
                        "archive_result": result,
                    },
                )

            raise HTTPException(
                status_code=500,
                detail={
                    "success": False,
                    "error": "ORDER_ARCHIVE_FAILED",
                    "message": result.get(
                        "message",
                        "Daily order archive failed.",
                    ),
                    "archive_result": result,
                },
            )

        logger.info(
            "Manual daily order book archive completed. "
            "market_date=%s, archive_status=%s, "
            "total_orders=%s, collection=%s",
            result.get("market_date"),
            result.get("status"),
            result.get("total_orders_count"),
            result.get("collection_name"),
        )

        return {
            "success": True,
            "message": ("Today's complete Upstox order book was " "saved to MongoDB."),
            "source": "manual_api",
            "archive_status": result.get("status"),
            "market_date": result.get("market_date"),
            "total_orders_count": result.get(
                "total_orders_count",
                0,
            ),
            "database_name": result.get("database_name"),
            "collection_name": result.get("collection_name"),
            "matched_count": result.get(
                "matched_count",
                0,
            ),
            "modified_count": result.get(
                "modified_count",
                0,
            ),
            "upserted_id": result.get("upserted_id"),
            "fetched_at": result.get("fetched_at"),
            "archive_result": result,
        }

    except HTTPException:
        raise

    except ValueError as error:
        logger.warning(
            "Manual daily order archive could not run: %s",
            error,
        )

        raise HTTPException(
            status_code=401,
            detail={
                "success": False,
                "error": "ACCESS_TOKEN_NOT_AVAILABLE",
                "message": str(error),
            },
        ) from error

    except ApiException as error:
        (
            status_code,
            reason,
            error_body,
        ) = extract_upstox_error(error)

        logger.exception(
            "Upstox order book API failed during manual "
            "archive. status=%s, reason=%s",
            status_code,
            reason,
        )

        raise HTTPException(
            status_code=status_code,
            detail={
                "success": False,
                "error": "UPSTOX_API_ERROR",
                "message": (
                    reason or ("Unable to retrieve today's " "Upstox order book.")
                ),
                "details": error_body,
            },
        ) from error

    except Exception as error:
        logger.exception("Manual daily order book archive failed.")

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "error": "ORDER_ARCHIVE_EXCEPTION",
                "message": (f"{type(error).__name__}: {error}"),
            },
        ) from error
