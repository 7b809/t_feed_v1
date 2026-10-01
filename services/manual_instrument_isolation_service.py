from copy import deepcopy
from threading import Lock
from typing import Any

from core import config
from core.logger import get_logger
from services.option_service import (
    get_contract_info_by_strike_type,
    normalize_option_type,
)
from services.opening_range import state as runtime_state
from services.opening_range.candle_utils import get_now_market_time

logger = get_logger(__file__)


class ManualInstrumentIsolationError(Exception):
    pass


class ManualInstrumentIsolationService:
    def __init__(self):
        self._operation_lock = Lock()

    @staticmethod
    def _safe_float(
        value: Any,
        default: float | None = None,
    ) -> float | None:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return default

    @staticmethod
    def _normalize_requested_by(value: Any) -> str:
        normalized = str(value or "").strip()
        return normalized or "unknown"

    @staticmethod
    def _normalize_source(value: Any) -> str:
        normalized = str(value or "").strip().lower()

        allowed_sources = {
            "api",
            "telegram",
            "application",
            "system",
        }

        if normalized in allowed_sources:
            return normalized

        return "application"

    def _get_configured_strike_range(self, underlying: str | None = None) -> tuple[float, float]:
        if underlying:
            try:
                from services.strategy_context import get_strategy_context
                context = get_strategy_context(str(underlying).upper(), active_only=True)
                option = context.option_config if context else {}
            except Exception:
                settings = getattr(config, "STRATEGY_UNDERLYINGS", {}).get(str(underlying).upper(), {})
                option = settings.get("option") or {}
            if option.get("strike_from") is not None and option.get("strike_to") is not None:
                low, high = float(option["strike_from"]), float(option["strike_to"])
                return (min(low, high), max(low, high))
            return (0.0, 999999.0)
        strike_from = self._safe_float(
            getattr(config, "STRIKE_FROM", 0.0),
            default=0.0,
        )

        strike_to = self._safe_float(
            getattr(config, "STRIKE_TO", 0.0),
            default=0.0,
        )

        strike_from = strike_from if strike_from is not None else 0.0
        strike_to = strike_to if strike_to is not None else 0.0

        if strike_from > strike_to:
            strike_from, strike_to = strike_to, strike_from

        return strike_from, strike_to

    def _build_result(
        self,
        *,
        success: bool,
        status: str,
        message: str,
        strike_price: float | None,
        option_type: str | None,
        source: str,
        requested_by: str,
        previous_instrument: dict | None = None,
        isolated_instrument: dict | None = None,
        error_code: str | None = None,
    ) -> dict:
        strike_from, strike_to = self._get_configured_strike_range()

        return {
            "success": success,
            "status": status,
            "message": message,
            "error_code": error_code,
            "request": {
                "strike_price": strike_price,
                "option_type": option_type,
                "source": source,
                "requested_by": requested_by,
            },
            "configured_strike_range": {
                "minimum": strike_from,
                "maximum": strike_to,
                "inclusive": True,
            },
            "previous_instrument": deepcopy(previous_instrument),
            "isolated_instrument": deepcopy(isolated_instrument),
            "processed_at": get_now_market_time().isoformat(),
        }

    def isolate(
        self,
        *,
        strike_price: Any,
        option_type: Any,
        requested_by: Any,
        source: str = "application",
        underlying: str | None = None,
    ) -> dict:
        normalized_source = self._normalize_source(source)
        normalized_requested_by = self._normalize_requested_by(requested_by)

        normalized_strike = self._safe_float(strike_price)
        normalized_option_type = normalize_option_type(option_type)

        if normalized_strike is None or normalized_strike <= 0:
            return self._build_result(
                success=False,
                status="rejected",
                message="A valid positive strike price is required.",
                strike_price=normalized_strike,
                option_type=normalized_option_type,
                source=normalized_source,
                requested_by=normalized_requested_by,
                error_code="INVALID_STRIKE_PRICE",
            )

        if normalized_option_type not in {"CE", "PE"}:
            return self._build_result(
                success=False,
                status="rejected",
                message="Invalid option type. Supported values are CE and PE.",
                strike_price=normalized_strike,
                option_type=None,
                source=normalized_source,
                requested_by=normalized_requested_by,
                error_code="INVALID_OPTION_TYPE",
            )

        normalized_underlying = str(underlying or "").strip().upper() or None
        strike_from, strike_to = self._get_configured_strike_range(normalized_underlying)

        if not strike_from <= normalized_strike <= strike_to:
            return self._build_result(
                success=False,
                status="rejected",
                message=(
                    f"Strike {normalized_strike:g} is outside the "
                    f"configured range {strike_from:g} to "
                    f"{strike_to:g}."
                ),
                strike_price=normalized_strike,
                option_type=normalized_option_type,
                source=normalized_source,
                requested_by=normalized_requested_by,
                error_code="STRIKE_OUTSIDE_CONFIGURED_RANGE",
            )

        contract_info = get_contract_info_by_strike_type(
            strike_price=normalized_strike,
            instrument_type=normalized_option_type,
            underlying=normalized_underlying,
        )

        if not isinstance(contract_info, dict) or not contract_info:
            return self._build_result(
                success=False,
                status="not_found",
                message=(
                    f"{normalized_strike:g} "
                    f"{normalized_option_type} is inside the configured "
                    "strike range, but the contract is not available in "
                    "the loaded option instruments."
                ),
                strike_price=normalized_strike,
                option_type=normalized_option_type,
                source=normalized_source,
                requested_by=normalized_requested_by,
                error_code="INSTRUMENT_NOT_AVAILABLE",
            )

        instrument_key = str(contract_info.get("instrument_key") or "").strip()
        normalized_underlying = normalized_underlying or str(contract_info.get("underlying") or "").strip().upper() or None
        context = None
        if normalized_underlying:
            from services.strategy_context import get_strategy_context
            context = get_strategy_context(normalized_underlying, active_only=True)
            if context is None:
                return self._build_result(
                    success=False, status="rejected", message="The requested underlying is not enabled.",
                    strike_price=normalized_strike, option_type=normalized_option_type,
                    source=normalized_source, requested_by=normalized_requested_by,
                    error_code="UNDERLYING_NOT_ENABLED",
                )

        if not instrument_key:
            return self._build_result(
                success=False,
                status="not_found",
                message=(
                    "The requested contract was found, but its "
                    "instrument key is unavailable."
                ),
                strike_price=normalized_strike,
                option_type=normalized_option_type,
                source=normalized_source,
                requested_by=normalized_requested_by,
                error_code="INSTRUMENT_KEY_UNAVAILABLE",
            )

        runtime_state.ensure_current_market_day()
        strike_from, strike_to = self._get_configured_strike_range(normalized_underlying)
        if not strike_from <= normalized_strike <= strike_to:
            return self._build_result(
                success=False, status="rejected", message="Strike is outside the underlying's configured range.",
                strike_price=normalized_strike, option_type=normalized_option_type,
                source=normalized_source, requested_by=normalized_requested_by,
                error_code="STRIKE_OUTSIDE_CONFIGURED_RANGE",
            )

        with self._operation_lock:
            latest_instrument_snapshot = runtime_state.get_latest_instrument_ltp_snapshot(instrument_key)
            latest_main_index_ltp = (
                context.runtime_metadata.get("underlying_ltp")
                if context else runtime_state.get_latest_main_index_ltp_value()
            )
            isolated_at = get_now_market_time().isoformat()
            selected_state = context.selected_instrument if context else runtime_state.selected_or_instrument_state
            state_lock = context._lock if context else runtime_state.selected_or_lock
            with state_lock:
                previous_state = deepcopy(selected_state)
                new_state = {
                    "selected": True,
                    "underlying": normalized_underlying or "NIFTY",
                    "underlying_instrument_key": context.index_instrument_key if context else contract_info.get("underlying_instrument_key"),
                    "instrument_key": instrument_key,
                    "selected_level": "MANUAL",
                    "level_value": None,
                    "trigger_price": None,
                    "trigger_field": "manual_selection",
                    "touch_time": None,
                    "touch_source": normalized_source,
                    "selected_at": isolated_at,
                    "selection_priority": 0,
                    "selection_reason": "manual_instrument_isolation_override",
                    "locked_for_market_day": True,
                    "reference_average": None,
                    "average_window": None,
                    "contract_info": deepcopy(contract_info),
                    "range": None,
                    "levels": None,
                    "latest_live_data": {"ltp": latest_instrument_snapshot.get("ltp"), "updated_at": latest_instrument_snapshot.get("updated_at")},
                    "latest_main_index_ltp": latest_main_index_ltp,
                    "ema_alerts_count": 0,
                    "telegram_attempts_count": 0,
                    "telegram_success_count": 0,
                    "telegram_failed_count": 0,
                    "algo_app_attempts_count": 0,
                    "algo_app_dispatch_count": 0,
                    "algo_app_failed_count": 0,
                    "last_ema_alert": None,
                    "last_telegram_delivery": None,
                    "last_algo_app_delivery": None,
                    "disabled": False,
                    "manual_override": True,
                    "manual_override_source": normalized_source,
                    "manual_override_requested_by": normalized_requested_by,
                    "manual_override_at": isolated_at,
                    "previous_instrument_key": previous_state.get("instrument_key"),
                    "message": "Instrument manually isolated and locked for the market day.",
                }
                selected_state.clear()
                selected_state.update(new_state)
                isolated_snapshot = deepcopy(selected_state)

            if context:
                context.isolation_state.update({"selected": True, "selected_at": isolated_at, "reason": "manual_instrument_isolation_override"})

            if context:
                from services.strategy_context import persist_strategy_context
                persist_strategy_context(context, event_type="manual_instrument_selected")
            else:
                with runtime_state.opening_range_cache_lock:
                    runtime_state.opening_range_cache["isolated_instrument"] = deepcopy(isolated_snapshot)
                    runtime_state.opening_range_cache["isolated_instrument_selected"] = True
                    runtime_state.opening_range_cache["isolated_instrument_selected_at"] = isolated_at
                    runtime_state.opening_range_cache["isolated_instrument_selection_reason"] = "manual_instrument_isolation_override"
                    runtime_state.opening_range_cache["isolated_instrument_locked_for_market_day"] = True

        logger.warning(
            "Manual instrument isolation completed. "
            "instrument_key=%s, strike=%s, option_type=%s, "
            "source=%s, requested_by=%s, previous_instrument_key=%s",
            instrument_key,
            normalized_strike,
            normalized_option_type,
            normalized_source,
            normalized_requested_by,
            previous_state.get("instrument_key"),
        )

        return self._build_result(
            success=True,
            status="isolated",
            message=(
                f"{normalized_strike:g} "
                f"{normalized_option_type} was isolated successfully."
            ),
            strike_price=normalized_strike,
            option_type=normalized_option_type,
            source=normalized_source,
            requested_by=normalized_requested_by,
            previous_instrument=previous_state,
            isolated_instrument=isolated_snapshot,
        )


manual_instrument_isolation_service = ManualInstrumentIsolationService()
