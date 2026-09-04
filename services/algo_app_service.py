import time
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from core import config
from core.logger import get_logger
from services.telegram_service import telegram_service

logger = get_logger(__file__)

# Legacy standalone Algo App URLs that are now replaced by the integrated endpoint
LEGACY_LOCAL_ALGO_APP_URLS = {
    "http://127.0.0.1:9000/local-algo/ema-alert",
    "http://localhost:9000/local-algo/ema-alert",
}

def _resolve_algo_app_url(configured_url: str) -> str:
    """
    Resolves the effective Algo App URL.
    If the configured URL is empty or matches a legacy standalone URL,
    the integrated local Algo App endpoint (from config.LOCAL_ALGO_APP_URL) is used.
    """
    url = str(configured_url or "").strip().rstrip("/")

    # If no URL is configured, use the integrated local Algo App endpoint
    if not url:
        integrated_url = getattr(config, "LOCAL_ALGO_APP_URL", None)
        if not integrated_url:
            # Fallback to constructing from base URL if not set
            base_url = getattr(config, "LOCAL_ALERT_APP_BASE_URL", "http://127.0.0.1:8000")
            integrated_url = f"{base_url}/local-algo/ema-alert"
        logger.info("ALGO_APP_URL is empty. Using integrated local Algo App endpoint: %s", integrated_url)
        return integrated_url

    # If the URL is a legacy standalone one, redirect to the integrated endpoint
    if url in LEGACY_LOCAL_ALGO_APP_URLS:
        integrated_url = getattr(config, "LOCAL_ALGO_APP_URL", None)
        if not integrated_url:
            base_url = getattr(config, "LOCAL_ALERT_APP_BASE_URL", "http://127.0.0.1:8000")
            integrated_url = f"{base_url}/local-algo/ema-alert"
        logger.info(
            "Legacy standalone Algo App URL detected: %s. Using integrated endpoint: %s",
            url,
            integrated_url,
        )
        return integrated_url

    # Otherwise, use the URL as configured
    return url


class AlgoAppService:
    def __init__(self):
        self.enabled = bool(getattr(config, "ALGO_APP_ENABLED", False))
        configured_url = str(getattr(config, "ALGO_APP_URL", "") or "").strip()
        self.url = _resolve_algo_app_url(configured_url)
        self.auth_type = str(getattr(config, "ALGO_APP_AUTH_TYPE", "none") or "none").strip().lower()
        self.auth_token = str(getattr(config, "ALGO_APP_AUTH_TOKEN", "") or "").strip()
        self.api_key = str(getattr(config, "ALGO_APP_API_KEY", "") or "").strip()
        self.api_key_header = str(getattr(config, "ALGO_APP_API_KEY_HEADER", "X-API-Key") or "X-API-Key").strip()
        self.timeout_seconds = max(0.1, float(getattr(config, "ALGO_APP_TIMEOUT_SECONDS", 10.0)))
        self.verify_ssl = bool(getattr(config, "ALGO_APP_VERIFY_SSL", True))
        self.max_retries = max(0, int(getattr(config, "ALGO_APP_MAX_RETRIES", 3)))
        self.retry_delay_seconds = max(0.0, float(getattr(config, "ALGO_APP_RETRY_DELAY_SECONDS", 2.0)))
        self.send_in_background = bool(getattr(config, "ALGO_APP_SEND_IN_BACKGROUND", True))
        self.background_queue_counts_as_accepted = bool(getattr(config, "ALGO_APP_BACKGROUND_QUEUE_COUNTS_AS_ACCEPTED", True))
        self.max_response_body_length = max(0, int(getattr(config, "ALGO_APP_MAX_RESPONSE_BODY_LENGTH", 2000)))
        self.max_workers = max(1, int(getattr(config, "ALGO_APP_BACKGROUND_MAX_WORKERS", 2)))
        self.telegram_success_notification_enabled = bool(getattr(config, "ALGO_TELE_APP", False))
        self.market_timezone = self._load_market_timezone()
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="algo-app-delivery")
        self._futures: set[Future] = set()
        self.dispatch_count = 0
        self.background_dispatch_count = 0
        self.delivery_attempt_count = 0
        self.delivery_success_count = 0
        self.delivery_failed_count = 0
        self.retry_count = 0
        self.pending_count = 0
        self.telegram_notification_attempt_count = 0
        self.telegram_notification_success_count = 0
        self.telegram_notification_failed_count = 0
        self.last_dispatch_at = None
        self.last_success_at = None
        self.last_failure_at = None
        self.last_event_id = None
        self.last_status_code = None
        self.last_error = None
        self.last_response = None
        self.last_delivery_result = None
        self.last_telegram_notification_at = None
        self.last_telegram_notification_event_id = None
        self.last_telegram_notification_success = None
        self.last_telegram_notification_error = None

    def _load_market_timezone(self) -> ZoneInfo:
        timezone_name = str(getattr(config, "MARKET_TIMEZONE", "Asia/Kolkata") or "Asia/Kolkata").strip()
        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            logger.warning("Invalid market timezone configured. timezone=%s, fallback=Asia/Kolkata", timezone_name)
            return ZoneInfo("Asia/Kolkata")
        except Exception as ex:
            logger.error("Failed loading market timezone. timezone=%s, error=%s: %s", timezone_name, type(ex).__name__, ex)
            return ZoneInfo("Asia/Kolkata")

    def _now_market_time(self) -> str:
        try:
            return datetime.now(self.market_timezone).isoformat()
        except Exception as ex:
            logger.error("Failed generating market timestamp. error=%s: %s", type(ex).__name__, ex)
            return datetime.now().isoformat()

    def is_configured(self) -> bool:
        return bool(self.enabled and self.url and self._is_authentication_configured())

    def _is_authentication_configured(self) -> bool:
        if self.auth_type == "none":
            return True
        if self.auth_type == "bearer":
            return bool(self.auth_token)
        if self.auth_type == "api_key":
            return bool(self.api_key and self.api_key_header)
        return False

    def _build_headers(self, payload: dict) -> dict:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "option-feed-engine/1.0",
        }
        if self.auth_type == "bearer" and self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        elif self.auth_type == "api_key" and self.api_key and self.api_key_header:
            headers[self.api_key_header] = self.api_key
        event_id = payload.get("event_id")
        if event_id:
            headers["X-Event-ID"] = str(event_id)
            headers["Idempotency-Key"] = str(event_id)
        schema_version = payload.get("schema_version")
        if schema_version is not None:
            headers["X-Payload-Schema-Version"] = str(schema_version)
        event_type = payload.get("event_type")
        if event_type:
            headers["X-Event-Type"] = str(event_type)
        return headers

    def _truncate_text(self, value: Any) -> str:
        if value is None or self.max_response_body_length <= 0:
            return ""
        return str(value)[:self.max_response_body_length]

    def _get_response_body(self, response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            try:
                return self._truncate_text(response.text)
            except Exception as ex:
                logger.warning("Failed reading Algo App response body. status_code=%s, error=%s: %s", response.status_code, type(ex).__name__, ex)
                return None
        except Exception as ex:
            logger.warning("Unexpected Algo App response parsing error. status_code=%s, error=%s: %s", response.status_code, type(ex).__name__, ex)
            return None

    def _is_retryable_status(self, status_code: int) -> bool:
        return status_code in {408, 425, 429} or status_code >= 500

    def _calculate_retry_delay(self, attempt_number: int, response: requests.Response | None = None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(0.0, float(retry_after))
                except (TypeError, ValueError, OverflowError):
                    logger.warning("Invalid Retry-After header from Algo App. value=%s", retry_after)
        return self.retry_delay_seconds * max(1, attempt_number)

    def _validate_payload(self, payload: Any) -> tuple[bool, str | None]:
        if not isinstance(payload, dict):
            return False, "Payload must be a JSON object."
        if not payload:
            return False, "Payload must not be empty."
        event_type = str(payload.get("event_type") or "").strip()
        if not event_type:
            return False, "Payload event_type is required."
        instrument = payload.get("instrument")
        if not isinstance(instrument, dict):
            return False, "Payload instrument must be an object."
        instrument_key = str(instrument.get("instrument_key") or "").strip()
        if not instrument_key:
            return False, "Payload instrument.instrument_key is required."
        order_suggestion = payload.get("order_suggestion")
        if order_suggestion is not None and not isinstance(order_suggestion, dict):
            return False, "Payload order_suggestion must be an object."
        if isinstance(order_suggestion, dict):
            nearest_instruments = order_suggestion.get("nearest_instruments", [])
            if not isinstance(nearest_instruments, list):
                return False, "order_suggestion.nearest_instruments must be a list."
            budget_filter = order_suggestion.get("budget_filter")
            if budget_filter is not None and not isinstance(budget_filter, dict):
                return False, "order_suggestion.budget_filter must be an object."
            if isinstance(budget_filter, dict):
                budget_instruments = budget_filter.get("instruments", [])
                if not isinstance(budget_instruments, list):
                    return False, "order_suggestion.budget_filter.instruments must be a list."
        return True, None

    def _record_dispatch(self, event_id: str | None, background: bool) -> None:
        now = self._now_market_time()
        with self._lock:
            self.dispatch_count += 1
            self.last_dispatch_at = now
            self.last_event_id = event_id
            if background:
                self.background_dispatch_count += 1
                self.pending_count += 1

    def _record_attempt(self) -> None:
        with self._lock:
            self.delivery_attempt_count += 1

    def _record_retry(self) -> None:
        with self._lock:
            self.retry_count += 1

    def _record_delivery_result(self, result: dict) -> None:
        success = bool(result.get("success"))
        now = self._now_market_time()
        with self._lock:
            self.last_delivery_result = deepcopy(result)
            self.last_event_id = result.get("event_id")
            self.last_status_code = result.get("status_code")
            self.last_error = result.get("error")
            self.last_response = deepcopy(result.get("response"))
            if success:
                self.delivery_success_count += 1
                self.last_success_at = now
            else:
                self.delivery_failed_count += 1
                self.last_failure_at = now

    def _record_telegram_notification_result(self, event_id: str | None, success: bool, error: str | None = None) -> None:
        now = self._now_market_time()
        with self._lock:
            self.telegram_notification_attempt_count += 1
            self.last_telegram_notification_at = now
            self.last_telegram_notification_event_id = event_id
            self.last_telegram_notification_success = success
            self.last_telegram_notification_error = error
            if success:
                self.telegram_notification_success_count += 1
            else:
                self.telegram_notification_failed_count += 1

    def _decrement_pending_count(self) -> None:
        with self._lock:
            self.pending_count = max(0, self.pending_count - 1)

    def _send_algo_app_success_telegram(self, event_id: str | None) -> bool:
        if not self.telegram_success_notification_enabled:
            return False
        try:
            success = bool(telegram_service.send_message(title="Algo App Payload Delivery", message="Payload sent successfully to Algo App.", level="ALGO"))
            if success:
                self._record_telegram_notification_result(event_id=event_id, success=True)
                logger.info("Algo App delivery confirmation sent to Telegram. event_id=%s", event_id)
                return True
            error = "Telegram service returned an unsuccessful result."
            self._record_telegram_notification_result(event_id=event_id, success=False, error=error)
            logger.warning("Algo App delivery confirmation was not sent to Telegram. event_id=%s, error=%s", event_id, error)
            return False
        except Exception as ex:
            error = f"{type(ex).__name__}: {ex}"
            self._record_telegram_notification_result(event_id=event_id, success=False, error=error)
            logger.error("Algo App delivery confirmation Telegram exception. event_id=%s, error=%s: %s", event_id, type(ex).__name__, ex)
            return False

    def _update_opening_range_delivery_state(self, event_id: str | None, delivery_result: dict) -> None:
        try:
            from services.opening_range import state
            state.update_last_algo_app_delivery(event_id=event_id, delivery_result=delivery_result)
        except Exception as ex:
            logger.warning("Could not update Algo App delivery result in Opening Range state. event_id=%s, error=%s: %s", event_id, type(ex).__name__, ex)

    def _build_not_configured_result(self, event_id: str | None, started_at: str) -> dict:
        if not self.enabled:
            error = "Algo App delivery is disabled."
        elif not self.url:
            error = "ALGO_APP_URL is empty."
        elif not self._is_authentication_configured():
            error = "Algo App authentication configuration is invalid."
        else:
            error = "Algo App delivery is not configured."
        return {"enabled": self.enabled, "configured": False, "attempted": False, "success": False, "event_id": event_id, "status_code": None, "attempt_count": 0, "response": None, "error": error, "started_at": started_at, "completed_at": self._now_market_time()}

    def _send_payload(self, payload: dict) -> dict:
        started_at = self._now_market_time()
        event_id = str(payload.get("event_id") or "").strip() or None
        valid_payload, validation_error = self._validate_payload(payload)
        if not valid_payload:
            result = {"enabled": self.enabled, "configured": self.is_configured(), "attempted": False, "success": False, "event_id": event_id, "status_code": None, "attempt_count": 0, "response": None, "error": validation_error, "started_at": started_at, "completed_at": self._now_market_time()}
            self._record_delivery_result(result)
            logger.warning("Algo App payload validation failed. event_id=%s, error=%s", event_id, validation_error)
            return result
        if not self.is_configured():
            result = self._build_not_configured_result(event_id=event_id, started_at=started_at)
            self._record_delivery_result(result)
            logger.warning("Algo App payload delivery skipped. event_id=%s, error=%s", event_id, result.get("error"))
            return result
        try:
            headers = self._build_headers(payload)
        except Exception as ex:
            result = {"enabled": True, "configured": True, "attempted": False, "success": False, "event_id": event_id, "status_code": None, "attempt_count": 0, "response": None, "error": f"HeaderBuildError: {type(ex).__name__}: {ex}", "started_at": started_at, "completed_at": self._now_market_time()}
            self._record_delivery_result(result)
            logger.error("Failed building Algo App request headers. event_id=%s, error=%s: %s", event_id, type(ex).__name__, ex)
            return result
        total_attempts = max(1, self.max_retries + 1)
        last_status_code = None
        last_response = None
        last_error = None
        completed_attempts = 0
        for attempt_number in range(1, total_attempts + 1):
            completed_attempts = attempt_number
            self._record_attempt()
            try:
                logger.info("Sending Algo App EMA payload. event_id=%s, attempt=%s, maximum_attempts=%s", event_id, attempt_number, total_attempts)
                response = requests.post(self.url, json=payload, headers=headers, timeout=self.timeout_seconds, verify=self.verify_ssl)
                last_status_code = response.status_code
                last_response = self._get_response_body(response)
                if 200 <= response.status_code < 300:
                    telegram_notification_sent = self._send_algo_app_success_telegram(event_id=event_id)
                    result = {"enabled": True, "configured": True, "attempted": True, "success": True, "event_id": event_id, "status_code": response.status_code, "attempt_count": attempt_number, "response": last_response, "error": None, "telegram_notification": {"enabled": self.telegram_success_notification_enabled, "attempted": self.telegram_success_notification_enabled, "success": telegram_notification_sent}, "started_at": started_at, "completed_at": self._now_market_time()}
                    self._record_delivery_result(result)
                    logger.info("Algo App EMA payload delivered. event_id=%s, status_code=%s, attempt=%s, telegram_notification=%s", event_id, response.status_code, attempt_number, telegram_notification_sent)
                    return result
                last_error = f"Algo App returned HTTP {response.status_code}."
                logger.warning("Algo App returned unsuccessful HTTP response. event_id=%s, status_code=%s, attempt=%s", event_id, response.status_code, attempt_number)
                if not self._is_retryable_status(response.status_code) or attempt_number >= total_attempts:
                    break
                retry_delay = self._calculate_retry_delay(attempt_number, response)
                self._record_retry()
                logger.warning("Retrying Algo App delivery. event_id=%s, status_code=%s, attempt=%s, retry_delay=%s", event_id, response.status_code, attempt_number, retry_delay)
                if retry_delay > 0:
                    time.sleep(retry_delay)
            except requests.Timeout as ex:
                last_error = f"{type(ex).__name__}: {ex}"
                logger.warning("Algo App request timed out. event_id=%s, attempt=%s, error=%s", event_id, attempt_number, last_error)
                if attempt_number >= total_attempts:
                    break
                retry_delay = self._calculate_retry_delay(attempt_number)
                self._record_retry()
                if retry_delay > 0:
                    time.sleep(retry_delay)
            except requests.ConnectionError as ex:
                last_error = f"{type(ex).__name__}: {ex}"
                logger.warning("Algo App connection failed. event_id=%s, attempt=%s, error=%s", event_id, attempt_number, last_error)
                if attempt_number >= total_attempts:
                    break
                retry_delay = self._calculate_retry_delay(attempt_number)
                self._record_retry()
                if retry_delay > 0:
                    time.sleep(retry_delay)
            except requests.RequestException as ex:
                last_error = f"{type(ex).__name__}: {ex}"
                logger.warning("Algo App request exception. event_id=%s, attempt=%s, error=%s", event_id, attempt_number, last_error)
                if attempt_number >= total_attempts:
                    break
                retry_delay = self._calculate_retry_delay(attempt_number)
                self._record_retry()
                if retry_delay > 0:
                    time.sleep(retry_delay)
            except Exception as ex:
                last_error = f"{type(ex).__name__}: {ex}"
                logger.exception("Unexpected Algo App delivery exception. event_id=%s, attempt=%s", event_id, attempt_number)
                break
        result = {"enabled": True, "configured": True, "attempted": True, "success": False, "event_id": event_id, "status_code": last_status_code, "attempt_count": completed_attempts, "response": last_response, "error": last_error or "Algo App delivery failed.", "telegram_notification": {"enabled": self.telegram_success_notification_enabled, "attempted": False, "success": False}, "started_at": started_at, "completed_at": self._now_market_time()}
        self._record_delivery_result(result)
        logger.error("Algo App EMA payload delivery failed. event_id=%s, status_code=%s, attempt_count=%s, error=%s", event_id, last_status_code, completed_attempts, result.get("error"))
        return result

    def _background_delivery(self, payload: dict) -> dict:
        event_id = str(payload.get("event_id") or "").strip() or None
        try:
            result = self._send_payload(payload)
            self._update_opening_range_delivery_state(event_id=event_id, delivery_result=result)
            if not bool(result.get("success")):
                logger.error("Background Algo App delivery was not successful. event_id=%s, status_code=%s, attempt_count=%s, error=%s", event_id, result.get("status_code"), result.get("attempt_count"), result.get("error"))
            return result
        except Exception as ex:
            logger.exception("Unhandled background Algo App delivery exception. event_id=%s, error=%s: %s", event_id, type(ex).__name__, ex)
            result = {"enabled": self.enabled, "configured": self.is_configured(), "attempted": False, "success": False, "event_id": event_id, "status_code": None, "attempt_count": 0, "response": None, "error": f"{type(ex).__name__}: {ex}", "started_at": self._now_market_time(), "completed_at": self._now_market_time()}
            self._record_delivery_result(result)
            self._update_opening_range_delivery_state(event_id=event_id, delivery_result=result)
            return result
        finally:
            self._decrement_pending_count()

    def _background_done(self, future: Future) -> None:
        with self._lock:
            self._futures.discard(future)
        try:
            result = future.result()
            if not isinstance(result, dict):
                logger.error("Algo App background delivery returned an invalid result.")
        except Exception as ex:
            logger.exception("Algo App background delivery future failed. error=%s: %s", type(ex).__name__, ex)

    def dispatch_ema_alert(self, payload: dict) -> bool:
        try:
            valid_payload, validation_error = self._validate_payload(payload)
            if not valid_payload:
                logger.warning("Algo App dispatch skipped because payload is invalid. error=%s", validation_error)
                return False
            if not self.enabled:
                logger.info("Algo App dispatch skipped because ALGO_APP_ENABLED=False.")
                return False
            if not self.url:
                logger.warning("Algo App dispatch skipped because ALGO_APP_URL is empty.")
                return False
            if not self._is_authentication_configured():
                logger.warning("Algo App dispatch skipped because authentication is not configured. auth_type=%s", self.auth_type)
                return False
            payload_copy = deepcopy(payload)
            event_id = str(payload_copy.get("event_id") or "").strip() or None
            if self.send_in_background:
                dispatch_recorded = False
                try:
                    self._record_dispatch(event_id=event_id, background=True)
                    dispatch_recorded = True
                    future = self._executor.submit(self._background_delivery, payload_copy)
                    with self._lock:
                        self._futures.add(future)
                    future.add_done_callback(self._background_done)
                    logger.info("Algo App EMA payload queued. event_id=%s", event_id)
                    return self.background_queue_counts_as_accepted
                except Exception as ex:
                    if dispatch_recorded:
                        self._decrement_pending_count()
                    logger.exception("Algo App background dispatch failed. event_id=%s, error=%s: %s", event_id, type(ex).__name__, ex)
                    return False
            self._record_dispatch(event_id=event_id, background=False)
            result = self._send_payload(payload_copy)
            self._update_opening_range_delivery_state(event_id=event_id, delivery_result=result)
            return bool(result.get("success"))
        except Exception as ex:
            logger.exception("Unexpected Algo App dispatch exception. error=%s: %s", type(ex).__name__, ex)
            return False

    def send_ema_alert(self, payload: dict) -> dict:
        started_at = self._now_market_time()
        try:
            valid_payload, validation_error = self._validate_payload(payload)
            if not valid_payload:
                logger.warning("Synchronous Algo App delivery skipped because payload is invalid. error=%s", validation_error)
                return {"enabled": self.enabled, "configured": self.is_configured(), "attempted": False, "success": False, "event_id": None, "status_code": None, "attempt_count": 0, "response": None, "error": validation_error, "started_at": started_at, "completed_at": self._now_market_time()}
            payload_copy = deepcopy(payload)
            event_id = str(payload_copy.get("event_id") or "").strip() or None
            self._record_dispatch(event_id=event_id, background=False)
            result = self._send_payload(payload_copy)
            self._update_opening_range_delivery_state(event_id=event_id, delivery_result=result)
            return result
        except Exception as ex:
            event_id = None
            if isinstance(payload, dict):
                event_id = str(payload.get("event_id") or "").strip() or None
            logger.exception("Unexpected synchronous Algo App delivery exception. event_id=%s, error=%s: %s", event_id, type(ex).__name__, ex)
            result = {"enabled": self.enabled, "configured": self.is_configured(), "attempted": False, "success": False, "event_id": event_id, "status_code": None, "attempt_count": 0, "response": None, "error": f"{type(ex).__name__}: {ex}", "started_at": started_at, "completed_at": self._now_market_time()}
            self._record_delivery_result(result)
            return result

    def get_status(self) -> dict:
        try:
            with self._lock:
                return {"enabled": self.enabled, "configured": self.is_configured(), "url_configured": bool(self.url), "auth_type": self.auth_type, "authentication_configured": self._is_authentication_configured(), "timeout_seconds": self.timeout_seconds, "verify_ssl": self.verify_ssl, "max_retries": self.max_retries, "retry_delay_seconds": self.retry_delay_seconds, "send_in_background": self.send_in_background, "background_queue_counts_as_accepted": self.background_queue_counts_as_accepted, "background_max_workers": self.max_workers, "telegram_success_notification_enabled": self.telegram_success_notification_enabled, "dispatch_count": self.dispatch_count, "background_dispatch_count": self.background_dispatch_count, "delivery_attempt_count": self.delivery_attempt_count, "delivery_success_count": self.delivery_success_count, "delivery_failed_count": self.delivery_failed_count, "retry_count": self.retry_count, "pending_count": self.pending_count, "telegram_notification_attempt_count": self.telegram_notification_attempt_count, "telegram_notification_success_count": self.telegram_notification_success_count, "telegram_notification_failed_count": self.telegram_notification_failed_count, "last_dispatch_at": self.last_dispatch_at, "last_success_at": self.last_success_at, "last_failure_at": self.last_failure_at, "last_event_id": self.last_event_id, "last_status_code": self.last_status_code, "last_error": self.last_error, "last_response": deepcopy(self.last_response), "last_delivery_result": deepcopy(self.last_delivery_result), "last_telegram_notification_at": self.last_telegram_notification_at, "last_telegram_notification_event_id": self.last_telegram_notification_event_id, "last_telegram_notification_success": self.last_telegram_notification_success, "last_telegram_notification_error": self.last_telegram_notification_error, "market_time": self._now_market_time()}
        except Exception as ex:
            logger.exception("Failed retrieving Algo App service status. error=%s: %s", type(ex).__name__, ex)
            return {"enabled": self.enabled, "configured": False, "error": f"{type(ex).__name__}: {ex}", "market_time": self._now_market_time()}

    def shutdown(self, wait: bool = False) -> None:
        try:
            self._executor.shutdown(wait=wait, cancel_futures=not wait)
            logger.info("Algo App background executor shutdown completed.")
        except Exception as ex:
            logger.exception("Algo App executor shutdown failed. error=%s: %s", type(ex).__name__, ex)

algo_app_service = AlgoAppService()

__all__ = ["AlgoAppService", "algo_app_service"]