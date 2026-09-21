"""
services/telegram_service.py

Centralized Telegram notification service.

Responsibilities:
    - Send Telegram messages
    - Handle Telegram API failures safely
    - Support silent notifications
    - Validate configuration
    - Reuse a persistent HTTP session
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests
from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core import config

logger = logging.getLogger(__name__)


class TelegramService:
    """Thread-safe Telegram Bot API service."""

    def __init__(
        self,
        session: Optional[Session] = None,
    ) -> None:
        self.enabled = bool(getattr(config, "TELEGRAM_ENABLED", False))

        self.bot_token = str(getattr(config, "TELEGRAM_BOT_TOKEN", "") or "")

        self.chat_id = getattr(
            config,
            "TELEGRAM_CHAT_ID",
            None,
        )

        self.timeout = float(
            getattr(
                config,
                "TELEGRAM_TIMEOUT_SECONDS",
                10,
            )
        )

        self.url = "https://api.telegram.org/bot" f"{self.bot_token}/sendMessage"

        self.session = session or self._create_session()

        self._validate_configuration()

    # ------------------------------------------------------------------
    # Session setup
    # ------------------------------------------------------------------

    @staticmethod
    def _create_session() -> Session:
        """
        Create a reusable HTTP session with limited retries.

        Retries are restricted to connection-level and temporary
        server failures. POST requests are not automatically retried
        to avoid unintended duplicate Telegram messages.
        """
        session = requests.Session()

        retry_strategy = Retry(
            total=2,
            connect=2,
            read=2,
            status=2,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )

        adapter = HTTPAdapter(
            max_retries=retry_strategy,
        )

        session.mount("https://", adapter)
        session.mount("http://", adapter)

        return session

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _validate_configuration(self) -> None:
        """Log configuration problems without crashing application startup."""
        if not self.enabled:
            logger.info("Telegram notifications are disabled.")
            return

        if not self.bot_token:
            logger.error("Telegram is enabled but TELEGRAM_BOT_TOKEN is missing.")

        if self.chat_id is None or str(self.chat_id).strip() == "":
            logger.error("Telegram is enabled but TELEGRAM_CHAT_ID is missing.")

        if self.timeout <= 0:
            logger.warning(
                "Invalid Telegram timeout: %s. " "Falling back to 10 seconds.",
                self.timeout,
            )
            self.timeout = 10.0

    def is_configured(self) -> bool:
        """Return whether the required Telegram configuration exists."""
        return bool(
            self.enabled
            and self.bot_token
            and self.chat_id is not None
            and str(self.chat_id).strip()
        )

    # ------------------------------------------------------------------
    # Telegram API
    # ------------------------------------------------------------------

    def send(
        self,
        message: str,
        silent: bool = False,
    ) -> bool:
        """
        Send a message through the Telegram Bot API.

        Args:
            message: Text to send.
            silent: Disable Telegram notification sound.

        Returns:
            True if Telegram confirms successful delivery.
            False if disabled, misconfigured, or failed.
        """
        if not self.enabled:
            logger.debug("Telegram send skipped because service is disabled.")
            return False

        if not self.is_configured():
            logger.error("Telegram send skipped because configuration is invalid.")
            return False

        if not message or not str(message).strip():
            logger.warning("Telegram send skipped because message is empty.")
            return False

        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": str(message),
            "disable_notification": bool(silent),
        }

        try:
            response = self.session.post(
                self.url,
                json=payload,
                timeout=self.timeout,
            )

            response.raise_for_status()

            result = response.json()

            if not isinstance(result, dict):
                logger.error(
                    "Unexpected Telegram API response: %s",
                    result,
                )
                return False

            if not result.get("ok", False):
                logger.error(
                    "Telegram API rejected message: %s",
                    result,
                )
                return False

            logger.debug("Telegram message sent successfully.")

            return True

        except requests.Timeout:
            logger.error(
                "Telegram request timed out after %s seconds.",
                self.timeout,
            )
            return False

        except requests.HTTPError as ex:
            response_text = ""

            if ex.response is not None:
                response_text = ex.response.text[:500]

            logger.error(
                "Telegram HTTP error: %s | response=%s",
                ex,
                response_text,
            )
            return False

        except requests.RequestException as ex:
            logger.error(
                "Telegram network request failed: %s",
                ex,
            )
            return False

        except ValueError as ex:
            logger.error(
                "Telegram returned invalid JSON: %s",
                ex,
            )
            return False

        except Exception:
            logger.exception("Unexpected Telegram message failure.")
            return False

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    def send_silent(
        self,
        message: str,
    ) -> bool:
        """Send a Telegram message without notification sound."""
        return self.send(
            message,
            silent=True,
        )

    def send_loud(
        self,
        message: str,
    ) -> bool:
        """Send a normal Telegram notification."""
        return self.send(
            message,
            silent=False,
        )

    # ------------------------------------------------------------------
    # Resource cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying HTTP session."""
        try:
            self.session.close()

        except Exception:
            logger.exception("Failed to close Telegram HTTP session.")


telegram_service = TelegramService()
