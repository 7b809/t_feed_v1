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

logger = get_logger(__file__)


class AlgoAppService:
    def __init__(self):
        self.enabled = bool(
            getattr(
                config,
                "ALGO_APP_ENABLED",
                False,
            )
        )

        self.url = str(
            getattr(
                config,
                "ALGO_APP_URL",
                "",
            )
            or ""
        ).strip()

        self.auth_type = (
            str(
                getattr(
                    config,
                    "ALGO_APP_AUTH_TYPE",
                    "none",
                )
                or "none"
            )
            .strip()
            .lower()
        )

        self.auth_token = str(
            getattr(
                config,
                "ALGO_APP_AUTH_TOKEN",
                "",
            )
            or ""
        ).strip()

        self.api_key = str(
            getattr(
                config,
                "ALGO_APP_API_KEY",
                "",
            )
            or ""
        ).strip()

        self.api_key_header = str(
            getattr(
                config,
                "ALGO_APP_API_KEY_HEADER",
                "X-API-Key",
            )
            or "X-API-Key"
        ).strip()

        self.timeout_seconds = float(
            getattr(
                config,
                "ALGO_APP_TIMEOUT_SECONDS",
                10.0,
            )
        )

        self.verify_ssl = bool(
            getattr(
                config,
                "ALGO_APP_VERIFY_SSL",
                True,
            )
        )

        self.max_retries = int(
            getattr(
                config,
                "ALGO_APP_MAX_RETRIES",
                3,
            )
        )

        self.retry_delay_seconds = float(
            getattr(
                config,
                "ALGO_APP_RETRY_DELAY_SECONDS",
                2.0,
            )
        )

        self.send_in_background = bool(
            getattr(
                config,
                "ALGO_APP_SEND_IN_BACKGROUND",
                True,
            )
        )

        self.max_response_body_length = int(
            getattr(
                config,
                "ALGO_APP_MAX_RESPONSE_BODY_LENGTH",
                2000,
            )
        )

        self.market_timezone = self._load_market_timezone()

        self._lock = Lock()

        self._executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="algo-app-delivery",
        )

        self._futures: set[Future] = set()

        self.dispatch_count = 0
        self.background_dispatch_count = 0
        self.delivery_attempt_count = 0
        self.delivery_success_count = 0
        self.delivery_failed_count = 0
        self.retry_count = 0
        self.pending_count = 0

        self.last_dispatch_at = None
        self.last_success_at = None
        self.last_failure_at = None
        self.last_event_id = None
        self.last_status_code = None
        self.last_error = None
        self.last_response = None
        self.last_delivery_result = None

    # ============================================================
    # Time Helpers
    # ============================================================

    def _load_market_timezone(self) -> ZoneInfo:
        timezone_name = str(
            getattr(
                config,
                "MARKET_TIMEZONE",
                "Asia/Kolkata",
            )
            or "Asia/Kolkata"
        ).strip()

        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            return ZoneInfo("Asia/Kolkata")

    def _now_market_time(self) -> str:
        return datetime.now(self.market_timezone).isoformat()

    # ============================================================
    # Configuration
    # ============================================================

    def is_configured(self) -> bool:
        return bool(self.enabled and self.url)

    def _build_headers(
        self,
        payload: dict,
    ) -> dict:
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

        if schema_version:
            headers["X-Payload-Schema-Version"] = str(schema_version)

        return headers

    # ============================================================
    # Value Helpers
    # ============================================================

    def _truncate_text(
        self,
        value: Any,
    ) -> str:
        if value is None:
            return ""

        text = str(value)

        if self.max_response_body_length <= 0:
            return ""

        return text[: self.max_response_body_length]

    def _get_response_body(
        self,
        response: requests.Response,
    ) -> Any:
        try:
            body = response.json()
        except ValueError:
            body = self._truncate_text(response.text)

        return body

    def _is_retryable_status(
        self,
        status_code: int,
    ) -> bool:
        return (
            status_code
            in {
                408,
                425,
                429,
            }
            or status_code >= 500
        )

    def _calculate_retry_delay(
        self,
        attempt_number: int,
        response: requests.Response | None = None,
    ) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")

            if retry_after:
                try:
                    return max(
                        0.0,
                        float(retry_after),
                    )
                except (
                    TypeError,
                    ValueError,
                    OverflowError,
                ):
                    pass

        base_delay = max(
            0.0,
            self.retry_delay_seconds,
        )

        return base_delay * max(
            1,
            attempt_number,
        )

    # ============================================================
    # Runtime State
    # ============================================================

    def _record_dispatch(
        self,
        event_id: str | None,
        background: bool,
    ) -> None:
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

    def _record_delivery_result(
        self,
        result: dict,
    ) -> None:
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

    def _decrement_pending_count(self) -> None:
        with self._lock:
            self.pending_count = max(
                0,
                self.pending_count - 1,
            )

    # ============================================================
    # Opening Range State Update
    # ============================================================

    def _update_opening_range_delivery_state(
        self,
        event_id: str | None,
        delivery_result: dict,
    ) -> None:
        try:
            from services.opening_range import state

            state.update_last_algo_app_delivery(
                event_id=event_id,
                delivery_result=delivery_result,
            )
        except Exception as ex:
            logger.warning(
                "Could not update Algo App delivery "
                "result in Opening Range state. "
                "event_id=%s, error=%s: %s",
                event_id,
                type(ex).__name__,
                ex,
            )

    # ============================================================
    # HTTP Delivery
    # ============================================================

    def _send_payload(
        self,
        payload: dict,
    ) -> dict:
        started_at = self._now_market_time()

        event_id = str(payload.get("event_id") or "").strip() or None

        if not self.is_configured():
            result = {
                "enabled": self.enabled,
                "configured": False,
                "attempted": False,
                "success": False,
                "event_id": event_id,
                "status_code": None,
                "attempt_count": 0,
                "response": None,
                "error": ("Algo App delivery is disabled " "or ALGO_APP_URL is empty."),
                "started_at": started_at,
                "completed_at": (self._now_market_time()),
            }

            self._record_delivery_result(result)

            return result

        headers = self._build_headers(payload)

        total_attempts = max(
            1,
            self.max_retries + 1,
        )

        last_status_code = None
        last_response = None
        last_error = None
        completed_attempts = 0

        for attempt_number in range(
            1,
            total_attempts + 1,
        ):
            completed_attempts = attempt_number

            self._record_attempt()

            try:
                logger.info(
                    "Sending Algo App EMA payload. "
                    "event_id=%s, attempt=%s, "
                    "maximum_attempts=%s",
                    event_id,
                    attempt_number,
                    total_attempts,
                )

                response = requests.post(
                    self.url,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout_seconds,
                    verify=self.verify_ssl,
                )

                last_status_code = response.status_code

                last_response = self._get_response_body(response)

                if 200 <= response.status_code < 300:
                    result = {
                        "enabled": True,
                        "configured": True,
                        "attempted": True,
                        "success": True,
                        "event_id": event_id,
                        "status_code": (response.status_code),
                        "attempt_count": (attempt_number),
                        "response": last_response,
                        "error": None,
                        "started_at": started_at,
                        "completed_at": (self._now_market_time()),
                    }

                    self._record_delivery_result(result)

                    logger.info(
                        "Algo App EMA payload delivered. "
                        "event_id=%s, status_code=%s, "
                        "attempt=%s",
                        event_id,
                        response.status_code,
                        attempt_number,
                    )

                    return result

                last_error = "Algo App returned HTTP " f"{response.status_code}."

                if (
                    not self._is_retryable_status(response.status_code)
                    or attempt_number >= total_attempts
                ):
                    break

                retry_delay = self._calculate_retry_delay(
                    attempt_number,
                    response,
                )

                self._record_retry()

                logger.warning(
                    "Retrying Algo App delivery. "
                    "event_id=%s, status_code=%s, "
                    "attempt=%s, retry_delay=%s",
                    event_id,
                    response.status_code,
                    attempt_number,
                    retry_delay,
                )

                if retry_delay > 0:
                    time.sleep(retry_delay)

            except requests.RequestException as ex:
                last_error = f"{type(ex).__name__}: {ex}"

                if attempt_number >= total_attempts:
                    break

                retry_delay = self._calculate_retry_delay(attempt_number)

                self._record_retry()

                logger.warning(
                    "Algo App request exception. "
                    "event_id=%s, attempt=%s, "
                    "retry_delay=%s, error=%s",
                    event_id,
                    attempt_number,
                    retry_delay,
                    last_error,
                )

                if retry_delay > 0:
                    time.sleep(retry_delay)

            except Exception as ex:
                last_error = f"{type(ex).__name__}: {ex}"

                logger.error(
                    "Unexpected Algo App delivery "
                    "exception. event_id=%s, "
                    "attempt=%s, error=%s",
                    event_id,
                    attempt_number,
                    last_error,
                )

                break

        result = {
            "enabled": True,
            "configured": True,
            "attempted": True,
            "success": False,
            "event_id": event_id,
            "status_code": last_status_code,
            "attempt_count": completed_attempts,
            "response": last_response,
            "error": (last_error or "Algo App delivery failed."),
            "started_at": started_at,
            "completed_at": (self._now_market_time()),
        }

        self._record_delivery_result(result)

        logger.error(
            "Algo App EMA payload delivery failed. "
            "event_id=%s, status_code=%s, "
            "attempt_count=%s, error=%s",
            event_id,
            last_status_code,
            completed_attempts,
            result.get("error"),
        )

        return result

    # ============================================================
    # Background Delivery
    # ============================================================

    def _background_delivery(
        self,
        payload: dict,
    ) -> dict:
        event_id = str(payload.get("event_id") or "").strip() or None

        try:
            result = self._send_payload(payload)

            self._update_opening_range_delivery_state(
                event_id=event_id,
                delivery_result=result,
            )

            return result

        finally:
            self._decrement_pending_count()

    def _background_done(
        self,
        future: Future,
    ) -> None:
        with self._lock:
            self._futures.discard(future)

        try:
            future.result()
        except Exception as ex:
            logger.error(
                "Algo App background delivery " "failed: %s: %s",
                type(ex).__name__,
                ex,
            )

    # ============================================================
    # Public Delivery
    # ============================================================

    def dispatch_ema_alert(
        self,
        payload: dict,
    ) -> bool:
        if not self.enabled:
            logger.info("Algo App dispatch skipped because " "ALGO_APP_ENABLED=False.")

            return False

        if not self.url:
            logger.warning(
                "Algo App dispatch skipped because " "ALGO_APP_URL is empty."
            )

            return False

        if not isinstance(payload, dict):
            logger.warning("Algo App dispatch skipped because " "payload is invalid.")

            return False

        payload_copy = deepcopy(payload)

        event_id = str(payload_copy.get("event_id") or "").strip() or None

        if self.send_in_background:
            try:
                self._record_dispatch(
                    event_id=event_id,
                    background=True,
                )

                future = self._executor.submit(
                    self._background_delivery,
                    payload_copy,
                )

                with self._lock:
                    self._futures.add(future)

                future.add_done_callback(self._background_done)

                logger.info(
                    "Algo App EMA payload queued. " "event_id=%s",
                    event_id,
                )

                return True

            except Exception as ex:
                self._decrement_pending_count()

                logger.error(
                    "Algo App background dispatch "
                    "failed. event_id=%s, "
                    "error=%s: %s",
                    event_id,
                    type(ex).__name__,
                    ex,
                )

                return False

        self._record_dispatch(
            event_id=event_id,
            background=False,
        )

        result = self._send_payload(payload_copy)

        self._update_opening_range_delivery_state(
            event_id=event_id,
            delivery_result=result,
        )

        return bool(result.get("success"))

    def send_ema_alert(
        self,
        payload: dict,
    ) -> dict:
        if not isinstance(payload, dict):
            return {
                "enabled": self.enabled,
                "configured": self.is_configured(),
                "attempted": False,
                "success": False,
                "event_id": None,
                "status_code": None,
                "attempt_count": 0,
                "response": None,
                "error": "Invalid payload.",
                "started_at": (self._now_market_time()),
                "completed_at": (self._now_market_time()),
            }

        payload_copy = deepcopy(payload)

        event_id = str(payload_copy.get("event_id") or "").strip() or None

        self._record_dispatch(
            event_id=event_id,
            background=False,
        )

        result = self._send_payload(payload_copy)

        self._update_opening_range_delivery_state(
            event_id=event_id,
            delivery_result=result,
        )

        return result

    # ============================================================
    # Status
    # ============================================================

    def get_status(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "configured": self.is_configured(),
                "url_configured": bool(self.url),
                "auth_type": self.auth_type,
                "authentication_configured": bool(
                    self.auth_type == "none"
                    or (self.auth_type == "bearer" and self.auth_token)
                    or (self.auth_type == "api_key" and self.api_key)
                ),
                "timeout_seconds": (self.timeout_seconds),
                "verify_ssl": self.verify_ssl,
                "max_retries": self.max_retries,
                "retry_delay_seconds": (self.retry_delay_seconds),
                "send_in_background": (self.send_in_background),
                "dispatch_count": (self.dispatch_count),
                "background_dispatch_count": (self.background_dispatch_count),
                "delivery_attempt_count": (self.delivery_attempt_count),
                "delivery_success_count": (self.delivery_success_count),
                "delivery_failed_count": (self.delivery_failed_count),
                "retry_count": self.retry_count,
                "pending_count": self.pending_count,
                "last_dispatch_at": (self.last_dispatch_at),
                "last_success_at": (self.last_success_at),
                "last_failure_at": (self.last_failure_at),
                "last_event_id": (self.last_event_id),
                "last_status_code": (self.last_status_code),
                "last_error": self.last_error,
                "last_response": deepcopy(self.last_response),
                "market_time": (self._now_market_time()),
            }

    # ============================================================
    # Shutdown
    # ============================================================

    def shutdown(
        self,
        wait: bool = False,
    ) -> None:
        try:
            self._executor.shutdown(
                wait=wait,
                cancel_futures=not wait,
            )

            logger.info("Algo App background executor " "shutdown completed.")

        except Exception as ex:
            logger.error(
                "Algo App executor shutdown failed: " "%s: %s",
                type(ex).__name__,
                ex,
            )


algo_app_service = AlgoAppService()


__all__ = [
    "AlgoAppService",
    "algo_app_service",
]
