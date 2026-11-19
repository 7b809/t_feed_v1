"""Order lifecycle orchestration using the official Upstox Python SDK."""
from __future__ import annotations

import ast
import json
import re
import threading
import uuid
from typing import Any

import upstox_client

from core.config import (
    DEFAULT_ORDER_QUANTITY,
    DEFAULT_PRODUCT,
    DEFAULT_VALIDITY,
    ORDERS_VERBOSE,  # <-- controls extra raw_error detail when true
    TRADING_ENABLED,
    UPSTOX_SANDBOX,
    TIMEZONE,  # noqa: F401  (kept for symmetry with config surface)
)
from core.logger import get_logger
from core.token_service import fetch_access_token
from core.telegram_notifier import notify_event
from models.order_models import PlaceOrderRequest
from repositories.order_repository import OrderRepository, now_iso

logger = get_logger(__file__)

# Re-exported here so existing callers that did
#   from services.order_service import _TZ
# continue to import cleanly, even though no internal code uses it.
from zoneinfo import ZoneInfo as _ZoneInfo

_TZ = _ZoneInfo(TIMEZONE)

# Friendly messages mapped by Upstox error codes / common causes.
# Raw Upstox messages are still preserved inside `broker_error.errors[].message`.
_FRIENDLY_ERROR_MESSAGES: dict[str, str] = {
    "UDAPI1154": "Order not placed due to static IP restriction.",
    "UDAPI1134": "GTT order ID is required.",
    "UDAPI1135": "GTT order ID must start with 'GTT-'.",
    "UDAPI1156": "Invalid algo name in request headers.",
    "TRADING_DISABLED": "Order not placed because trading is disabled.",
}


class OrderServiceError(RuntimeError):
    """An order operation could not safely be completed."""


class TradingDisabledError(OrderServiceError):
    """Order execution is disabled by configuration."""


def _plain(value: Any) -> Any:
    """Convert SDK model objects into JSON-compatible Python values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "to_dict"):
        try:
            return _plain(value.to_dict())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return {
            str(k): _plain(v)
            for k, v in vars(value).items()
            if not str(k).startswith("_")
        }
    return str(value)


def _response_data(response: Any) -> dict:
    raw = _plain(response)
    if not isinstance(raw, dict):
        return {"raw_response": raw}
    data = raw.get("data")
    return data if isinstance(data, dict) else raw


def _format_success_response(response: Any, *, default_status: int = 200) -> dict:
    """Normalize a successful Upstox SDK response into standard API shape."""
    plain = _plain(response)
    if not isinstance(plain, dict):
        return {
            "http_status": default_status,
            "status": "success",
            "data": {"result": plain},
        }

    formatted: dict[str, Any] = {
        "http_status": default_status,
        "status": plain.get("status") or "success",
    }
    if "data" in plain and plain["data"] is not None:
        formatted["data"] = plain["data"]
    else:
        formatted["data"] = {
            k: v for k, v in plain.items() if k not in {"status", "metadata"}
        }
    if "metadata" in plain and plain["metadata"] is not None:
        formatted["metadata"] = plain["metadata"]
    return formatted


def _simplify_error_message(
    error_code: str | None, raw_message: str, action: str = "order"
) -> str:
    """Return a short, human-friendly summary for quick reading."""
    code = (error_code or "").strip()
    if code in _FRIENDLY_ERROR_MESSAGES:
        msg = _FRIENDLY_ERROR_MESSAGES[code]
        if action == "cancel":
            return msg.replace("Order not placed", "Order not cancelled")
        if action == "exit":
            return msg.replace("Order not placed", "Order not exited")
        return msg

    lower_raw = (raw_message or "").lower()
    if (
        "static ip" in lower_raw
        or "ip restrictions" in lower_raw
        or "udapi1154" in lower_raw
    ):
        if action == "cancel":
            return "Order not cancelled due to static IP restriction."
        if action == "exit":
            return "Order not exited due to static IP restriction."
        return "Order not placed due to static IP restriction."

    if "trading_enabled" in lower_raw or "trading is disabled" in lower_raw:
        return "Order not placed because trading is disabled."

    if "insufficient" in lower_raw or "margin" in lower_raw or "fund" in lower_raw:
        return "Order not placed due to insufficient funds or margin."

    if "market" in lower_raw and ("closed" in lower_raw or "outside" in lower_raw):
        return "Order not placed because the market is closed."

    first_line = (raw_message or "").splitlines()[0].strip()
    if len(first_line) > 160:
        first_line = first_line[:157] + "..."
    return first_line or f"{action.capitalize()} failed."


def _parse_api_exception(exc: Exception, *, action: str = "order") -> dict:
    """Parse an exception/ApiException into Upstox error format while keeping raw error."""
    http_status = getattr(exc, "status", None)
    reason = getattr(exc, "reason", None)
    raw_body = getattr(exc, "body", None)
    text = str(exc)

    if http_status is None:
        match = re.search(r"ApiException:\s*\((\d+)\)", text)
        if match:
            http_status = int(match.group(1))

    body_text: str | None = None
    if raw_body is not None:
        if isinstance(raw_body, bytes):
            body_text = raw_body.decode("utf-8", errors="replace")
        else:
            body_text = str(raw_body)
    else:
        body_marker = "\nHTTP response body:"
        idx = text.find(body_marker)
        if idx >= 0:
            candidate = text[idx + len(body_marker) :].strip()
            if candidate.startswith(("b'", 'b"')):
                try:
                    parsed_bytes = ast.literal_eval(candidate)
                    if isinstance(parsed_bytes, bytes):
                        body_text = parsed_bytes.decode("utf-8", errors="replace")
                    else:
                        body_text = str(parsed_bytes)
                except Exception:
                    body_text = candidate
            else:
                body_text = candidate

    clean_pre_header = text.split("\nHTTP response headers:", 1)[0].strip()

    if body_text:
        try:
            parsed_json = json.loads(body_text)
            if isinstance(parsed_json, dict):
                errors = parsed_json.get("errors")
                normalized_errors: list[dict[str, Any]] = []
                if isinstance(errors, list):
                    for err in errors:
                        if isinstance(err, dict):
                            code = err.get("errorCode") or err.get("error_code")
                            exact_msg = err.get("message") or ""
                            prop = err.get("propertyPath") or err.get("property_path")
                            inv_val = err.get("invalidValue") or err.get("invalid_value")
                            normalized_errors.append(
                                {
                                    "errorCode": code,
                                    "message": exact_msg,
                                    "propertyPath": prop,
                                    "invalidValue": inv_val,
                                    "error_code": code,
                                    "property_path": prop,
                                    "invalid_value": inv_val,
                                }
                            )
                if not normalized_errors:
                    exact_msg = (
                        parsed_json.get("message")
                        or reason
                        or clean_pre_header
                        or type(exc).__name__
                    )
                    code = type(exc).__name__
                    normalized_errors.append(
                        {
                            "errorCode": code,
                            "message": exact_msg,
                            "propertyPath": None,
                            "invalidValue": None,
                            "error_code": code,
                            "property_path": None,
                            "invalid_value": None,
                        }
                    )

                result: dict[str, Any] = {
                    "http_status": http_status,
                    "status": parsed_json.get("status") or "error",
                    "errors": normalized_errors,
                }
                if "metadata" in parsed_json and parsed_json["metadata"] is not None:
                    result["metadata"] = parsed_json["metadata"]
                if ORDERS_VERBOSE:
                    result["raw_error"] = clean_pre_header or body_text
                return result
        except Exception:
            pass

    fallback_msg = body_text or clean_pre_header or str(reason) or type(exc).__name__
    code = type(exc).__name__
    result = {
        "http_status": http_status,
        "status": "error",
        "errors": [
            {
                "errorCode": code,
                "message": fallback_msg,
                "propertyPath": None,
                "invalidValue": None,
                "error_code": code,
                "property_path": None,
                "invalid_value": None,
            }
        ],
    }
    if ORDERS_VERBOSE:
        result["raw_error"] = clean_pre_header or fallback_msg
    return result


def _exception_text(exc: Exception, *, action: str = "order") -> str:
    """Simplified human-readable summary stored in the `error` field."""
    parsed = _parse_api_exception(exc, action=action)
    errs = parsed.get("errors") or []
    if errs:
        first = errs[0]
        code = first.get("errorCode") or first.get("error_code")
        raw_msg = first.get("message") or ""
        return _simplify_error_message(code, raw_msg, action=action)
    return _simplify_error_message(type(exc).__name__, str(exc), action=action)


def _event_details(**details) -> dict:
    """Clean event payload without empty None noise."""
    cleaned: dict[str, Any] = {}
    for k, v in details.items():
        if v is None and k in {"broker_response", "broker_error", "raw_response"}:
            continue
        cleaned[k] = _plain(v)
    return cleaned


class OrderService:
    """Place, cancel and square-off orders, recording every action."""

    def __init__(self, repository: OrderRepository | None = None) -> None:
        self.repository = repository or OrderRepository()
        self._operation_lock = threading.RLock()

    def _clients(self):
        token = fetch_access_token()
        configuration = upstox_client.Configuration(sandbox=UPSTOX_SANDBOX)
        configuration.access_token = token
        client = upstox_client.ApiClient(configuration)
        # V3 for place/cancel; V2 OrderApi exposes order details and exit_positions.
        return upstox_client.OrderApiV3(client), upstox_client.OrderApi(client)

    def _require_trading(self) -> None:
        if not TRADING_ENABLED:
            raise TradingDisabledError(
                "Order execution is disabled. Set TRADING_ENABLED=true in .env "
                "after verifying the Upstox environment and account."
            )

    @staticmethod
    def _new_event(event_type: str, **details) -> dict:
        return {
            "event_type": event_type,
            "event_time": now_iso(),
            **_event_details(**details),
        }

    def _new_entry(self, record_type: str, **fields) -> dict:
        stamp = now_iso()
        return {
            "local_id": str(uuid.uuid4()),
            "record_type": record_type,
            "status": "REQUESTED",
            "order_id": None,
            "order_ids": [],
            "created_time": stamp,
            "last_modified_time": stamp,
            "events": [],
            **fields,
        }

    def _append_event(
        self,
        local_id: str,
        event: dict,
        *,
        status: str | None = None,
        require_mongo: bool = False,
    ) -> dict:
        entry = self.repository.find_entry(local_id)
        if not entry:
            raise OrderServiceError(f"Order audit record disappeared: {local_id}")
        events = list(entry.get("events") or [])
        events.append(event)
        updates = {"events": events, "last_modified_time": now_iso()}
        if status:
            updates["status"] = status
        return self.repository.update_entry(
            local_id, updates, require_mongo=require_mongo
        )

    def _persist_before_action(self, entry: dict, action: str) -> None:
        """Require Mongo + local audit persistence before any broker mutation."""
        try:
            self.repository.append_entry(entry, require_mongo=True)
        except Exception as exc:
            err_resp = _parse_api_exception(exc, action=action)
            summary_err = _exception_text(exc, action=action)
            try:
                existing = self.repository.find_entry(entry["local_id"])
                event = self._new_event(
                    "PERSISTENCE_EXCEPTION",
                    action=action,
                    error=summary_err,
                    broker_error=err_resp,
                    broker_request_sent=False,
                )
                if existing:
                    events = list(existing.get("events") or [])
                    events.append(event)
                    self.repository.update_entry(
                        entry["local_id"],
                        {
                            "status": "PERSISTENCE_EXCEPTION",
                            "error": summary_err,
                            "broker_error": err_resp,
                            "events": events,
                            "last_modified_time": now_iso(),
                        },
                        require_mongo=False,
                    )
                else:
                    entry["status"] = "PERSISTENCE_EXCEPTION"
                    entry["error"] = summary_err
                    entry["broker_error"] = err_resp
                    entry.setdefault("events", []).append(event)
                    self.repository.append_entry(entry, require_mongo=False)
            except Exception:
                logger.exception("Could not persist pre-action exception audit")
            raise OrderServiceError(
                f"Persistence failed before {action}; no Upstox request was sent."
            ) from exc

    # ---------------------------------------------------------------
    # PLACE ORDER
    # ---------------------------------------------------------------
    def place_order(self, request: PlaceOrderRequest) -> dict:
        """Place a V3 order. Defaults are BUY, 65 units, MARKET and Intraday."""
        payload = request.model_dump()
        entry = self._new_entry(
            "ORDER",
            instrument_token=request.instrument_token,
            trading_symbol=request.trading_symbol,
            segment=request.segment,
            quantity=request.quantity or DEFAULT_ORDER_QUANTITY,
            transaction_type=request.transaction_type or "BUY",
            product=request.product or DEFAULT_PRODUCT,
            order_type=request.order_type or "MARKET",
            validity=request.validity or DEFAULT_VALIDITY,
            price=request.price,
            tag=request.tag,
            exit_orders_by_order_id={},
            request=payload,
        )
        entry["events"].append(self._new_event("PLACE_REQUESTED", request=payload))
        self._persist_before_action(entry, "place_order")

        if not TRADING_ENABLED:
            blocked_msg = _simplify_error_message("TRADING_DISABLED", "", action="order")
            blocked_resp = {
                "http_status": 403,
                "status": "error",
                "errors": [
                    {
                        "errorCode": "TRADING_DISABLED",
                        "message": blocked_msg,
                        "propertyPath": None,
                        "invalidValue": None,
                        "error_code": "TRADING_DISABLED",
                        "property_path": None,
                        "invalid_value": None,
                    }
                ],
            }
            self._append_event(
                entry["local_id"],
                self._new_event(
                    "TRADING_DISABLED",
                    reason="TRADING_ENABLED=false",
                    error=blocked_msg,
                    broker_error=blocked_resp,
                ),
                status="BLOCKED",
                require_mongo=False,
            )
            notify_event(
                "Order blocked — trading disabled",
                [
                    f"Instrument: {request.trading_symbol or request.instrument_token}",
                    f"Quantity: {request.quantity}",
                    f"Reason: {blocked_msg}",
                ],
                "WARNING",
            )
            raise TradingDisabledError(blocked_msg)

        try:
            order_v3, _ = self._clients()
            body = upstox_client.PlaceOrderV3Request(
                quantity=request.quantity,
                product=request.product,
                validity=request.validity,
                price=request.price if request.order_type != "MARKET" else 0,
                tag=request.tag,
                instrument_token=request.instrument_token,
                order_type=request.order_type,
                transaction_type=request.transaction_type,
                disclosed_quantity=request.disclosed_quantity,
                trigger_price=request.trigger_price,
                is_amo=request.is_amo,
                slice=request.slice,
            )
            response = order_v3.place_order(body)
            formatted_resp = _format_success_response(response, default_status=200)
            response_data = (
                formatted_resp.get("data")
                if isinstance(formatted_resp.get("data"), dict)
                else _response_data(response)
            )
            ids = response_data.get("order_ids") or []
            if not ids and response_data.get("order_id"):
                ids = [response_data["order_id"]]
            ids = [str(order_id) for order_id in ids if order_id]
            if not ids:
                raise OrderServiceError(
                    f"Upstox response did not contain order_ids: {response_data}"
                )

            events = list(entry["events"])
            events.append(
                self._new_event(
                    "ORDER_PLACED",
                    order_ids=ids,
                    broker_response=formatted_resp,
                )
            )
            updates = {
                "order_id": ids[0] if len(ids) == 1 else None,
                "order_ids": ids,
                "status": "PLACED",
                "broker_response": formatted_resp,
                "events": events,
                "last_modified_time": now_iso(),
            }
            persistence = self.repository.update_entry(
                entry["local_id"], updates, require_mongo=False
            )
            logger.info(
                "Upstox order accepted | ids=%s | side=%s | qty=%s | instrument=%s",
                ids,
                request.transaction_type,
                request.quantity,
                request.instrument_token,
            )
            notify_event(
                "Order placed",
                [
                    f"Symbol: {request.trading_symbol or request.instrument_token}",
                    f"Side: {request.transaction_type} | Qty: {request.quantity}",
                    f"Type: {request.order_type} | Product: {request.product}",
                    "Order ID(s): " + ", ".join(ids),
                    "Mode: " + ("Sandbox" if UPSTOX_SANDBOX else "Live"),
                ],
                "ORDER",
            )
            return {
                "status": "placed",
                "order_ids": ids,
                "order": updates,
                "broker_response": formatted_resp,
                "persistence": persistence,
                "sandbox": UPSTOX_SANDBOX,
            }
        except TradingDisabledError:
            raise
        except Exception as exc:
            err_resp = _parse_api_exception(exc, action="order")
            summary_err = _exception_text(exc, action="order")
            try:
                self._append_event(
                    entry["local_id"],
                    self._new_event(
                        "ORDER_EXCEPTION",
                        error=summary_err,
                        broker_error=err_resp,
                    ),
                    status="EXCEPTION",
                    require_mongo=False,
                )
                self.repository.update_entry(
                    entry["local_id"],
                    {
                        "error": summary_err,
                        "broker_error": err_resp,
                        "last_modified_time": now_iso(),
                    },
                    require_mongo=False,
                )
            except Exception:
                logger.exception("Could not save order exception audit")
            logger.exception("Upstox order placement failed")
            notify_event(
                "Order placement failed",
                [
                    f"Symbol: {request.trading_symbol or request.instrument_token}",
                    f"Quantity: {request.quantity}",
                    f"Error: {summary_err}",
                ],
                "ERROR",
            )
            raise

    def _get_or_create_order_entry(self, order_id: str) -> dict:
        entry = self.repository.find_order(order_id)
        if entry:
            return entry
        entry = self._new_entry(
            "EXTERNAL_ORDER",
            order_id=order_id,
            order_ids=[order_id],
        )
        entry["events"].append(self._new_event("EXTERNAL_ORDER_TRACKED"))
        self.repository.append_entry(entry, require_mongo=True)
        return entry

    # ---------------------------------------------------------------
    # CANCEL ORDER
    # ---------------------------------------------------------------
    def cancel_order(self, order_id: str) -> dict:
        """Cancel one open/pending order. This does not square off a filled trade."""
        with self._operation_lock:
            entry = self._get_or_create_order_entry(order_id)
            self._append_event(
                entry["local_id"],
                self._new_event("CANCEL_REQUESTED", order_id=order_id),
                status="CANCEL_REQUESTED",
                require_mongo=True,
            )
            try:
                self._require_trading()
                order_v3, _ = self._clients()
                response = order_v3.cancel_order(order_id)
                formatted_resp = _format_success_response(response, default_status=200)
                persistence = self._append_event(
                    entry["local_id"],
                    self._new_event(
                        "ORDER_CANCEL_RESPONSE",
                        order_id=order_id,
                        broker_response=formatted_resp,
                    ),
                    status="CANCEL_SUBMITTED",
                    require_mongo=False,
                )
                self.repository.update_entry(
                    entry["local_id"],
                    {"broker_response": formatted_resp, "last_modified_time": now_iso()},
                    require_mongo=False,
                )
                notify_event(
                    "Order cancellation submitted",
                    [
                        f"Order ID: {order_id}",
                        "Broker cancellation request submitted.",
                    ],
                    "CANCEL",
                )
                return {
                    "status": "cancel_submitted",
                    "order_id": order_id,
                    "broker_response": formatted_resp,
                    "persistence": persistence,
                }
            except TradingDisabledError:
                blocked_msg = _simplify_error_message(
                    "TRADING_DISABLED", "", action="cancel"
                )
                try:
                    self._append_event(
                        entry["local_id"],
                        self._new_event(
                            "TRADING_DISABLED",
                            order_id=order_id,
                            reason="TRADING_ENABLED=false",
                            error=blocked_msg,
                        ),
                        status="BLOCKED",
                        require_mongo=False,
                    )
                except Exception:
                    logger.exception("Could not save blocked-cancel audit")
                notify_event(
                    "Order cancellation blocked — trading disabled",
                    [f"Order ID: {order_id}", f"Reason: {blocked_msg}"],
                    "WARNING",
                )
                raise
            except Exception as exc:
                err_resp = _parse_api_exception(exc, action="cancel")
                summary_err = _exception_text(exc, action="cancel")
                try:
                    self._append_event(
                        entry["local_id"],
                        self._new_event(
                            "CANCEL_EXCEPTION",
                            error=summary_err,
                            broker_error=err_resp,
                        ),
                        status="CANCEL_EXCEPTION",
                        require_mongo=False,
                    )
                    self.repository.update_entry(
                        entry["local_id"],
                        {
                            "error": summary_err,
                            "broker_error": err_resp,
                            "last_modified_time": now_iso(),
                        },
                        require_mongo=False,
                    )
                except Exception:
                    logger.exception("Could not save cancel exception")
                notify_event(
                    "Order cancellation failed",
                    [f"Order ID: {order_id}", f"Error: {summary_err}"],
                    "ERROR",
                )
                raise

    def cancel_orders(self, order_ids: list[str]) -> list[dict]:
        results = []
        for order_id in dict.fromkeys(str(value) for value in order_ids):
            try:
                results.append({"order_id": order_id, **self.cancel_order(order_id)})
            except Exception as exc:
                results.append(
                    {
                        "order_id": order_id,
                        "status": "error",
                        "error": _exception_text(exc, action="cancel"),
                        "broker_error": _parse_api_exception(exc, action="cancel"),
                    }
                )
        return results

    def _order_details(self, order_v2, order_id: str) -> dict:
        response = order_v2.get_order_status(order_id=order_id)
        return _response_data(response)

    # ---------------------------------------------------------------
    # EXIT (SQUARE-OFF) SINGLE ORDER
    # ---------------------------------------------------------------
    def exit_order(self, order_id: str) -> dict:
        """Square off the filled quantity from one order tracked by this app."""
        with self._operation_lock:
            entry = self.repository.find_order(order_id)
            if not entry or entry.get("record_type") != "ORDER":
                raise OrderServiceError(
                    "Order ID is not tracked as an order placed by this service; "
                    "ID-specific exit is refused to avoid an untracked/double square-off."
                )
            self._append_event(
                entry["local_id"],
                self._new_event("EXIT_REQUESTED", order_id=order_id),
                status="EXIT_REQUESTED",
                require_mongo=True,
            )
            try:
                self._require_trading()
                order_v3, order_v2 = self._clients()
                original = self._order_details(order_v2, order_id)
                filled_qty = int(original.get("filled_quantity") or 0)
                if filled_qty <= 0:
                    raise OrderServiceError(
                        f"Order {order_id} has no filled quantity to exit."
                    )

                exit_map = dict(entry.get("exit_orders_by_order_id") or {})
                prior_exit_ids = list(exit_map.get(order_id) or [])
                exited_qty = 0
                pending_exit_ids = []
                for exit_id in prior_exit_ids:
                    exit_detail = self._order_details(order_v2, exit_id)
                    pending_qty = int(exit_detail.get("pending_quantity") or 0)
                    status = str(exit_detail.get("status") or "").lower()
                    if pending_qty > 0 and status not in {
                        "complete",
                        "cancelled",
                        "rejected",
                    }:
                        pending_exit_ids.append(exit_id)
                    exited_qty += int(exit_detail.get("filled_quantity") or 0)

                if pending_exit_ids:
                    raise OrderServiceError(
                        "An earlier exit order is still pending; wait for its final "
                        f"status before submitting another exit: {pending_exit_ids}"
                    )
                remaining_qty = filled_qty - exited_qty
                if remaining_qty <= 0:
                    notify_event(
                        "Position already exited",
                        [
                            f"Original order ID: {order_id}",
                            f"Filled quantity: {filled_qty}",
                            f"Exited quantity: {exited_qty}",
                        ],
                        "INFO",
                    )
                    return {
                        "status": "already_exited",
                        "order_id": order_id,
                        "filled_quantity": filled_qty,
                        "exited_quantity": exited_qty,
                    }

                side = str(
                    entry.get("transaction_type")
                    or original.get("transaction_type")
                    or ""
                ).upper()
                if side not in {"BUY", "SELL"}:
                    raise OrderServiceError(
                        "Original order transaction_type is unavailable."
                    )
                reverse_side = "SELL" if side == "BUY" else "BUY"
                instrument = (
                    entry.get("instrument_token") or original.get("instrument_token")
                )
                product = entry.get("product") or original.get("product") or "I"
                if not instrument:
                    raise OrderServiceError(
                        "Original instrument_token is unavailable."
                    )

                body = upstox_client.PlaceOrderV3Request(
                    quantity=remaining_qty,
                    product=product,
                    validity="DAY",
                    price=0,
                    tag=f"exit-{order_id}"[:40],
                    instrument_token=instrument,
                    order_type="MARKET",
                    transaction_type=reverse_side,
                    disclosed_quantity=0,
                    trigger_price=0.0,
                    is_amo=False,
                    slice=True,
                )
                response = order_v3.place_order(body)
                formatted_resp = _format_success_response(response, default_status=200)
                response_data = (
                    formatted_resp.get("data")
                    if isinstance(formatted_resp.get("data"), dict)
                    else _response_data(response)
                )
                exit_ids = response_data.get("order_ids") or []
                if not exit_ids and response_data.get("order_id"):
                    exit_ids = [response_data["order_id"]]
                exit_ids = [str(value) for value in exit_ids if value]
                if not exit_ids:
                    raise OrderServiceError(
                        "Upstox exit response did not contain order IDs: "
                        f"{response_data}"
                    )

                exit_map.setdefault(order_id, []).extend(exit_ids)
                parent_events = list(entry.get("events") or [])
                parent_events.append(
                    self._new_event(
                        "EXIT_ORDER_PLACED",
                        source_order_id=order_id,
                        exit_order_ids=exit_ids,
                        quantity=remaining_qty,
                        transaction_type=reverse_side,
                        broker_response=formatted_resp,
                    )
                )
                parent_updates = {
                    "exit_orders_by_order_id": exit_map,
                    "broker_response": formatted_resp,
                    "events": parent_events,
                    "last_modified_time": now_iso(),
                }
                persistence = self.repository.update_entry(
                    entry["local_id"], parent_updates, require_mongo=False
                )
                for exit_id in exit_ids:
                    exit_entry = self._new_entry(
                        "EXIT_ORDER",
                        order_id=exit_id,
                        order_ids=[exit_id],
                        parent_order_id=order_id,
                        instrument_token=instrument,
                        quantity=remaining_qty,
                        transaction_type=reverse_side,
                        product=product,
                        order_type="MARKET",
                        status="PLACED",
                        broker_response=formatted_resp,
                    )
                    exit_entry["events"].append(
                        self._new_event(
                            "EXIT_ORDER_PLACED",
                            source_order_id=order_id,
                            broker_response=formatted_resp,
                        )
                    )
                    try:
                        self.repository.append_entry(
                            exit_entry, require_mongo=False
                        )
                    except Exception:
                        logger.exception(
                            "Could not persist exit order audit entry %s", exit_id
                        )
                notify_event(
                    "Exit order submitted",
                    [
                        f"Original order ID: {order_id}",
                        "Exit order ID(s): " + ", ".join(exit_ids),
                        f"Quantity: {remaining_qty} | Side: {reverse_side}",
                        f"Instrument: {instrument}",
                    ],
                    "EXIT",
                )
                return {
                    "status": "exit_submitted",
                    "source_order_id": order_id,
                    "exit_order_ids": exit_ids,
                    "quantity_requested": remaining_qty,
                    "transaction_type": reverse_side,
                    "broker_response": formatted_resp,
                    "persistence": persistence,
                }
            except TradingDisabledError:
                blocked_msg = _simplify_error_message(
                    "TRADING_DISABLED", "", action="exit"
                )
                try:
                    self._append_event(
                        entry["local_id"],
                        self._new_event(
                            "TRADING_DISABLED",
                            order_id=order_id,
                            reason="TRADING_ENABLED=false",
                            error=blocked_msg,
                        ),
                        status="BLOCKED",
                        require_mongo=False,
                    )
                except Exception:
                    logger.exception("Could not save blocked-exit audit")
                notify_event(
                    "Exit request blocked — trading disabled",
                    [f"Order ID: {order_id}", f"Reason: {blocked_msg}"],
                    "WARNING",
                )
                raise
            except Exception as exc:
                err_resp = _parse_api_exception(exc, action="exit")
                summary_err = _exception_text(exc, action="exit")
                try:
                    self._append_event(
                        entry["local_id"],
                        self._new_event(
                            "EXIT_EXCEPTION",
                            order_id=order_id,
                            error=summary_err,
                            broker_error=err_resp,
                        ),
                        status="EXIT_EXCEPTION",
                        require_mongo=False,
                    )
                    self.repository.update_entry(
                        entry["local_id"],
                        {
                            "error": summary_err,
                            "broker_error": err_resp,
                            "last_modified_time": now_iso(),
                        },
                        require_mongo=False,
                    )
                except Exception:
                    logger.exception("Could not save exit exception")
                notify_event(
                    "Exit request failed",
                    [f"Order ID: {order_id}", f"Error: {summary_err}"],
                    "ERROR",
                )
                raise

    def exit_orders(self, order_ids: list[str]) -> list[dict]:
        results = []
        for order_id in dict.fromkeys(str(value) for value in order_ids):
            try:
                results.append(self.exit_order(order_id))
            except Exception as exc:
                results.append(
                    {
                        "order_id": order_id,
                        "status": "error",
                        "error": _exception_text(exc, action="exit"),
                        "broker_error": _parse_api_exception(exc, action="exit"),
                    }
                )
        return results

    # ---------------------------------------------------------------
    # EXIT ALL POSITIONS
    # ---------------------------------------------------------------
    def exit_all_positions(
        self, *, tag: str | None = None, segment: str | None = None
    ) -> dict:
        """Call Upstox's native exit_positions API; no order IDs are needed."""
        entry = self._new_entry(
            "EXIT_ALL",
            action="EXIT_ALL_POSITIONS",
            tag=tag,
            segment=segment,
        )
        entry["events"].append(
            self._new_event("EXIT_ALL_REQUESTED", tag=tag, segment=segment)
        )
        self._persist_before_action(entry, "exit_all_positions")

        try:
            self._require_trading()
            _, order_v2 = self._clients()
            response = order_v2.exit_positions(tag=tag, segment=segment)
            formatted_resp = _format_success_response(response, default_status=200)
            data = (
                formatted_resp.get("data")
                if isinstance(formatted_resp.get("data"), dict)
                else _response_data(response)
            )
            ids = data.get("order_ids") or []
            ids = [str(value) for value in ids if value]
            persistence = self._append_event(
                entry["local_id"],
                self._new_event(
                    "EXIT_ALL_RESPONSE",
                    order_ids=ids,
                    broker_response=formatted_resp,
                ),
                status="EXIT_ALL_SUBMITTED",
                require_mongo=False,
            )
            self.repository.update_entry(
                entry["local_id"],
                {"broker_response": formatted_resp, "last_modified_time": now_iso()},
                require_mongo=False,
            )
            for exit_id in ids:
                audit = self._new_entry(
                    "EXIT_ALL_ORDER",
                    order_id=exit_id,
                    order_ids=[exit_id],
                    parent_action_id=entry["local_id"],
                    status="SUBMITTED",
                    broker_response=formatted_resp,
                )
                audit["events"].append(
                    self._new_event(
                        "EXIT_ALL_ORDER_CREATED",
                        parent_action_id=entry["local_id"],
                        broker_response=formatted_resp,
                    )
                )
                try:
                    self.repository.append_entry(audit, require_mongo=False)
                except Exception:
                    logger.exception(
                        "Could not persist exit-all order %s", exit_id
                    )
            notify_event(
                "Exit all positions submitted",
                [
                    "Exit order ID(s): "
                    + (", ".join(ids) if ids else "Not returned"),
                    f"Tag: {tag or 'All'} | Segment: {segment or 'All'}",
                    "Broker-native exit_positions request submitted.",
                ],
                "EXIT",
            )
            return {
                "status": "exit_all_submitted",
                "order_ids": ids,
                "broker_response": formatted_resp,
                "persistence": persistence,
            }
        except TradingDisabledError:
            blocked_msg = _simplify_error_message(
                "TRADING_DISABLED", "", action="exit"
            )
            try:
                self._append_event(
                    entry["local_id"],
                    self._new_event(
                        "TRADING_DISABLED",
                        reason="TRADING_ENABLED=false",
                        error=blocked_msg,
                    ),
                    status="BLOCKED",
                    require_mongo=False,
                )
            except Exception:
                logger.exception("Could not save blocked exit-all audit")
            notify_event(
                "Exit all positions blocked — trading disabled",
                [f"Reason: {blocked_msg}"],
                "WARNING",
            )
            raise
        except Exception as exc:
            err_resp = _parse_api_exception(exc, action="exit")
            summary_err = _exception_text(exc, action="exit")
            try:
                self._append_event(
                    entry["local_id"],
                    self._new_event(
                        "EXIT_ALL_EXCEPTION",
                        error=summary_err,
                        broker_error=err_resp,
                    ),
                    status="EXIT_ALL_EXCEPTION",
                    require_mongo=False,
                )
                self.repository.update_entry(
                    entry["local_id"],
                    {
                        "error": summary_err,
                        "broker_error": err_resp,
                        "last_modified_time": now_iso(),
                    },
                    require_mongo=False,
                )
            except Exception:
                logger.exception("Could not save exit-all exception")
            notify_event(
                "Exit all positions failed",
                [f"Error: {summary_err}"],
                "ERROR",
            )
            raise