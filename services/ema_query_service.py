"""
services/ema_query_service.py

Centralized read-only query service for EMA data.

Supported query types:
    - Historical EMA data
    - Intraday EMA data
    - Current/latest EMA state
    - One instrument
    - Multiple instruments
    - All selected instruments

This service is intended to be used by:
    - api/ema_routes.py
    - REST EMA endpoints
    - Dashboard APIs
    - WebSocket initial state responses

The service does not:
    - Modify EMA state
    - Process live candles
    - Send Telegram notifications
    - Broadcast WebSocket events
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


class EmaQueryService:
    """
    Read-only query layer for EMA runtime and persisted data.

    Dependencies are injected to keep the service compatible with
    different repository and runtime implementations.
    """

    def __init__(
        self,
        *,
        runtime: Optional[Any] = None,
        repository: Optional[Any] = None,
        state_repository: Optional[Any] = None,
        candle_repository: Optional[Any] = None,
        crossover_repository: Optional[Any] = None,
    ) -> None:
        self.runtime = runtime
        self.repository = repository
        self.state_repository = state_repository
        self.candle_repository = candle_repository
        self.crossover_repository = crossover_repository

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_value(source: Any, key: str, default: Any = None) -> Any:
        """Read a value from a dictionary, dataclass, or object."""
        if source is None:
            return default

        if isinstance(source, Mapping):
            return source.get(key, default)

        return getattr(source, key, default)

    @staticmethod
    def _to_dict(value: Any) -> Any:
        """Convert supported objects into JSON-compatible structures."""
        if value is None:
            return None

        if isinstance(value, Mapping):
            return {
                str(key): EmaQueryService._to_dict(item) for key, item in value.items()
            }

        if isinstance(value, (list, tuple, set)):
            return [EmaQueryService._to_dict(item) for item in value]

        if isinstance(value, datetime):
            return value.isoformat()

        if isinstance(value, date):
            return value.isoformat()

        if hasattr(value, "model_dump"):
            try:
                return EmaQueryService._to_dict(value.model_dump(mode="json"))
            except Exception:
                logger.debug("Unable to model_dump object.", exc_info=True)

        if hasattr(value, "to_dict"):
            try:
                return EmaQueryService._to_dict(value.to_dict())
            except Exception:
                logger.debug("Unable to convert object using to_dict.", exc_info=True)

        if hasattr(value, "__dict__"):
            return {
                str(key): EmaQueryService._to_dict(item)
                for key, item in vars(value).items()
                if not str(key).startswith("_")
            }

        return value

    @staticmethod
    def _normalize_instrument_keys(
        instrument_keys: Optional[str | Iterable[str]],
    ) -> Optional[list[str]]:
        """
        Normalize instrument filters.

        Supported:
            None
            "NSE_FO|123"
            "NSE_FO|123,NSE_FO|456"
            ["NSE_FO|123", "NSE_FO|456"]
        """
        if instrument_keys is None:
            return None

        if isinstance(instrument_keys, str):
            values = instrument_keys.split(",")
        else:
            values = list(instrument_keys)

        normalized: list[str] = []
        for value in values:
            item = str(value).strip()
            if item and item not in normalized:
                normalized.append(item)

        return normalized or None

    @staticmethod
    def _normalize_limit(
        limit: Optional[int], *, default: int = 100, maximum: int = 1000
    ) -> int:
        """Apply safe query limits."""
        if limit is None:
            return default

        try:
            normalized = int(limit)
        except (TypeError, ValueError):
            return default

        return max(1, min(normalized, maximum))

    @staticmethod
    def _normalize_offset(offset: Optional[int]) -> int:
        """Apply a safe pagination offset."""
        if offset is None:
            return 0

        try:
            return max(0, int(offset))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _matches_instrument(item: Any, instrument_keys: Optional[set[str]]) -> bool:
        """Return whether a record matches the requested instruments."""
        if not instrument_keys:
            return True

        possible_keys = {
            "instrument_key",
            "instrument",
            "security_id",
            "contract_key",
            "trading_symbol",
        }
        for key in possible_keys:
            value = EmaQueryService._get_value(item, key)
            if value is not None and str(value) in instrument_keys:
                return True

        return False

    @classmethod
    def _filter_instruments(
        cls, items: Any, instrument_keys: Optional[list[str]]
    ) -> list[Any]:
        """Filter a collection by instrument keys."""
        if items is None:
            return []

        if isinstance(items, Mapping):
            values = list(items.values())
        elif isinstance(items, (list, tuple, set)):
            values = list(items)
        else:
            values = [items]

        if not instrument_keys:
            return values

        requested = set(instrument_keys)
        return [item for item in values if cls._matches_instrument(item, requested)]

    # ------------------------------------------------------------------
    # Async compatibility
    # ------------------------------------------------------------------

    @staticmethod
    async def _call(target: Any, method_name: str, **kwargs: Any) -> Any:
        """
        Call a method synchronously or asynchronously.

        Unknown methods return None instead of raising AttributeError.
        """
        if target is None:
            return None

        method = getattr(target, method_name, None)
        if method is None or not callable(method):
            return None

        try:
            result = method(**kwargs)
        except TypeError:
            """
            Some existing repository methods may not accept all
            optional keyword arguments. Retry without kwargs.
            """
            try:
                result = method()
            except TypeError:
                logger.debug(
                    "Method signature mismatch: %s.%s",
                    type(target).__name__,
                    method_name,
                    exc_info=True,
                )
                return None

        if inspect.isawaitable(result):
            return await result

        return result

    async def _call_first_available(
        self, targets: list[Any], method_names: list[str], **kwargs: Any
    ) -> Any:
        """
        Call the first available method on the supplied targets.
        """
        for target in targets:
            if target is None:
                continue

            for method_name in method_names:
                method = getattr(target, method_name, None)
                if method is None or not callable(method):
                    continue

                result = await self._call(target, method_name, **kwargs)
                if result is not None:
                    return result

        return None

    # ------------------------------------------------------------------
    # Runtime state
    # ------------------------------------------------------------------

    def _runtime_states(self) -> Any:
        """Read current in-memory runtime states."""
        if self.runtime is None:
            return None

        for attribute in ("states", "state", "ema_states", "current_states"):
            value = getattr(self.runtime, attribute, None)
            if value is not None:
                return value

        return None

    async def get_current_state(
        self,
        *,
        instrument_key: Optional[str] = None,
        instrument_keys: Optional[str | Iterable[str]] = None,
    ) -> dict[str, Any]:
        """
        Return current EMA state for one, multiple, or all instruments.
        """
        requested_keys = self._normalize_instrument_keys(instrument_keys)
        if instrument_key:
            requested_keys = self._normalize_instrument_keys([instrument_key])

        states = self._runtime_states()
        if states is None:
            states = await self._call_first_available(
                [self.state_repository, self.repository],
                [
                    "get_current_states",
                    "get_latest_states",
                    "fetch_current_states",
                    "find_current_states",
                ],
                instrument_keys=requested_keys,
            )

        filtered_states = self._filter_instruments(states, requested_keys)
        result = [self._to_dict(item) for item in filtered_states]

        return {"count": len(result), "instruments": result}

    async def get_latest_state(self, instrument_key: str) -> Optional[dict[str, Any]]:
        """Return the latest EMA state for one instrument."""
        result = await self.get_current_state(instrument_key=instrument_key)
        instruments = result.get("instruments", [])
        if not instruments:
            return None

        return instruments[0]

    # ------------------------------------------------------------------
    # Historical EMA queries
    # ------------------------------------------------------------------

    async def get_historical_ema(
        self,
        *,
        instrument_key: Optional[str] = None,
        instrument_keys: Optional[str | Iterable[str]] = None,
        trading_date: Optional[str] = None,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Query historical EMA records.

        The method supports multiple possible repository method names
        to remain compatible with existing repository implementations.
        """
        requested_keys = self._normalize_instrument_keys(instrument_keys)
        if instrument_key:
            requested_keys = self._normalize_instrument_keys([instrument_key])

        safe_limit = self._normalize_limit(limit)
        safe_offset = self._normalize_offset(offset)
        query_kwargs = {
            "instrument_key": instrument_key,
            "instrument_keys": requested_keys,
            "trading_date": trading_date,
            "start_time": start_time,
            "end_time": end_time,
            "limit": safe_limit,
            "offset": safe_offset,
        }

        records = await self._call_first_available(
            [self.repository, self.candle_repository, self.state_repository],
            [
                "get_historical_ema",
                "fetch_historical_ema",
                "query_historical_ema",
                "get_ema_history",
                "fetch_ema_history",
                "get_historical_records",
                "find_historical_records",
            ],
            **query_kwargs,
        )

        filtered_records = self._filter_instruments(records, requested_keys)
        result = [self._to_dict(item) for item in filtered_records]

        return {
            "count": len(result),
            "limit": safe_limit,
            "offset": safe_offset,
            "trading_date": trading_date,
            "instruments": requested_keys,
            "data": result,
        }

    # ------------------------------------------------------------------
    # Intraday EMA queries
    # ------------------------------------------------------------------

    async def get_intraday_ema(
        self,
        *,
        instrument_key: Optional[str] = None,
        instrument_keys: Optional[str | Iterable[str]] = None,
        trading_date: Optional[str] = None,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Query intraday EMA records.

        Intraday and historical records may be stored separately,
        therefore dedicated intraday repository methods are attempted
        first.
        """
        requested_keys = self._normalize_instrument_keys(instrument_keys)
        if instrument_key:
            requested_keys = self._normalize_instrument_keys([instrument_key])

        safe_limit = self._normalize_limit(limit)
        safe_offset = self._normalize_offset(offset)
        query_kwargs = {
            "instrument_key": instrument_key,
            "instrument_keys": requested_keys,
            "trading_date": trading_date,
            "start_time": start_time,
            "end_time": end_time,
            "limit": safe_limit,
            "offset": safe_offset,
        }

        records = await self._call_first_available(
            [self.repository, self.candle_repository, self.state_repository],
            [
                "get_intraday_ema",
                "fetch_intraday_ema",
                "query_intraday_ema",
                "get_intraday_records",
                "fetch_intraday_records",
                "find_intraday_records",
            ],
            **query_kwargs,
        )

        filtered_records = self._filter_instruments(records, requested_keys)
        result = [self._to_dict(item) for item in filtered_records]

        return {
            "count": len(result),
            "limit": safe_limit,
            "offset": safe_offset,
            "trading_date": trading_date,
            "instruments": requested_keys,
            "data": result,
        }

    # ------------------------------------------------------------------
    # Crossover queries
    # ------------------------------------------------------------------

    async def get_crossovers(
        self,
        *,
        instrument_key: Optional[str] = None,
        instrument_keys: Optional[str | Iterable[str]] = None,
        trading_date: Optional[str] = None,
        cross_type: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> dict[str, Any]:
        """Return EMA crossover records."""
        requested_keys = self._normalize_instrument_keys(instrument_keys)
        if instrument_key:
            requested_keys = self._normalize_instrument_keys([instrument_key])

        safe_limit = self._normalize_limit(limit)
        safe_offset = self._normalize_offset(offset)

        records = await self._call_first_available(
            [self.crossover_repository, self.repository, self.state_repository],
            [
                "get_crossovers",
                "fetch_crossovers",
                "query_crossovers",
                "get_latest_crosses",
                "fetch_latest_crosses",
                "find_crossovers",
            ],
            instrument_key=instrument_key,
            instrument_keys=requested_keys,
            trading_date=trading_date,
            cross_type=cross_type,
            limit=safe_limit,
            offset=safe_offset,
        )

        filtered_records = self._filter_instruments(records, requested_keys)

        if cross_type:
            normalized_cross_type = cross_type.upper()
            filtered_records = [
                item
                for item in filtered_records
                if str(
                    self._get_value(
                        item, "cross_type", self._get_value(item, "signal", "")
                    )
                ).upper()
                == normalized_cross_type
            ]

        result = [self._to_dict(item) for item in filtered_records]

        return {
            "count": len(result),
            "limit": safe_limit,
            "offset": safe_offset,
            "trading_date": trading_date,
            "cross_type": cross_type,
            "data": result,
        }

    # ------------------------------------------------------------------
    # Unified query interface
    # ------------------------------------------------------------------

    async def query(
        self,
        *,
        query_type: str = "current",
        instrument_key: Optional[str] = None,
        instrument_keys: Optional[str | Iterable[str]] = None,
        trading_date: Optional[str] = None,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
        cross_type: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Unified query method.

        Supported query_type values:
            current
            latest
            historical
            intraday
            crossovers
        """
        normalized_type = str(query_type).strip().lower()

        if normalized_type in {"current", "latest", "state", "live"}:
            return await self.get_current_state(
                instrument_key=instrument_key, instrument_keys=instrument_keys
            )

        if normalized_type in {"historical", "history"}:
            return await self.get_historical_ema(
                instrument_key=instrument_key,
                instrument_keys=instrument_keys,
                trading_date=trading_date,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                offset=offset,
            )

        if normalized_type in {"intraday", "intra_day"}:
            return await self.get_intraday_ema(
                instrument_key=instrument_key,
                instrument_keys=instrument_keys,
                trading_date=trading_date,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                offset=offset,
            )

        if normalized_type in {"crossovers", "crosses", "signals"}:
            return await self.get_crossovers(
                instrument_key=instrument_key,
                instrument_keys=instrument_keys,
                trading_date=trading_date,
                cross_type=cross_type,
                limit=limit,
                offset=offset,
            )

        raise ValueError(f"Unsupported EMA query type: {query_type}")

    async def query_many(
        self,
        *,
        query_type: str = "current",
        instrument_keys: Iterable[str],
        trading_date: Optional[str] = None,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
        cross_type: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> dict[str, Any]:
        """Query EMA data for multiple instruments."""
        normalized_keys = self._normalize_instrument_keys(instrument_keys)
        return await self.query(
            query_type=query_type,
            instrument_keys=normalized_keys,
            trading_date=trading_date,
            start_time=start_time,
            end_time=end_time,
            cross_type=cross_type,
            limit=limit,
            offset=offset,
        )

    async def query_all(
        self,
        *,
        query_type: str = "current",
        trading_date: Optional[str] = None,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
        cross_type: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> dict[str, Any]:
        """Query EMA data for all selected instruments."""
        return await self.query(
            query_type=query_type,
            trading_date=trading_date,
            start_time=start_time,
            end_time=end_time,
            cross_type=cross_type,
            limit=limit,
            offset=offset,
        )


# Shared default instance.
ema_query_service = EmaQueryService()
