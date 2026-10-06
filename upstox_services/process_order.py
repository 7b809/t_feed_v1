from __future__ import annotations

import json
import os
from datetime import datetime, date
from pathlib import Path
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
# ---------------------------------------------------------------------------
try:
    from services.order_saving_service import (  # type: ignore
        upstox_order_saving_service,
    )
except Exception:  # pragma: no cover - fallback so file still imports
    upstox_order_saving_service = None  # type: ignore

logger = get_logger(__file__)


# ===========================================================================
# CONFIG FILE LOADING
# ===========================================================================
# Instead of hardcoding collection names / tags / API versions in this file,
# we load them from a JSON config file. Path resolution order:
#   1) env PROCESS_ORDER_CONFIG_FILE
#   2) <project_root>/config/process_order_config.json
#
# Example JSON:
# {
#   "sell_exit_collection_name": "sell_exit_orders",
#   "order_history_collection_name": "order_history",
#   "default_order_tag": "EMA_ALGO",
#   "upstox_api_version": "2.0"
# }
# ===========================================================================

_DEFAULT_CONFIG_FILENAME = "config/process_order_config.json"

# Fallback values used when the config file is missing / unreadable.
_DEFAULT_CONFIG_VALUES: dict[str, Any] = {
    "sell_exit_collection_name": "sell_exit_orders",
    "order_history_collection_name": "order_history",
    "default_order_tag": "EMA_ALGO",
    "upstox_api_version": "2.0",
}

# In-memory cache so we don't hit disk on every call.
_PROCESS_ORDER_CONFIG_CACHE: dict[str, Any] | None = None


def _resolve_config_path() -> Path:
    """
    Resolves the JSON config file path.
    """
    env_path = os.getenv("PROCESS_ORDER_CONFIG_FILE", "").strip()
    if env_path:
        return Path(env_path).expanduser()

    # Try relative to CWD first, then relative to this file's grandparent.
    cwd_candidate = Path(_DEFAULT_CONFIG_FILENAME)
    if cwd_candidate.exists():
        return cwd_candidate

    here = Path(__file__).resolve()
    # services/... -> project root is parents[1] usually; be tolerant.
    for parent in here.parents:
        candidate = parent / _DEFAULT_CONFIG_FILENAME
        if candidate.exists():
            return candidate

    return cwd_candidate  # return default even if it doesn't exist


def _load_process_order_config(force_reload: bool = False) -> dict[str, Any]:
    """
    Loads and caches the process-order config JSON.
    Any missing key falls back to _DEFAULT_CONFIG_VALUES.
    """
    global _PROCESS_ORDER_CONFIG_CACHE

    if _PROCESS_ORDER_CONFIG_CACHE is not None and not force_reload:
        return _PROCESS_ORDER_CONFIG_CACHE

    merged: dict[str, Any] = dict(_DEFAULT_CONFIG_VALUES)
    path = _resolve_config_path()

    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                merged.update(loaded)
                logger.info("Loaded process-order config from %s", path)
            else:
                logger.warning(
                    "Process-order config file %s is not a JSON object. "
                    "Using defaults.",
                    path,
                )
        else:
            logger.warning(
                "Process-order config file not found at %s. Using defaults.",
                path,
            )
    except Exception:
        logger.exception(
            "Failed to read process-order config file at %s. Using defaults.",
            path,
        )

    _PROCESS_ORDER_CONFIG_CACHE = merged
    return merged


def _cfg(key: str, default: Any = None) -> Any:
    """
    Convenience accessor for the process-order config.
    """
    cfg = _load_process_order_config()
    if key in cfg and cfg[key] is not None:
        return cfg[key]
    if default is not None:
        return default
    return _DEFAULT_CONFIG_VALUES.get(key)


# ---------------------------------------------------------------------------
# Values pulled from the config file (evaluated lazily at call time).
# ---------------------------------------------------------------------------
def _sell_exit_collection_name() -> str:
    return str(_cfg("sell_exit_collection_name")).strip() or "sell_exit_orders"


def _order_history_collection_name() -> str:
    return str(_cfg("order_history_collection_name")).strip() or "order_history"


def _default_order_tag() -> str:
    return str(_cfg("default_order_tag")).strip() or "EMA_ALGO"


def _upstox_api_version() -> str:
    return str(_cfg("upstox_api_version")).strip() or "2.0"


# ---------------------------------------------------------------------------
# Config helpers (env-based, unchanged API)
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
    """
    return bool(
        getattr(config, "SELL_EXIT", False) or getattr(config, "sell_exit", False)
    )


def _get_upstox_access_token() -> str | None:
    """
    Resolves the Upstox access token from the available configuration.
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

    getter = getattr(config, "get_upstox_access_token", None)
    if callable(getter):
        try:
            value = getter()
            if value:
                return str(value).strip()
        except Exception:
            logger.exception("config.get_upstox_access_token() raised an exception.")

    return None


# ===========================================================================
# MongoDB access (reuses order_saving_service's connection settings)
# ===========================================================================
def _get_mongo_database():
    """
    Returns the MongoDB database handle used by the order-saving service.
    """
    if upstox_order_saving_service is not None:
        try:
            collection = upstox_order_saving_service._get_collection()  # type: ignore[attr-defined]
            if collection is not None:
                db = getattr(collection, "database", None)
                if db is not None:
                    return db
        except Exception:
            logger.exception(
                "Failed to obtain MongoDB database via "
                "order_saving_service. Falling back to direct config."
            )

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


# ===========================================================================
# TELEGRAM helper
# ===========================================================================
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


# ===========================================================================
# Safe conversion helpers
# ===========================================================================
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


# ===========================================================================
# Date-wise single-document helpers
# ===========================================================================
def _today_str() -> str:
    """
    Returns today's date as YYYY-MM-DD (local time).
    """
    return date.today().isoformat()


def _now_time_key(transaction_type: str) -> str:
    """
    Builds a time-based key like '10:54_BUY' or '14:13_SELL'.

    transaction_type is normalized to uppercase; anything non-SELL becomes
    'BUY'.
    """
    hhmm = datetime.now().strftime("%H:%M")
    side = "SELL" if str(transaction_type).strip().upper() == "SELL" else "BUY"
    return f"{hhmm}_{side}"


def _get_daily_collection(collection_name: str):
    """
    Returns a MongoDB collection handle for the given collection name.
    """
    db = _get_mongo_database()
    return db[collection_name]


def _ensure_daily_doc(collection, *, strategy: str) -> dict[str, Any]:
    """
    Ensures today's single daily document exists in the collection.
    Returns the (upserted) document filter used.
    """
    now_iso = datetime.now().isoformat()
    day = _today_str()

    doc_filter = {"date": day, "strategy": strategy}

    collection.update_one(
        doc_filter,
        {
            "$setOnInsert": {
                "date": day,
                "strategy": strategy,
                "created_at": now_iso,
            },
            "$set": {"updated_at": now_iso},
        },
        upsert=True,
    )
    return doc_filter


def _append_daily_entry(
    collection_name: str,
    *,
    strategy: str,
    transaction_type: str,
    payload: dict[str, Any],
    key_hint: str | None = None,
) -> dict[str, Any]:
    """
    Appends a time-based entry to today's daily document.

    Document shape (per collection):
        {
          "date": "YYYY-MM-DD",
          "strategy": "<tag>",
          "entries": {
              "10:54_BUY":  { ... },   # payload merged with key + timestamp
              "14:13_SELL": { ... }
          },
          "created_at": "...",
          "updated_at": "..."
        }

    Collision handling: if the exact time key already exists, a suffix
    '_2', '_3', ... is appended so data is never overwritten.

    Returns the final entry key used.
    """
    now_iso = datetime.now().isoformat()
    day = _today_str()

    collection = _get_daily_collection(collection_name)
    _ensure_daily_doc(collection, strategy=strategy)

    base_key = key_hint or _now_time_key(transaction_type)

    # Find a free key (avoid overwriting an existing entry in the same minute).
    final_key = base_key
    suffix = 1
    while True:
        existing = collection.find_one(
            {
                "date": day,
                "strategy": strategy,
                f"entries.{final_key}": {"$exists": True},
            },
            {"_id": 1},
        )
        if not existing:
            break
        suffix += 1
        final_key = f"{base_key}_{suffix}"

    entry_doc = {
        **payload,
        "entry_key": final_key,
        "transaction_type": (
            "SELL" if str(transaction_type).strip().upper() == "SELL" else "BUY"
        ),
        "recorded_at": now_iso,
    }

    try:
        collection.update_one(
            {"date": day, "strategy": strategy},
            {
                "$set": {
                    f"entries.{final_key}": entry_doc,
                    "updated_at": now_iso,
                }
            },
            upsert=True,
        )
        logger.info(
            "Daily entry appended. collection=%s, date=%s, key=%s",
            collection_name,
            day,
            final_key,
        )
    except Exception:
        logger.exception(
            "Failed to append daily entry. collection=%s, date=%s, key=%s",
            collection_name,
            day,
            final_key,
        )

    return entry_doc


def _append_daily_entry_to_specific_key(
    collection_name: str,
    *,
    strategy: str,
    entry_key: str,
    payload: dict[str, Any],
) -> None:
    """
    Writes/overwrites a specific entry key in today's daily document.
    Used when we need to merge a SELL back into the BUY entry key.
    """
    now_iso = datetime.now().isoformat()
    day = _today_str()

    collection = _get_daily_collection(collection_name)
    _ensure_daily_doc(collection, strategy=strategy)

    try:
        collection.update_one(
            {"date": day, "strategy": strategy},
            {
                "$set": {
                    f"entries.{entry_key}": {
                        **payload,
                        "entry_key": entry_key,
                        "recorded_at": now_iso,
                    },
                    "updated_at": now_iso,
                }
            },
            upsert=True,
        )
        logger.info(
            "Daily entry merged. collection=%s, date=%s, key=%s",
            collection_name,
            day,
            entry_key,
        )
    except Exception:
        logger.exception(
            "Failed to merge daily entry. collection=%s, date=%s, key=%s",
            collection_name,
            day,
            entry_key,
        )


def _load_active_buy_order() -> dict[str, Any] | None:
    """
    Returns the currently active BUY entry from today's daily document.

    An entry is considered active if:
        - it exists at any key matching '*_BUY*'
        - it has no 'closed_at' / 'sell_order_id' yet
    """
    strategy = _default_order_tag()
    day = _today_str()

    try:
        collection = _get_daily_collection(_sell_exit_collection_name())
        doc = collection.find_one({"date": day, "strategy": strategy})
        if not doc:
            return None

        entries = doc.get("entries") or {}
        if not isinstance(entries, dict):
            return None

        # Prefer the most recently recorded BUY without a matching SELL.
        candidates: list[tuple[str, dict[str, Any]]] = []
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            if (
                not str(key).upper().endswith("_BUY")
                and "_BUY_" not in str(key).upper()
            ):
                # allow keys like "10:54_BUY_2"
                if not str(key).upper().split("_", 1)[-1].startswith("BUY"):
                    continue
            if entry.get("sell_order_id") or entry.get("closed_at"):
                continue
            candidates.append((str(key), entry))

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[1].get("recorded_at") or "", reverse=True)
        _key, entry = candidates[0]

        # Provide a top-level "buy_order_id" for the SELL step.
        entry = {**entry, "buy_order_id": entry.get("order_id")}
        return entry

    except Exception:
        logger.exception("Failed to load active BUY order from MongoDB.")
        return None


# ===========================================================================
# Live LTP fetch via Upstox MarketQuoteApi
# ===========================================================================
def _fetch_live_ltp(instrument_key: str) -> float | None:
    """
    Fetches the live LTP for the given Upstox instrument_key.
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
            "Live LTP fetch skipped: no Upstox access token available in config."
        )
        return None

    try:
        configuration = upstox_client.Configuration()
        configuration.access_token = access_token

        api_instance = upstox_client.MarketQuoteApi(
            upstox_client.ApiClient(configuration)
        )

        api_response = api_instance.ltp(instrument_key, _upstox_api_version())

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


# ===========================================================================
# Instrument details builder (fetches live LTP)
# ===========================================================================
def _build_instrument_details(
    selected_instrument: dict[str, Any],
) -> dict[str, Any]:
    """
    Builds the normalized instrument details dict.
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
        "option_type": selected_instrument.get("option_type")
        or selected_instrument.get("instrument_type"),
        "instrument_type": selected_instrument.get("instrument_type")
        or selected_instrument.get("option_type"),
        "strike_price": selected_instrument.get("strike_price"),
        "expiry": selected_instrument.get("expiry"),
        "underlying_symbol": selected_instrument.get("underlying_symbol"),
        "strategy_instrument": selected_instrument.get("strategy_instrument"),
        "order_target_mode": selected_instrument.get("order_target_mode"),
        "order_selection_reason": selected_instrument.get("order_selection_reason"),
        "order_target": selected_instrument.get("order_target"),
    }


# ===========================================================================
# Result builders
# ===========================================================================
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


# ===========================================================================
# Order id extraction
# ===========================================================================
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


# ===========================================================================
# Daily-document writers (new date-wise model)
# ===========================================================================
def _save_active_buy_order(
    instrument_details: dict[str, Any],
    *,
    order_id: str | None,
    place_order_result: Any = None,
) -> str:
    """
    Appends a BUY entry into today's single document in
    `sell_exit_orders` collection.

    Returns the entry key (e.g. '10:54_BUY').
    """
    entry = _append_daily_entry(
        _sell_exit_collection_name(),
        strategy=_default_order_tag(),
        transaction_type="BUY",
        payload={
            "type": "BUY_ENTRY",
            "instrument_key": instrument_details.get("instrument_key"),
            "trading_symbol": instrument_details.get("trading_symbol"),
            "lot_size": instrument_details.get("lot_size"),
            "buy_live_ltp": instrument_details.get("live_ltp"),
            "live_ltp": instrument_details.get("live_ltp"),
            "strategy_instrument": instrument_details.get("strategy_instrument"),
            "order_target_mode": instrument_details.get("order_target_mode"),
            "order_selection_reason": instrument_details.get("order_selection_reason"),
            "order_target": instrument_details.get("order_target"),
            "order_id": order_id,
            "buy_order_id": order_id,
            "place_order_result": place_order_result,
        },
    )
    return str(entry.get("entry_key") or "")


def _append_sell_exit_result(
    sell_instrument: dict[str, Any],
    sell_result: dict[str, Any] | None,
    *,
    error: str | None = None,
    buy_entry_key: str | None = None,
    buy_order_id: str | None = None,
) -> str:
    """
    Appends a SELL entry into today's single document in
    `sell_exit_orders` collection.

    If `buy_entry_key` is provided, we also merge the SELL data back into
    the matching BUY entry (so the BUY entry becomes a TRADE_PAIR).
    """
    now_iso = datetime.now().isoformat()
    sell_order_id = _extract_order_id(sell_result) if sell_result else None
    sell_live_ltp = _safe_float(sell_instrument.get("live_ltp"))

    entry = _append_daily_entry(
        _sell_exit_collection_name(),
        strategy=_default_order_tag(),
        transaction_type="SELL",
        payload={
            "type": "SELL_EXIT",
            "instrument_key": sell_instrument.get("instrument_key"),
            "trading_symbol": sell_instrument.get("trading_symbol"),
            "lot_size": sell_instrument.get("lot_size"),
            "sell_live_ltp": sell_live_ltp,
            "live_ltp": sell_live_ltp,
            "sell_order_id": sell_order_id,
            "sell_result": sell_result,
            "sell_error": error,
            "buy_order_id": buy_order_id,
            "paired_buy_entry_key": buy_entry_key,
            "closed_at": now_iso if not error else None,
        },
    )
    sell_entry_key = str(entry.get("entry_key") or "")

    # Merge SELL fields back into the matching BUY entry for a paired view.
    if buy_entry_key:
        _merge_sell_into_buy_entry(
            buy_entry_key=buy_entry_key,
            sell_entry_key=sell_entry_key,
            sell_order_id=sell_order_id,
            sell_live_ltp=sell_live_ltp,
            sell_result=sell_result,
            error=error,
        )

    return sell_entry_key


def _merge_sell_into_buy_entry(
    *,
    buy_entry_key: str,
    sell_entry_key: str,
    sell_order_id: str | None,
    sell_live_ltp: float | None,
    sell_result: dict[str, Any] | None,
    error: str | None,
) -> None:
    """
    Merges SELL fields into the existing BUY entry inside today's document.
    """
    strategy = _default_order_tag()
    day = _today_str()
    now_iso = datetime.now().isoformat()

    try:
        collection = _get_daily_collection(_sell_exit_collection_name())

        doc = collection.find_one({"date": day, "strategy": strategy})
        if not doc:
            return

        entries = doc.get("entries") or {}
        buy_entry = entries.get(buy_entry_key)
        if not isinstance(buy_entry, dict):
            return

        buy_ltp = _safe_float(buy_entry.get("buy_live_ltp"))
        if buy_ltp is None:
            buy_ltp = _safe_float(buy_entry.get("live_ltp"))

        ltp_change = None
        ltp_change_pct = None
        if buy_ltp is not None and sell_live_ltp is not None:
            ltp_change = sell_live_ltp - buy_ltp
            if buy_ltp != 0:
                ltp_change_pct = (ltp_change / buy_ltp) * 100.0

        merged_buy_entry = {
            **buy_entry,
            "type": "TRADE_PAIR",
            "sell_entry_key": sell_entry_key,
            "sell_order_id": sell_order_id,
            "sell_live_ltp": sell_live_ltp,
            "sell_result": sell_result,
            "sell_error": error,
            "ltp_change": ltp_change,
            "ltp_change_pct": ltp_change_pct,
            "closed_at": now_iso if not error else None,
        }

        collection.update_one(
            {"date": day, "strategy": strategy},
            {
                "$set": {
                    f"entries.{buy_entry_key}": merged_buy_entry,
                    "updated_at": now_iso,
                }
            },
        )
        logger.info(
            "SELL merged into BUY entry. buy_key=%s, sell_key=%s",
            buy_entry_key,
            sell_entry_key,
        )
    except Exception:
        logger.exception(
            "Failed to merge SELL into BUY entry. buy_key=%s, sell_key=%s",
            buy_entry_key,
            sell_entry_key,
        )


def _append_order_history_entry(
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
    transaction_type: str = "BUY",
) -> str:
    """
    Appends an order-history entry into today's single document in
    `order_history` collection.
    """
    payload: dict[str, Any] = {
        "type": "ORDER_HISTORY",
        "instrument_key": instrument_details.get("instrument_key"),
        "trading_symbol": instrument_details.get("trading_symbol"),
        "lot_size": instrument_details.get("lot_size"),
        "live_ltp": instrument_details.get("live_ltp"),
        "strategy_instrument": instrument_details.get("strategy_instrument"),
        "order_target_mode": instrument_details.get("order_target_mode"),
        "order_selection_reason": instrument_details.get("order_selection_reason"),
        "order_target": instrument_details.get("order_target"),
        "order_status": order_status,
        "success": bool(success),
        "executed": bool(executed),
        "skipped": bool(skipped),
        "order_id": order_id,
        "exit_result": exit_result,
        "margin_result": margin_result,
        "place_order_result": place_order_result,
        "error": error,
        "processed_at": datetime.now().isoformat(),
    }

    if extra:
        payload.update(extra)

    entry = _append_daily_entry(
        _order_history_collection_name(),
        strategy=_default_order_tag(),
        transaction_type=transaction_type,
        payload=payload,
    )
    return str(entry.get("entry_key") or "")


# ===========================================================================
# Margin calculation
# ===========================================================================
def _calculate_margin(
    instrument_details: dict[str, Any],
    *,
    transaction_type: str = "BUY",
) -> dict[str, Any]:
    """
    Calculates margin for the given instrument using the margin service.
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


# ===========================================================================
# Dummy order builder
# ===========================================================================
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


# ===========================================================================
# Safe place order (BUY-side)
# ===========================================================================
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

        buy_entry_key: str | None = None
        if persist_as_active_buy:
            buy_entry_key = _save_active_buy_order(
                instrument_details=instrument_details,
                order_id=order_id,
                place_order_result=place_order_result,
            )

        _append_order_history_entry(
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
            extra={"dummy": True, "buy_entry_key": buy_entry_key},
            transaction_type="BUY",
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

        _append_order_history_entry(
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
            transaction_type="BUY",
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

        _append_order_history_entry(
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
            transaction_type="BUY",
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

        _append_order_history_entry(
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
            transaction_type="BUY",
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

    buy_entry_key: str | None = None
    if persist_as_active_buy:
        buy_entry_key = _save_active_buy_order(
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

    _append_order_history_entry(
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
        extra={"buy_entry_key": buy_entry_key},
        transaction_type="BUY",
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


# ===========================================================================
# SELL_EXIT: place SELL for the previously stored BUY instrument
# ===========================================================================
def _run_sell_exit_step(
    stored_buy_order: dict[str, Any],
) -> dict[str, Any]:
    """
    Sells the previously stored BUY instrument (SELL_EXIT mode).
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
            "buy_entry_key": None,
            "error": None,
        }

    sell_instrument_key = stored_buy_order.get("instrument_key")
    sell_trading_symbol = stored_buy_order.get("trading_symbol")
    sell_lot_size = stored_buy_order.get("lot_size")
    buy_entry_key = stored_buy_order.get("entry_key")
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
            "buy_entry_key": buy_entry_key,
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
            "buy_entry_key": buy_entry_key,
            "error": f"Stored BUY order has invalid lot_size: {sell_lot_size}",
        }

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

    sell_entry_key = _append_sell_exit_result(
        sell_instrument=sell_payload,
        sell_result=sell_result if isinstance(sell_result, dict) else None,
        error=error_message,
        buy_entry_key=buy_entry_key,
        buy_order_id=stored_buy_order.get("buy_order_id"),
    )

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
        "buy_entry_key": buy_entry_key,
        "sell_entry_key": sell_entry_key,
        "error": error_message,
    }


# ===========================================================================
# Legacy exit-all-positions step
# ===========================================================================
def _run_exit_step(
    instrument_details: dict[str, Any],
) -> Any:
    """
    Runs the exit-all-positions step.
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


# ===========================================================================
# Main workflow
# ===========================================================================
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

        _append_order_history_entry(
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
            transaction_type="BUY",
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

        _append_order_history_entry(
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
            transaction_type="BUY",
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
        "option_type": instrument_details.get("option_type"),
        "instrument_type": instrument_details.get("instrument_type"),
        "strike_price": instrument_details.get("strike_price"),
        "expiry": instrument_details.get("expiry"),
        "underlying_symbol": instrument_details.get("underlying_symbol"),
        "strategy_instrument": instrument_details.get("strategy_instrument"),
        "order_target_mode": instrument_details.get("order_target_mode"),
        "order_selection_reason": instrument_details.get("order_selection_reason"),
        "order_target": instrument_details.get("order_target"),
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
                "buy_entry_key": None,
                "sell_entry_key": None,
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
