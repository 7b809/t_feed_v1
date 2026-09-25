from __future__ import annotations

from typing import Any

import upstox_client
from upstox_client.rest import ApiException

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


class MarginService:
    """
    Fetches margin requirements for a given instrument using the
    Upstox Charge/Margin API.

    The default quantity is 65 (lot size) unless overridden.
    """

    DEFAULT_QUANTITY = 65
    DEFAULT_PRODUCT = "D"
    DEFAULT_TRANSACTION_TYPE = "BUY"

    def __init__(self) -> None:
        self._api_instance: upstox_client.ChargeApi | None = None

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------
    def _get_access_token(self) -> str:
        token = (
            getattr(config, "UPSTOX_ACCESS_TOKEN", None)
            or getattr(config, "ACCESS_TOKEN", None)
            or ""
        )

        return str(token).strip()

    def _get_api_instance(self) -> upstox_client.ChargeApi:
        if self._api_instance is not None:
            return self._api_instance

        access_token = self._get_access_token()

        if not access_token:
            raise ValueError(
                "Upstox access token is not configured. "
                "Set UPSTOX_ACCESS_TOKEN or ACCESS_TOKEN in config."
            )

        configuration = upstox_client.Configuration()
        configuration.access_token = access_token

        api_client = upstox_client.ApiClient(configuration)

        self._api_instance = upstox_client.ChargeApi(api_client)

        logger.info("Upstox ChargeApi (margin) instance initialized.")

        return self._api_instance

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def calculate_margin(
        self,
        *,
        instrument_key: str,
        quantity: int | None = None,
        product: str | None = None,
        transaction_type: str | None = None,
    ) -> dict[str, Any]:
        """
        Calculates the margin requirement for the given instrument.

        Returns a normalized dictionary:

        {
            "success": bool,
            "instrument_key": str,
            "quantity": int,
            "product": str,
            "transaction_type": str,
            "margin": dict | None,          # raw API margin response
            "required_margin": float | None,
            "available_margin": float | None,
            "raw_response": dict | None,    # full raw API response
            "error": str | None,
        }
        """
        resolved_quantity = (
            quantity if quantity is not None else self.DEFAULT_QUANTITY
        )

        resolved_product = product or self.DEFAULT_PRODUCT
        resolved_transaction_type = (
            transaction_type or self.DEFAULT_TRANSACTION_TYPE
        )

        result: dict[str, Any] = {
            "success": False,
            "instrument_key": instrument_key,
            "quantity": resolved_quantity,
            "product": resolved_product,
            "transaction_type": resolved_transaction_type,
            "margin": None,
            "required_margin": None,
            "available_margin": None,
            "raw_response": None,
            "error": None,
        }

        normalized_instrument_key = str(
            instrument_key or ""
        ).strip()

        if not normalized_instrument_key:
            result["error"] = "instrument_key is required."

            logger.error(
                "Margin calculation skipped: instrument_key is empty."
            )

            return result

        try:
            api_instance = self._get_api_instance()

        except Exception as exc:
            logger.exception(
                "Failed to initialize Upstox margin API client. "
                "error_type=%s",
                type(exc).__name__,
            )

            result["error"] = (
                f"{type(exc).__name__}: {exc}"
            )

            return result

        instruments = [
            upstox_client.Instrument(
                instrument_key=normalized_instrument_key,
                quantity=resolved_quantity,
                product=resolved_product,
                transaction_type=resolved_transaction_type,
            )
        ]

        margin_body = upstox_client.MarginRequest(instruments)

        try:
            api_response = api_instance.post_margin(margin_body)

        except ApiException as exc:
            logger.exception(
                "Upstox Margin API raised ApiException. "
                "instrument_key=%s, quantity=%s, "
                "error_type=%s",
                normalized_instrument_key,
                resolved_quantity,
                type(exc).__name__,
            )

            result["error"] = (
                f"ApiException: {exc.body or exc}"
            )

            return result

        except Exception as exc:
            logger.exception(
                "Unexpected error while calling Upstox "
                "Margin API. instrument_key=%s, "
                "error_type=%s",
                normalized_instrument_key,
                type(exc).__name__,
            )

            result["error"] = (
                f"{type(exc).__name__}: {exc}"
            )

            return result

        raw_response = self._to_dict(api_response)

        result.update(
            {
                "success": True,
                "raw_response": raw_response,
                "margin": self._extract_margin_block(
                    raw_response
                ),
            }
        )

        result["required_margin"] = (
            self._extract_required_margin(raw_response)
        )

        result["available_margin"] = (
            self._extract_available_margin(raw_response)
        )

        logger.info(
            "Margin calculated successfully. "
            "instrument_key=%s, quantity=%s, "
            "required_margin=%s, available_margin=%s",
            normalized_instrument_key,
            resolved_quantity,
            result.get("required_margin"),
            result.get("available_margin"),
        )

        return result

    # ------------------------------------------------------------------
    # Normalization helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _to_dict(value: Any) -> Any:
        """
        Converts Upstox SDK response objects to plain Python
        dictionaries/lists so they can be safely stored in MongoDB.
        """
        if value is None:
            return None

        if isinstance(value, (str, int, float, bool)):
            return value

        if isinstance(value, dict):
            return {
                key: MarginService._to_dict(val)
                for key, val in value.items()
            }

        if isinstance(value, (list, tuple)):
            return [
                MarginService._to_dict(item)
                for item in value
            ]

        if hasattr(value, "to_dict"):
            try:
                return MarginService._to_dict(
                    value.to_dict()
                )

            except Exception:
                logger.exception(
                    "Failed to convert Upstox SDK object "
                    "using to_dict()."
                )

        if hasattr(value, "__dict__"):
            return {
                key: MarginService._to_dict(val)
                for key, val in vars(value).items()
                if not key.startswith("_")
            }

        return str(value)

    @staticmethod
    def _extract_margin_block(
        raw_response: Any,
    ) -> Any:
        if not isinstance(raw_response, dict):
            return None

        data = raw_response.get("data")

        if isinstance(data, dict):
            return data

        return raw_response

    @staticmethod
    def _extract_required_margin(
        raw_response: Any,
    ) -> float | None:
        candidates: list[Any] = []

        if isinstance(raw_response, dict):
            data = raw_response.get("data")

            if isinstance(data, dict):
                candidates.extend(
                    [
                        data.get("required_margin"),
                        data.get("total_margin"),
                        data.get("margin_required"),
                    ]
                )

            candidates.extend(
                [
                    raw_response.get("required_margin"),
                    raw_response.get("total_margin"),
                    raw_response.get("margin_required"),
                ]
            )

        for candidate in candidates:
            try:
                if candidate is None:
                    continue

                return float(candidate)

            except (TypeError, ValueError, OverflowError):
                continue

        return None

    @staticmethod
    def _extract_available_margin(
        raw_response: Any,
    ) -> float | None:
        candidates: list[Any] = []

        if isinstance(raw_response, dict):
            data = raw_response.get("data")

            if isinstance(data, dict):
                candidates.extend(
                    [
                        data.get("available_margin"),
                        data.get("available_balance"),
                        data.get("margin_available"),
                    ]
                )

            candidates.extend(
                [
                    raw_response.get("available_margin"),
                    raw_response.get("available_balance"),
                    raw_response.get("margin_available"),
                ]
            )

        for candidate in candidates:
            try:
                if candidate is None:
                    continue

                return float(candidate)

            except (TypeError, ValueError, OverflowError):
                continue

        return None


margin_service = MarginService()