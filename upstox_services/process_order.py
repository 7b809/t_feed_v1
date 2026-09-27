from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any
from uuid import uuid4

import upstox_client
from upstox_client.rest import ApiException

from core import config
from core.logger import get_logger
from services.telegram_service import telegram_service
from upstox_services.margin_service import margin_service
from upstox_services.place_order import place_selected_instrument
from upstox_services.position_service import exit_all_positions

# ---------------------------------------------------------------------------
# Reuse the existing order-saving service infrastructure for MongoDB.
# This avoids needing a separate `core.db.get_database()` helper.
# ---------------------------------------------------------------------------
try:
    from services.order_saving_service import (  # type: ignore
        upstox_order_saving_service,
    )
except Exception:  # pragma: no cover - fallback so file still imports
    upstox_order_saving_service = None  # type: ignore

logger = get_logger(__file__)


# Name of the MongoDB collection used for "sell-exit" pair tracking.
SELL_EXIT_COLLECTION_NAME = "sell_exit_orders"

# Tag used by place_selected_instrument -> place_market_order. Keep in sync.
DEFAULT_ORDER_TAG = "EMA_ALGO"

# Upstox API version for market quote calls.
UPSTOX_API_VERSION = "2.0"

# Collection used to record every processed instrument together with the
# live LTP fetched from Upstox at processing time.
ORDER_HISTORY_COLLECTION_NAME = "order_history"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def _is_dummy_orders_enabled() -> bool:
    """
    Returns True when dummy order mode is enabled.
    Supports both lowercase and uppercase configuration names.
    """
    return bool(
        getattr(config, "dummy_orders", False) or getattr(config, "DUMMY_ORDERS", False)
    )


def _is_sell_exit_enabled() -> bool:
    """
    Returns True when SELL_EXIT mode is enabled.

    SELL_EXIT mode means:
        - First BUY order  -> stored locally (instrument_key, lot_size, etc.)
        - Next order       -> SELL the stored instrument, then place new BUY,
                              and store the new BUY instrument.
    When False, the legacy behaviour (exit_all_positions) is used.
    """
    return bool(
        getattr(config, "SELL_EXIT", False) or getattr(config, "sell_exit", False)
    )


def _get_upstox_access_token() -> str | None:
    """
    Resolves the Upstox access token from the available configuration.

    Tries, in order:
        1) config.UPSTOX_ACCESS_TOKEN (string)
        2) config.upstox_access_token (string)
        3) config.get_upstox_access_token() (callable)
        4) config.access_token (string)

    Adjust this function if your token lives somewhere else.
    """
    candidates: list[Any] = [
        getattr(config, "UPSTOX_ACCESS_TOKEN", None),
        getattr(config, "upstox_access_token", None),
        getattr(config, "access_token", None),
    ]

    for candidate in candidates:
        if callable(candidate):
            try:
                value = candidate()
            except Exception:
                logger.exception("Callable access-token source raised an exception.")
                continue
            if value:
                return str(value).strip()
        elif candidate:
            return str(candidate).strip()

    # Last resort: try a well-known callable name.
    getter = getattr(config, "get_upstox_access_token", None)
    if callable(getter):
        try:
            value = getter()
            if value:
                return str(value).strip()
        except Exception:
            logger.exception("config.get_upstox_access_token() raised an exception.")

    return None


# ---------------------------------------------------------------------------
# MongoDB access (reuses order_saving_service's connection settings)
# ---------------------------------------------------------------------------
def _get_mongo_database():
    """
    Returns the MongoDB database handle used by the order-saving service.

    This reuses the same MONGO_URI / MONGO_DB configuration values that
    `order_saving_service` already uses, so we don't need a separate
    `core.db.get_database()` helper.

    Strategy:
        1) If `upstox_order_saving_service` is importable, use its internal
           `_get_collection()` bootstrap once to establish a client, then
           grab its database handle.
        2) Otherwise, fall back to reading config.MONGO_URI / config.MONGO_DB
           directly and opening a short-lived MongoClient.
    """
    # ---- Path 1: reuse the order-saving service's already-open client ----
    if upstox_order_saving_service is not None:
        try:
            # Calling _get_collection() ensures the client/database are
            # initialized (and respects UPSTOX_ORDER_ENABLED).
            collection = upstox_order_saving_service._get_collection()  # type: ignore[attr-defined]

            if collection is not None:
                # pymongo Collection exposes .database
                db = getattr(collection, "database", None)
                if db is not None:
                    return db
        except Exception:
            logger.exception(
                "Failed to obtain MongoDB database via "
                "order_saving_service. Falling back to direct config."
            )

    # ---- Path 2: open our own client from config ----
    mongo_uri = str(
        getattr(config, "MONGO_URI", None) or getattr(config, "MONGO_URL", "") or ""
    ).strip()

    database_name = str(getattr(config, "MONGO_DB", "") or "").strip()

    if not mongo_uri or not database_name:
        raise RuntimeError(
            "MongoDB is not configured. Please set MONGO_URI/MONGO_URL "
            "and MONGO_DB in config."
        )

    from pymongo import MongoClient  # local import to avoid hard dependency

    client = MongoClient(
        mongo_uri,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        socketTimeoutMS=10000,
    )
    client.admin.command("ping")
    return client[database_name]


# ---------------------------------------------------------------------------
# Telegram helper
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Safe conversion helpers
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Live LTP fetch via Upstox MarketQuoteApi
# ---------------------------------------------------------------------------
def _fetch_live_ltp(instrument_key: str) -> float | None:
    """
    Fetches the live LTP for the given Upstox instrument_key using the
    MarketQuoteApi.ltp() endpoint.

    Accepts instrument_key in any of these forms:
        - "NSE_FO|73923"          (pipe form, as used by Upstox APIs)
        - "NSE_FO:NIFTY26SEP23300CE" (colon form, as returned by ltp())

    The Upstox ltp() endpoint expects the pipe form ("NSE_FO|73923").
    If a colon-form key is passed, we return None and log the issue.

    Returns:
        float | None: the last_price if successful, otherwise None.
    """
    if not instrument_key:
        logger.warning("Live LTP fetch skipped: empty instrument_key.")
        return None

    if ":" in instrument_key and "|" not in instrument_key:
        logger.error(
            "Live LTP fetch skipped: instrument_key uses colon form (%s). "
            "The Upstox ltp() API requires the pipe form (e.g. 'NSE_FO|73923').",
            instrument_key,
        )
        return None

    access_token = _get_upstox_access_token()

    if not access_token:
        logger.error(
            "Live LTP fetch skipped: no Upstox access token available " "in config."
        )
        return None

    try:
        configuration = upstox_client.Configuration()
        configuration.access_token = access_token

        api_instance = upstox_client.MarketQuoteApi(
            upstox_client.ApiClient(configuration)
        )

        api_response = api_instance.ltp(instrument_key, UPSTOX_API_VERSION)

        last_price = _extract_last_price_from_ltp_response(api_response, instrument_key)

        if last_price is None:
            logger.error(
                "Live LTP fetch: could not extract last_price. "
                "instrument_key=%s, response=%r",
                instrument_key,
                api_response,
            )
            return None

        logger.info(
            "Live LTP fetched successfully. instrument_key=%s, last_price=%s",
            instrument_key,
            last_price,
        )
        return last_price

    except ApiException as exc:
        logger.exception(
            "Upstox ApiException while fetching live LTP. "
            "instrument_key=%s, error=%s",
            instrument_key,
            exc,
        )
        return None

    except Exception as exc:
        logger.exception(
            "Unexpected exception while fetching live LTP. "
            "instrument_key=%s, exception_type=%s, error=%s",
            instrument_key,
            type(exc).__name__,
            exc,
        )
        return None


def _extract_last_price_from_ltp_response(
    api_response: Any,
    instrument_key: str,
) -> float | None:
    """
    Extracts the last_price from the Upstox ltp() response.
    """
    if api_response is None:
        return None

    response_dict: dict[str, Any] | None = None

    if isinstance(api_response, dict):
        response_dict = api_response
    else:
        to_dict = getattr(api_response, "to_dict", None)
        if callable(to_dict):
            try:
                response_dict = to_dict()
            except Exception:
                logger.exception(
                    "Failed to call to_dict() on ltp response. instrument_key=%s",
                    instrument_key,
                )

    if not isinstance(response_dict, dict):
        logger.error(
            "ltp response is not a dict and has no usable to_dict(). "
            "instrument_key=%s, response_type=%s",
            instrument_key,
            type(api_response).__name__,
        )
        return None

    data = response_dict.get("data")

    if not isinstance(data, dict):
        logger.error(
            "ltp response missing 'data' dict. instrument_key=%s, response=%r",
            instrument_key,
            response_dict,
        )
        return None

    if not data:
        logger.error("ltp response 'data' is empty. instrument_key=%s", instrument_key)
        return None

    direct = data.get(instrument_key)
    if isinstance(direct, dict):
        last_price = _safe_float(direct.get("last_price"))
        if last_price is not None:
            return last_price

    for _key, entry in data.items():
        if not isinstance(entry, dict):
            continue

        entry_token = entry.get("instrument_token")
        if entry_token and str(entry_token) == str(instrument_key):
            last_price = _safe_float(entry.get("last_price"))
            if last_price is not None:
                return last_price

        if len(data) == 1:
            last_price = _safe_float(entry.get("last_price"))
            if last_price is not None:
                return last_price

    for _key, entry in data.items():
        if isinstance(entry, dict):
            last_price = _safe_float(entry.get("last_price"))
            if last_price is not None:
                return last_price

    return None


# ---------------------------------------------------------------------------
# Instrument details builder (fetches live LTP)
# ---------------------------------------------------------------------------
def _build_instrument_details(
    selected_instrument: dict[str, Any],
) -> dict[str, Any]:
    """
    Builds the normalized instrument details dict.

    The live LTP is fetched from Upstox via MarketQuoteApi.ltp() using
    the instrument_key. If the fetch fails, we fall back to any LTP
    already present in the selected_instrument dict.
    """
    instrument_key = (
        str(selected_instrument.get("instrument_key") or "").strip() or None
    )
    trading_symbol = (
        str(selected_instrument.get("trading_symbol") or "").strip() or None
    )
    lot_size = _safe_int(selected_instrument.get("lot_size"))

    live_ltp: float | None = None

    if instrument_key:
        live_ltp = _fetch_live_ltp(instrument_key)

    if live_ltp is None:
        fallback_ltp = _safe_float(selected_instrument.get("live_ltp"))
        if fallback_ltp is None:
            fallback_ltp = _safe_float(selected_instrument.get("ltp"))

        if fallback_ltp is not None:
            logger.warning(
                "Using fallback LTP from selected_instrument. "
                "instrument_key=%s, fallback_ltp=%s",
                instrument_key,
                fallback_ltp,
            )
        live_ltp = fallback_ltp

    return {
        "instrument_key": instrument_key,
        "trading_symbol": trading_symbol,
        "live_ltp": live_ltp,
        "lot_size": lot_size,
    }


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Order id extraction
# ---------------------------------------------------------------------------
def _extract_order_id(
    place_order_result: dict[str, Any] | None,
) -> str | None:
    """
    Extracts the Upstox order id from a place_order_result dictionary.
    """
    if not isinstance(place_order_result, dict):
        return None

    direct_order_id = (
        place_order_result.get("order_id")
        or place_order_result.get("orderId")
        or place_order_result.get("id")
        or place_order_result.get("_order_id")
    )

    if direct_order_id is not None:
        normalized_order_id = str(direct_order_id).strip()

        if normalized_order_id:
            return normalized_order_id

    data = place_order_result.get("data") or place_order_result.get("_data")

    if isinstance(data, dict):
        data_order_id = (
            data.get("order_id")
            or data.get("orderId")
            or data.get("id")
            or data.get("_order_id")
        )

        if data_order_id is not None:
            normalized_order_id = str(data_order_id).strip()

            if normalized_order_id:
                return normalized_order_id

    response = place_order_result.get("response")

    if isinstance(response, dict):
        response_order_id = (
            response.get("order_id")
            or response.get("orderId")
            or response.get("id")
            or response.get("_order_id")
        )

        if response_order_id is not None:
            normalized_order_id = str(response_order_id).strip()

            if normalized_order_id:
                return normalized_order_id

        response_data = response.get("data") or response.get("_data")

        if isinstance(response_data, dict):
            response_data_order_id = (
                response_data.get("order_id")
                or response_data.get("orderId")
                or response_data.get("id")
                or response_data.get("_order_id")
            )

            if response_data_order_id is not None:
                normalized_order_id = str(response_data_order_id).strip()

                if normalized_order_id:
                    return normalized_order_id

    return None


# ---------------------------------------------------------------------------
# MongoDB helpers for SELL_EXIT pair tracking
# ---------------------------------------------------------------------------
def _get_sell_exit_collection():
    """
    Returns the MongoDB collection used to track the active BUY instrument
    and the SELL exit results.

    Reuses the same MongoDB database handle as the order-saving service.
    """
    db = _get_mongo_database()
    return db[SELL_EXIT_COLLECTION_NAME]


def _pair_document_filter() -> dict[str, Any]:
    """
    Filter identifying the single "active pair" document for this strategy.
    """
    return {"strategy": DEFAULT_ORDER_TAG, "active": True}


def _load_active_buy_order() -> dict[str, Any] | None:
    """
    Returns the currently stored BUY instrument (or None if none stored).
    """
    try:
        collection = _get_sell_exit_collection()
        document = collection.find_one(_pair_document_filter())
        return document
    except Exception:
        logger.exception("Failed to load active BUY order from MongoDB.")
        return None


def _save_active_buy_order(
    instrument_details: dict[str, Any],
    *,
    order_id: str | None,
    place_order_result: Any = None,
) -> None:
    """
    Upserts the active BUY instrument for SELL_EXIT mode.

    Stores the fetched live LTP alongside the instrument so the SELL step
    can compute entry-vs-exit LTP analysis.
    """
    now_iso = datetime.now().isoformat()

    document = {
        "strategy": DEFAULT_ORDER_TAG,
        "active": True,
        "type": "BUY_ENTRY",
        "instrument_key": instrument_details.get("instrument_key"),
        "trading_symbol": instrument_details.get("trading_symbol"),
        "lot_size": instrument_details.get("lot_size"),
        # BUY-side live LTP fetched at order-processing time.
        "buy_live_ltp": instrument_details.get("live_ltp"),
        # Legacy generic field for backward-compat consumers.
        "live_ltp": instrument_details.get("live_ltp"),
        "buy_order_id": order_id,
        "buy_placed_at": now_iso,
        "buy_place_order_result": place_order_result,
        "updated_at": now_iso,
    }

    try:
        collection = _get_sell_exit_collection()
        collection.update_one(
            _pair_document_filter(),
            {
                "$set": document,
                "$setOnInsert": {"created_at": now_iso},
            },
            upsert=True,
        )
        logger.info(
            "Active BUY order saved. instrument_key=%s, order_id=%s, "
            "buy_live_ltp=%s",
            instrument_details.get("instrument_key"),
            order_id,
            instrument_details.get("live_ltp"),
        )
    except Exception:
        logger.exception(
            "Failed to save active BUY order to MongoDB. instrument_key=%s",
            instrument_details.get("instrument_key"),
        )


def _clear_active_buy_order(reason: str = "sold") -> None:
    """
    Removes the active BUY marker once the instrument has been sold.
    """
    try:
        collection = _get_sell_exit_collection()
        collection.update_one(
            _pair_document_filter(),
            {
                "$set": {
                    "active": False,
                    "closed_reason": reason,
                    "closed_at": datetime.now().isoformat(),
                }
            },
        )
        logger.info("Active BUY order cleared. reason=%s", reason)
    except Exception:
        logger.exception("Failed to clear active BUY order from MongoDB.")


def _save_sell_exit_result(
    sell_instrument: dict[str, Any],
    sell_result: dict[str, Any] | None,
    *,
    error: str | None = None,
    buy_order_id: str | None = None,
) -> None:
    """
    Persists the SELL exit alongside the BUY entry.

    Strategy:
        1) Try to update the existing pair document (the one created when
           the BUY was placed). This keeps BUY-side LTP and SELL-side LTP
           in the SAME document.
        2) If no matching BUY document exists, insert a standalone
           SELL_EXIT document so we never lose the SELL record.

    Fields written on the SELL side:
        - sell_live_ltp:  LTP fetched at SELL time
        - buy_live_ltp:   LTP fetched at BUY time (preserved)
        - ltp_change / ltp_change_pct: computed for analysis
    """
    now_iso = datetime.now().isoformat()

    sell_order_id = _extract_order_id(sell_result) if sell_result else None
    sell_live_ltp = _safe_float(sell_instrument.get("live_ltp"))

    sell_fields = {
        "sell_order_id": sell_order_id,
        "sell_live_ltp": sell_live_ltp,
        "sell_placed_at": now_iso,
        "sell_result": sell_result,
        "sell_error": error,
        "sell_instrument_key": sell_instrument.get("instrument_key"),
        "sell_trading_symbol": sell_instrument.get("trading_symbol"),
        "sell_lot_size": sell_instrument.get("lot_size"),
        "updated_at": now_iso,
    }

    # ------------------------------------------------------------------
    # Attempt to update the existing pair document (same doc as BUY).
    # ------------------------------------------------------------------
    updated_existing = False

    try:
        collection = _get_sell_exit_collection()

        pair_filter: dict[str, Any] | None = None

        if buy_order_id:
            pair_filter = {
                "strategy": DEFAULT_ORDER_TAG,
                "buy_order_id": buy_order_id,
            }

        if pair_filter is not None:
            existing = collection.find_one(pair_filter)

            if existing:
                buy_ltp = _safe_float(existing.get("buy_live_ltp"))
                if buy_ltp is None:
                    buy_ltp = _safe_float(existing.get("live_ltp"))

                ltp_change = None
                ltp_change_pct = None

                if buy_ltp is not None and sell_live_ltp is not None:
                    ltp_change = sell_live_ltp - buy_ltp
                    if buy_ltp != 0:
                        ltp_change_pct = (ltp_change / buy_ltp) * 100.0

                update_doc = {
                    "$set": {
                        **sell_fields,
                        "ltp_change": ltp_change,
                        "ltp_change_pct": ltp_change_pct,
                        "type": "TRADE_PAIR",
                    },
                    "$setOnInsert": {
                        "strategy": DEFAULT_ORDER_TAG,
                        "instrument_key": sell_instrument.get("instrument_key"),
                        "trading_symbol": sell_instrument.get("trading_symbol"),
                        "lot_size": sell_instrument.get("lot_size"),
                        "created_at": now_iso,
                    },
                }

                collection.update_one(pair_filter, update_doc)
                updated_existing = True

                logger.info(
                    "SELL exit merged into existing BUY pair document. "
                    "instrument_key=%s, buy_order_id=%s, sell_order_id=%s, "
                    "buy_live_ltp=%s, sell_live_ltp=%s, ltp_change=%s",
                    sell_instrument.get("instrument_key"),
                    buy_order_id,
                    sell_order_id,
                    buy_ltp,
                    sell_live_ltp,
                    ltp_change,
                )

    except Exception:
        logger.exception(
            "Failed to update existing BUY pair document with SELL result. "
            "instrument_key=%s",
            sell_instrument.get("instrument_key"),
        )

    # ------------------------------------------------------------------
    # Fallback: no existing pair document -> insert a standalone SELL doc.
    # ------------------------------------------------------------------
    if updated_existing:
        return

    document = {
        "strategy": DEFAULT_ORDER_TAG,
        "type": "SELL_EXIT",
        "instrument_key": sell_instrument.get("instrument_key"),
        "trading_symbol": sell_instrument.get("trading_symbol"),
        "lot_size": sell_instrument.get("lot_size"),
        "sell_live_ltp": sell_live_ltp,
        "live_ltp": sell_live_ltp,
        "buy_order_id": buy_order_id,
        **sell_fields,
    }

    try:
        collection = _get_sell_exit_collection()
        collection.insert_one(document)
        logger.info(
            "Standalone SELL exit result saved (no matching BUY pair doc). "
            "instrument_key=%s, sell_order_id=%s, sell_live_ltp=%s",
            sell_instrument.get("instrument_key"),
            sell_order_id,
            sell_live_ltp,
        )
    except Exception:
        logger.exception(
            "Failed to save SELL exit result to MongoDB. instrument_key=%s",
            sell_instrument.get("instrument_key"),
        )


# ---------------------------------------------------------------------------
# Order-history persistence with fetched LTP
# ---------------------------------------------------------------------------
def _get_order_history_collection():
    """
    Returns the MongoDB collection used to record order-history entries.
    Reuses the same MongoDB database handle as the order-saving service.
    """
    db = _get_mongo_database()
    return db[ORDER_HISTORY_COLLECTION_NAME]


def _save_order_history_entry(
    instrument_details: dict[str, Any],
    *,
    order_status: str,
    success: bool,
    executed: bool,
    skipped: bool,
    order_id: str | None,
    exit_result: Any = None,
    margin_result: Any = None,
    place_order_result: Any = None,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """
    Persists a single order-workflow history entry.

    The entry stores the live LTP fetched from Upstox at processing time.
    """
    now_iso = datetime.now().isoformat()

    document = {
        "strategy": DEFAULT_ORDER_TAG,
        "type": "ORDER_HISTORY",
        "instrument_key": instrument_details.get("instrument_key"),
        "trading_symbol": instrument_details.get("trading_symbol"),
        "lot_size": instrument_details.get("lot_size"),
        "live_ltp": instrument_details.get("live_ltp"),
        "order_status": order_status,
        "success": bool(success),
        "executed": bool(executed),
        "skipped": bool(skipped),
        "order_id": order_id,
        "exit_result": exit_result,
        "margin_result": margin_result,
        "place_order_result": place_order_result,
        "error": error,
        "processed_at": now_iso,
        "updated_at": now_iso,
    }

    if extra:
        document.update(extra)

    try:
        collection = _get_order_history_collection()
        collection.insert_one(document)
        logger.info(
            "Order history entry saved. instrument_key=%s, order_status=%s, "
            "live_ltp=%s, order_id=%s",
            instrument_details.get("instrument_key"),
            order_status,
            instrument_details.get("live_ltp"),
            order_id,
        )
    except Exception:
        logger.exception(
            "Failed to save order history entry. instrument_key=%s",
            instrument_details.get("instrument_key"),
        )


# ---------------------------------------------------------------------------
# Margin calculation
# ---------------------------------------------------------------------------
def _calculate_margin(
    instrument_details: dict[str, Any],
    *,
    transaction_type: str = "BUY",
) -> dict[str, Any]:
    """
    Calculates margin for the given instrument using the margin service.

    This is a best-effort step: any failure is logged and notified via
    Telegram, but it does NOT block the order workflow.
    """
    instrument_key = instrument_details.get("instrument_key")
    lot_size = instrument_details.get("lot_size")

    quantity = (
        lot_size if lot_size and lot_size > 0 else margin_service.DEFAULT_QUANTITY
    )

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
            "Margin calculation failed. instrument_key=%s, error=%s",
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


# ---------------------------------------------------------------------------
# Dummy order builder
# ---------------------------------------------------------------------------
def _build_dummy_place_order_result(
    normalized_instrument: dict[str, Any],
    instrument_details: dict[str, Any],
) -> dict[str, Any]:
    """
    Builds a simulated (dummy) order placement response.
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


# ---------------------------------------------------------------------------
# Safe place order (BUY-side)
# ---------------------------------------------------------------------------
def _safe_place_order(
    normalized_instrument: dict[str, Any],
    instrument_details: dict[str, Any],
    exit_result: Any,
    margin_result: Any = None,
    *,
    persist_as_active_buy: bool = False,
) -> dict[str, Any]:
    """
    Wraps place_selected_instrument so that exceptions are logged,
    notified via Telegram, and converted to a structured failure result.
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
    # DUMMY ORDER MODE
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

        if persist_as_active_buy:
            _save_active_buy_order(
                instrument_details=instrument_details,
                order_id=order_id,
                place_order_result=place_order_result,
            )

        _save_order_history_entry(
            instrument_details=instrument_details,
            order_status="DUMMY_ORDER_PLACED",
            success=True,
            executed=True,
            skipped=False,
            order_id=order_id,
            exit_result=exit_result,
            margin_result=margin_result,
            place_order_result=place_order_result,
            error=None,
            extra={"dummy": True},
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
    # REAL ORDER MODE
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

        error_msg = f"Order placement raised an exception: {type(exc).__name__}: {exc}"

        _save_order_history_entry(
            instrument_details=instrument_details,
            order_status="PLACE_ORDER_FAILED",
            success=False,
            executed=True,
            skipped=False,
            order_id=None,
            exit_result=exit_result,
            margin_result=margin_result,
            place_order_result=None,
            error=error_msg,
        )

        return _build_failure_result(
            order_status="PLACE_ORDER_FAILED",
            instrument_details=normalized_instrument,
            exit_result=exit_result,
            margin_result=margin_result,
            error=error_msg,
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

        _save_order_history_entry(
            instrument_details=instrument_details,
            order_status="PLACE_ORDER_FAILED",
            success=False,
            executed=True,
            skipped=False,
            order_id=None,
            exit_result=exit_result,
            margin_result=margin_result,
            place_order_result=place_order_result,
            error="Invalid order placement response.",
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
            "Order placement failed. trading_symbol=%s, instrument_key=%s, error=%s",
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

        _save_order_history_entry(
            instrument_details=instrument_details,
            order_status="PLACE_ORDER_FAILED",
            success=False,
            executed=True,
            skipped=False,
            order_id=None,
            exit_result=exit_result,
            margin_result=margin_result,
            place_order_result=place_order_result,
            error=str(order_error),
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

    if persist_as_active_buy:
        _save_active_buy_order(
            instrument_details=instrument_details,
            order_id=order_id,
            place_order_result=place_order_result,
        )

    _send_telegram_message(
        title="Order Placed Successfully",
        message=(
            f"Symbol: {trading_symbol}\n"
            f"Instrument: {instrument_key}\n"
            f"Live LTP: {live_ltp}\n"
            f"Lot Size: {lot_size}\n"
            f"Order ID: {order_id or 'N/A'}\n\n"
            "Order workflow completed."
        ),
        level="SUCCESS",
        notification_context=(
            f"process_order|completed|instrument_key={instrument_key}"
        ),
    )

    _save_order_history_entry(
        instrument_details=instrument_details,
        order_status="ORDER_PLACED",
        success=True,
        executed=True,
        skipped=False,
        order_id=order_id,
        exit_result=exit_result,
        margin_result=margin_result,
        place_order_result=place_order_result,
        error=None,
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


# ---------------------------------------------------------------------------
# SELL_EXIT: place SELL for the previously stored BUY instrument
# ---------------------------------------------------------------------------
def _run_sell_exit_step(
    stored_buy_order: dict[str, Any],
) -> dict[str, Any]:
    """
    Sells the previously stored BUY instrument (SELL_EXIT mode).

    IMPORTANT: A FRESH LTP is fetched for the instrument being sold so
    that the stored SELL record captures the exit-time market price,
    enabling entry-vs-exit LTP analysis.
    """
    if not stored_buy_order:
        logger.info("SELL_EXIT: no stored BUY order found. Skipping SELL step.")
        _send_telegram_message(
            title="SELL_EXIT: Nothing To Sell",
            message=(
                "SELL_EXIT is enabled, but no previous BUY order was stored.\n"
                "This looks like the first order — nothing to sell yet."
            ),
            level="INFO",
            notification_context="process_order|sell_exit|no_stored_buy",
        )
        return {
            "success": True,
            "skipped": True,
            "reason": "no_stored_buy_order",
            "instrument": None,
            "sell_order_id": None,
            "sell_result": None,
            "sell_live_ltp": None,
            "buy_live_ltp": None,
            "error": None,
        }

    sell_instrument_key = stored_buy_order.get("instrument_key")
    sell_trading_symbol = stored_buy_order.get("trading_symbol")
    sell_lot_size = stored_buy_order.get("lot_size")
    buy_live_ltp = _safe_float(stored_buy_order.get("buy_live_ltp"))
    if buy_live_ltp is None:
        buy_live_ltp = _safe_float(stored_buy_order.get("live_ltp"))

    if not sell_instrument_key:
        logger.error("SELL_EXIT: stored BUY order is missing instrument_key.")
        _send_telegram_message(
            title="SELL_EXIT: Invalid Stored BUY",
            message=(
                "Stored BUY order is missing instrument_key.\n"
                f"Stored document: {stored_buy_order}"
            ),
            level="ERROR",
            notification_context="process_order|sell_exit|invalid_stored_buy",
        )
        return {
            "success": False,
            "skipped": False,
            "reason": "invalid_stored_buy_order",
            "instrument": stored_buy_order,
            "sell_order_id": None,
            "sell_result": None,
            "sell_live_ltp": None,
            "buy_live_ltp": buy_live_ltp,
            "error": "Stored BUY order is missing instrument_key.",
        }

    if not sell_lot_size or int(sell_lot_size) <= 0:
        logger.error(
            "SELL_EXIT: stored BUY order has invalid lot_size=%s.", sell_lot_size
        )
        _send_telegram_message(
            title="SELL_EXIT: Invalid Stored BUY Lot Size",
            message=(
                f"Stored BUY order has invalid lot_size: {sell_lot_size}\n"
                f"Instrument: {sell_instrument_key}"
            ),
            level="ERROR",
            notification_context="process_order|sell_exit|invalid_lot_size",
        )
        return {
            "success": False,
            "skipped": False,
            "reason": "invalid_stored_buy_lot_size",
            "instrument": stored_buy_order,
            "sell_order_id": None,
            "sell_result": None,
            "sell_live_ltp": None,
            "buy_live_ltp": buy_live_ltp,
            "error": f"Stored BUY order has invalid lot_size: {sell_lot_size}",
        }

    # ------------------------------------------------------------------
    # FRESH LTP FETCH for the instrument being sold.
    # ------------------------------------------------------------------
    logger.info(
        "SELL_EXIT: fetching fresh LTP before selling. instrument_key=%s",
        sell_instrument_key,
    )

    sell_live_ltp = _fetch_live_ltp(sell_instrument_key)

    if sell_live_ltp is None:
        logger.warning(
            "SELL_EXIT: fresh LTP fetch failed. Falling back to stored "
            "BUY-time LTP. instrument_key=%s, buy_live_ltp=%s",
            sell_instrument_key,
            buy_live_ltp,
        )
        sell_live_ltp = buy_live_ltp

    sell_payload = {
        "instrument_key": sell_instrument_key,
        "trading_symbol": sell_trading_symbol,
        "lot_size": int(sell_lot_size),
        "live_ltp": sell_live_ltp,
    }

    logger.info(
        "SELL_EXIT: placing SELL for previously stored BUY. "
        "instrument_key=%s, trading_symbol=%s, lot_size=%s, "
        "buy_live_ltp=%s, sell_live_ltp=%s",
        sell_instrument_key,
        sell_trading_symbol,
        sell_lot_size,
        buy_live_ltp,
        sell_live_ltp,
    )

    _send_telegram_message(
        title="SELL_EXIT: Selling Previous BUY",
        message=(
            "SELL_EXIT mode is enabled.\n"
            "Selling the previously stored BUY instrument.\n"
            f"Symbol: {sell_trading_symbol}\n"
            f"Instrument: {sell_instrument_key}\n"
            f"Lot Size: {sell_lot_size}\n"
            f"BUY LTP: {buy_live_ltp}\n"
            f"SELL LTP (fresh): {sell_live_ltp}\n"
            f"Stored Buy Order ID: {stored_buy_order.get('buy_order_id')}"
        ),
        level="REFRESH",
        notification_context=(
            f"process_order|sell_exit_started|instrument_key={sell_instrument_key}"
        ),
    )

    dummy_orders_enabled = _is_dummy_orders_enabled()

    sell_result: dict[str, Any] | None = None
    sell_order_id: str | None = None
    error_message: str | None = None

    if dummy_orders_enabled:
        sell_order_id = f"DUMMY-SELL-{uuid4().hex[:12].upper()}"
        sell_result = {
            "success": True,
            "dummy": True,
            "order_id": sell_order_id,
            "message": "Dummy SELL simulated. No real order was sent to Upstox.",
            "instrument": sell_payload,
        }

        logger.info(
            "SELL_EXIT: dummy SELL simulated. instrument_key=%s, order_id=%s",
            sell_instrument_key,
            sell_order_id,
        )

        _send_telegram_message(
            title="SELL_EXIT: Dummy SELL Simulated",
            message=(
                "Dummy mode: SELL was simulated.\n"
                f"Symbol: {sell_trading_symbol}\n"
                f"Instrument: {sell_instrument_key}\n"
                f"Dummy Sell Order ID: {sell_order_id}"
            ),
            level="INFO",
            notification_context=(
                f"process_order|sell_exit_dummy|instrument_key={sell_instrument_key}"
            ),
        )

    else:
        try:
            sell_result = place_selected_instrument(
                sell_payload, transaction_type="SELL"
            )
        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "SELL_EXIT: exception while selling stored BUY. "
                "instrument_key=%s, error=%s",
                sell_instrument_key,
                error_message,
            )
            _send_telegram_message(
                title="SELL_EXIT: SELL Exception",
                message=(
                    f"Symbol: {sell_trading_symbol}\n"
                    f"Instrument: {sell_instrument_key}\n"
                    f"Error Type: {type(exc).__name__}\n"
                    f"Error: {exc}\n"
                    "Proceeding to place the new BUY anyway."
                ),
                level="ERROR",
                notification_context=(
                    f"process_order|sell_exit_exception|instrument_key={sell_instrument_key}"
                ),
            )

        if isinstance(sell_result, dict):
            sell_order_id = _extract_order_id(sell_result)

            if sell_result.get("success") is True:
                logger.info(
                    "SELL_EXIT: SELL placed successfully. "
                    "instrument_key=%s, order_id=%s",
                    sell_instrument_key,
                    sell_order_id,
                )
                _send_telegram_message(
                    title="SELL_EXIT: SELL Placed",
                    message=(
                        f"Symbol: {sell_trading_symbol}\n"
                        f"Instrument: {sell_instrument_key}\n"
                        f"SELL Order ID: {sell_order_id or 'N/A'}\n"
                        "Proceeding to place the new BUY."
                    ),
                    level="SUCCESS",
                    notification_context=(
                        f"process_order|sell_exit_success|instrument_key={sell_instrument_key}"
                    ),
                )
            else:
                error_message = (
                    sell_result.get("error")
                    or sell_result.get("message")
                    or "Unknown SELL error."
                )
                logger.error(
                    "SELL_EXIT: SELL reported failure. instrument_key=%s, error=%s",
                    sell_instrument_key,
                    error_message,
                )
                _send_telegram_message(
                    title="SELL_EXIT: SELL Failed",
                    message=(
                        f"Symbol: {sell_trading_symbol}\n"
                        f"Instrument: {sell_instrument_key}\n"
                        f"Error: {error_message}\n"
                        "Proceeding to place the new BUY anyway."
                    ),
                    level="ERROR",
                    notification_context=(
                        f"process_order|sell_exit_failed|instrument_key={sell_instrument_key}"
                    ),
                )
        else:
            error_message = "Invalid SELL response from order service."
            logger.error(
                "SELL_EXIT: invalid SELL response. response_type=%s, response=%r",
                type(sell_result).__name__,
                sell_result,
            )
            _send_telegram_message(
                title="SELL_EXIT: Invalid SELL Response",
                message=(
                    f"Symbol: {sell_trading_symbol}\n"
                    f"Instrument: {sell_instrument_key}\n"
                    f"Response Type: {type(sell_result).__name__}\n"
                    "Proceeding to place the new BUY anyway."
                ),
                level="ERROR",
                notification_context=(
                    f"process_order|sell_exit_invalid_response|instrument_key={sell_instrument_key}"
                ),
            )

    # ------------------------------------------------------------------
    # Persist SELL exit alongside BUY (same document when possible).
    # ------------------------------------------------------------------
    _save_sell_exit_result(
        sell_instrument=sell_payload,
        sell_result=sell_result if isinstance(sell_result, dict) else None,
        error=error_message,
        buy_order_id=stored_buy_order.get("buy_order_id"),
    )

    # ------------------------------------------------------------------
    # Clear the active BUY marker so the next cycle can start fresh.
    # ------------------------------------------------------------------
    _clear_active_buy_order(reason="sold" if not error_message else "sell_failed")

    return {
        "success": sell_result is not None
        and isinstance(sell_result, dict)
        and sell_result.get("success") is True
        and error_message is None,
        "skipped": False,
        "reason": None,
        "instrument": sell_payload,
        "sell_order_id": sell_order_id,
        "sell_live_ltp": sell_live_ltp,
        "buy_live_ltp": buy_live_ltp,
        "sell_result": sell_result,
        "error": error_message,
    }


# ---------------------------------------------------------------------------
# Legacy exit-all-positions step
# ---------------------------------------------------------------------------
def _run_exit_step(
    instrument_details: dict[str, Any],
) -> Any:
    """
    Runs the exit-all-positions step.

    NOTE: In EXIT_ALL mode we do not have per-position detail from
    exit_all_positions(), so we cannot fetch per-instrument exit LTP here.
    The new instrument's LTP has already been fetched in
    _build_instrument_details() and is persisted via order_history.
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


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------
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

    place_order_enabled = bool(getattr(config, "PLACE_ORDER", False))

    dummy_orders_enabled = _is_dummy_orders_enabled()
    sell_exit_enabled = _is_sell_exit_enabled()

    logger.info(
        "Order workflow started. "
        "trading_symbol=%s, instrument_key=%s, "
        "live_ltp=%s, lot_size=%s, place_order_enabled=%s, "
        "dummy_orders_enabled=%s, sell_exit_enabled=%s",
        trading_symbol,
        instrument_key,
        live_ltp,
        lot_size,
        place_order_enabled,
        dummy_orders_enabled,
        sell_exit_enabled,
    )

    _send_telegram_message(
        title="Order Workflow Started",
        message=(
            f"Symbol: {trading_symbol}\n"
            f"Instrument: {instrument_key}\n"
            f"Live LTP: {live_ltp}\n"
            f"Lot Size: {lot_size}\n"
            f"Place Order: {'ENABLED' if place_order_enabled else 'DISABLED'}\n"
            f"Order Mode: {'DUMMY' if dummy_orders_enabled else 'REAL'}\n"
            f"Exit Mode: {'SELL_EXIT' if sell_exit_enabled else 'EXIT_ALL'}"
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

        _save_order_history_entry(
            instrument_details=instrument_details,
            order_status="DISABLED",
            success=False,
            executed=False,
            skipped=True,
            order_id=None,
            exit_result=None,
            margin_result=None,
            place_order_result=None,
            error=None,
            extra={"reason": "PLACE_ORDER=false"},
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

        _save_order_history_entry(
            instrument_details=instrument_details,
            order_status="INVALID_INSTRUMENT",
            success=False,
            executed=False,
            skipped=False,
            order_id=None,
            exit_result=None,
            margin_result=None,
            place_order_result=None,
            error=validation_error,
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

    # ------------------------------------------------------------------
    # Branch: SELL_EXIT vs EXIT_ALL
    # ------------------------------------------------------------------
    if sell_exit_enabled:
        stored_buy_order = _load_active_buy_order()

        if stored_buy_order:
            logger.info(
                "SELL_EXIT: found stored BUY. Selling it before new BUY. "
                "stored_instrument_key=%s",
                stored_buy_order.get("instrument_key"),
            )
            exit_result = _run_sell_exit_step(stored_buy_order)
        else:
            logger.info(
                "SELL_EXIT: no stored BUY found. This looks like the first order."
            )
            exit_result = {
                "success": True,
                "skipped": True,
                "reason": "no_stored_buy_order",
                "instrument": None,
                "sell_order_id": None,
                "sell_result": None,
                "sell_live_ltp": None,
                "buy_live_ltp": None,
                "error": None,
            }

        logger.info(
            "Step 2 started (SELL_EXIT). Placing new BUY. "
            "trading_symbol=%s, instrument_key=%s",
            trading_symbol,
            instrument_key,
        )

        margin_result = _calculate_margin(
            instrument_details=normalized_instrument,
            transaction_type="BUY",
        )

        return _safe_place_order(
            normalized_instrument=normalized_instrument,
            instrument_details=instrument_details,
            exit_result=exit_result,
            margin_result=margin_result,
            persist_as_active_buy=True,
        )

    # ---- Legacy EXIT_ALL mode --------------------------------------
    exit_result = _run_exit_step(normalized_instrument)

    logger.info(
        "Step 2 started. Placing new order. trading_symbol=%s, instrument_key=%s",
        trading_symbol,
        instrument_key,
    )

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
