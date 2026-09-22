from __future__ import annotations

import inspect
import logging
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

from core import config
from services.state import (
    load_historical_crosses,
    load_intraday_crosses,
)
from utils.common import parse_timestamp
from utils.json_store import read_json

logger = logging.getLogger(__name__)


class EmaQueryService:
    def __init__(
        self,
        *,
        runtime: Any = None,
        repository: Any = None,
        state_repository: Any = None,
        candle_repository: Any = None,
        crossover_repository: Any = None,
    ) -> None:
        self.runtime = runtime
        self.repository = repository
        self.state_repository = state_repository
        self.candle_repository = candle_repository
        self.crossover_repository = (
            crossover_repository
        )

    def configure(
        self,
        *,
        runtime: Any = None,
        repository: Any = None,
        state_repository: Any = None,
        candle_repository: Any = None,
        crossover_repository: Any = None,
    ) -> None:
        if runtime is not None:
            self.runtime = runtime

        if repository is not None:
            self.repository = repository

        if state_repository is not None:
            self.state_repository = (
                state_repository
            )

        if candle_repository is not None:
            self.candle_repository = (
                candle_repository
            )

        if crossover_repository is not None:
            self.crossover_repository = (
                crossover_repository
            )

    def set_runtime(
        self,
        runtime: Any,
    ):
        self.runtime = runtime

    @staticmethod
    def _get_value(
        source: Any,
        key: str,
        default: Any = None,
    ) -> Any:
        if source is None:
            return default

        if isinstance(source, Mapping):
            return source.get(
                key,
                default,
            )

        return getattr(
            source,
            key,
            default,
        )

    @staticmethod
    def _to_dict(
        value: Any,
    ) -> Any:
        if value is None:
            return None

        if isinstance(value, Mapping):
            return {
                str(key): (
                    EmaQueryService._to_dict(
                        item
                    )
                )
                for key, item in value.items()
            }

        if isinstance(
            value,
            (list, tuple, set),
        ):
            return [
                EmaQueryService._to_dict(
                    item
                )
                for item in value
            ]

        if isinstance(
            value,
            (datetime, date),
        ):
            return value.isoformat()

        if hasattr(
            value,
            "model_dump",
        ):
            try:
                return EmaQueryService._to_dict(
                    value.model_dump(
                        mode="json"
                    )
                )
            except Exception:
                logger.debug(
                    "Unable to convert object "
                    "using model_dump",
                    exc_info=True,
                )

        if hasattr(
            value,
            "to_dict",
        ):
            try:
                return EmaQueryService._to_dict(
                    value.to_dict()
                )
            except Exception:
                logger.debug(
                    "Unable to convert object "
                    "using to_dict",
                    exc_info=True,
                )

        if hasattr(
            value,
            "__dict__",
        ):
            return {
                str(key): (
                    EmaQueryService._to_dict(
                        item
                    )
                )
                for key, item in vars(
                    value
                ).items()
                if not str(key).startswith(
                    "_"
                )
            }

        return value

    @staticmethod
    def _normalize_instrument_keys(
        instrument_keys: (
            str
            | Iterable[str]
            | None
        ),
    ) -> list[str] | None:
        if instrument_keys is None:
            return None

        if isinstance(
            instrument_keys,
            str,
        ):
            values = instrument_keys.split(
                ","
            )
        else:
            values = list(
                instrument_keys
            )

        normalized: list[str] = []

        for value in values:
            item = str(value).strip()

            if (
                item
                and item not in normalized
            ):
                normalized.append(item)

        return normalized or None

    @staticmethod
    def _normalize_limit(
        limit: int | None,
    ) -> int:
        default = getattr(
            config,
            "EMA_API_DEFAULT_LIMIT",
            100,
        )

        maximum = getattr(
            config,
            "EMA_API_MAX_LIMIT",
            1000,
        )

        if limit is None:
            return default

        try:
            normalized = int(limit)
        except (
            TypeError,
            ValueError,
        ):
            return default

        return max(
            1,
            min(
                normalized,
                maximum,
            ),
        )

    @staticmethod
    def _normalize_offset(
        offset: int | None,
    ) -> int:
        if offset is None:
            return 0

        try:
            return max(
                0,
                int(offset),
            )
        except (
            TypeError,
            ValueError,
        ):
            return 0

    @staticmethod
    def _normalize_cross_scope(
        cross_scope: str | None,
    ) -> str:
        normalized = str(
            cross_scope or "all"
        ).strip().lower()

        aliases = {
            "history": "historical",
            "historic": "historical",
            "historical": "historical",
            "today": "intraday",
            "live": "intraday",
            "intra_day": "intraday",
            "intraday": "intraday",
            "all": "all",
            "both": "all",
        }

        resolved = aliases.get(
            normalized
        )

        if resolved is None:
            raise ValueError(
                "cross_scope must be "
                "historical, intraday, or all"
            )

        return resolved

    @staticmethod
    def _normalize_cross_type(
        cross_type: str | None,
    ) -> str | None:
        if cross_type is None:
            return None

        normalized = str(
            cross_type
        ).strip().lower()

        if not normalized:
            return None

        if normalized not in {
            "bullish",
            "bearish",
        }:
            raise ValueError(
                "cross_type must be "
                "bullish or bearish"
            )

        return normalized

    @staticmethod
    def _normalize_trading_date(
        value: date | str | None,
    ) -> date | None:
        if value is None:
            return None

        if isinstance(value, date):
            return value

        text = str(value).strip()

        if not text:
            return None

        try:
            return date.fromisoformat(
                text[:10]
            )
        except ValueError as ex:
            raise ValueError(
                "trading_date must use "
                "YYYY-MM-DD format"
            ) from ex

    @staticmethod
    def _normalize_datetime(
        value: Any,
    ) -> datetime | None:
        if value is None:
            return None

        parsed = parse_timestamp(
            value
        )

        if parsed is None:
            raise ValueError(
                f"Invalid timestamp: {value}"
            )

        return parsed

    @staticmethod
    def _matches_instrument(
        item: Any,
        instrument_keys: set[str] | None,
    ) -> bool:
        if not instrument_keys:
            return True

        if isinstance(item, Mapping):
            direct_key = item.get(
                "instrument_key"
            )

            if (
                direct_key is not None
                and str(direct_key)
                in instrument_keys
            ):
                return True

            instrument = item.get(
                "instrument"
            )

            if isinstance(
                instrument,
                Mapping,
            ):
                nested_key = instrument.get(
                    "instrument_key"
                )

                if (
                    nested_key is not None
                    and str(nested_key)
                    in instrument_keys
                ):
                    return True

            trading_symbol = item.get(
                "trading_symbol"
            )

            if (
                trading_symbol is not None
                and str(trading_symbol)
                in instrument_keys
            ):
                return True

        for key in (
            "instrument_key",
            "security_id",
            "contract_key",
            "trading_symbol",
        ):
            value = EmaQueryService._get_value(
                item,
                key,
            )

            if (
                value is not None
                and str(value)
                in instrument_keys
            ):
                return True

        return False

    @classmethod
    def _filter_instruments(
        cls,
        items: Any,
        instrument_keys: list[str] | None,
    ) -> list:
        if items is None:
            return []

        if isinstance(items, Mapping):
            values: list[Any] = []

            for key, value in items.items():
                if isinstance(
                    value,
                    Mapping,
                ):
                    item = dict(value)

                    item.setdefault(
                        "instrument_key",
                        str(key),
                    )

                    values.append(item)
                else:
                    values.append(
                        {
                            "instrument_key": (
                                str(key)
                            ),
                            "state": value,
                        }
                    )

        elif isinstance(
            items,
            (list, tuple, set),
        ):
            values = list(items)

        else:
            values = [items]

        if not instrument_keys:
            return values

        requested = set(
            instrument_keys
        )

        return [
            item
            for item in values
            if cls._matches_instrument(
                item,
                requested,
            )
        ]

    @staticmethod
    def _apply_pagination(
        rows: list[Any],
        *,
        limit: int,
        offset: int,
    ) -> list:
        return rows[
            offset : offset + limit
        ]

    @staticmethod
    async def _call(
        target: Any,
        method_name: str,
        **kwargs: Any,
    ) -> Any:
        if target is None:
            return None

        method = getattr(
            target,
            method_name,
            None,
        )

        if not callable(method):
            return None

        try:
            signature = inspect.signature(
                method
            )

            accepts_kwargs = any(
                parameter.kind
                == inspect.Parameter.VAR_KEYWORD
                for parameter
                in signature.parameters.values()
            )

            if accepts_kwargs:
                call_kwargs = kwargs
            else:
                call_kwargs = {
                    key: value
                    for key, value
                    in kwargs.items()
                    if key
                    in signature.parameters
                }

            result = method(
                **call_kwargs
            )

        except (
            TypeError,
            ValueError,
        ):
            logger.debug(
                "Method invocation failed "
                "target=%s method=%s",
                type(target).__name__,
                method_name,
                exc_info=True,
            )
            return None

        if inspect.isawaitable(
            result
        ):
            return await result

        return result

    async def _call_first_available(
        self,
        targets: list[Any],
        method_names: list[str],
        **kwargs: Any,
    ) -> Any:
        for target in targets:
            if target is None:
                continue

            for method_name in method_names:
                method = getattr(
                    target,
                    method_name,
                    None,
                )

                if not callable(method):
                    continue

                result = await self._call(
                    target,
                    method_name,
                    **kwargs,
                )

                if result is not None:
                    return result

        return None

    def _runtime_states(
        self,
    ) -> Any:
        if self.runtime is None:
            return None

        for attribute in (
            "states",
            "state",
            "ema_states",
            "current_states",
        ):
            value = getattr(
                self.runtime,
                attribute,
                None,
            )

            if value is not None:
                return value

        return None

    def _runtime_contracts(
        self,
    ) -> list:
        if self.runtime is not None:
            contracts = getattr(
                self.runtime,
                "contracts",
                None,
            )

            if isinstance(
                contracts,
                list,
            ):
                return [
                    contract
                    for contract in contracts
                    if isinstance(
                        contract,
                        dict,
                    )
                ]

        payload = read_json(
            config.RUNTIME_ROOT
            / "selected_contracts.json",
            {},
        )

        if not isinstance(
            payload,
            dict,
        ):
            return []

        contracts = payload.get(
            "data",
            [],
        )

        if not isinstance(
            contracts,
            list,
        ):
            return []

        return [
            contract
            for contract in contracts
            if isinstance(
                contract,
                dict,
            )
        ]

    def _contract_map(
        self,
    ) -> dict[str, dict]:
        return {
            str(
                contract.get(
                    "instrument_key"
                )
            ): contract
            for contract
            in self._runtime_contracts()
            if contract.get(
                "instrument_key"
            )
        }

    def _selected_contracts(
        self,
        requested_keys: list[str] | None,
    ) -> list:
        contracts = (
            self._runtime_contracts()
        )

        if not requested_keys:
            return contracts

        requested = set(
            requested_keys
        )

        return [
            contract
            for contract in contracts
            if str(
                contract.get(
                    "instrument_key"
                )
            )
            in requested
        ]

    @staticmethod
    def _candle_timestamp(
        item: Mapping[str, Any],
    ) -> datetime | None:
        return parse_timestamp(
            item.get("timestamp")
            or item.get(
                "candle_timestamp"
            )
        )

    @classmethod
    def _filter_by_time(
        cls,
        rows: list[dict],
        *,
        trading_date: date | None,
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> list:
        output: list[dict] = []

        for row in rows:
            timestamp = (
                cls._candle_timestamp(
                    row
                )
            )

            if timestamp is None:
                continue

            if (
                trading_date is not None
                and timestamp.date()
                != trading_date
            ):
                continue

            if (
                start_time is not None
                and timestamp < start_time
            ):
                continue

            if (
                end_time is not None
                and timestamp > end_time
            ):
                continue

            output.append(row)

        return output

    async def get_current_state(
        self,
        *,
        instrument_key: str | None = None,
        instrument_keys: (
            str
            | Iterable[str]
            | None
        ) = None,
    ) -> dict[str, Any]:
        requested_keys = (
            self._normalize_instrument_keys(
                instrument_keys
            )
        )

        if instrument_key:
            requested_keys = (
                self._normalize_instrument_keys(
                    [instrument_key]
                )
            )

        states = self._runtime_states()

        if states is None:
            states = await (
                self._call_first_available(
                    [
                        self.state_repository,
                        self.repository,
                    ],
                    [
                        "get_current_states",
                        "get_latest_states",
                        "fetch_current_states",
                        "find_current_states",
                    ],
                    instrument_keys=(
                        requested_keys
                    ),
                )
            )

        filtered_states = (
            self._filter_instruments(
                states,
                requested_keys,
            )
        )

        contract_map = (
            self._contract_map()
        )

        result: list[dict] = []

        for item in filtered_states:
            converted = self._to_dict(
                item
            )

            if not isinstance(
                converted,
                dict,
            ):
                continue

            key = converted.get(
                "instrument_key"
            )

            if key is None:
                continue

            contract = contract_map.get(
                str(key),
                {},
            )

            if (
                "state" in converted
                and isinstance(
                    converted.get("state"),
                    dict,
                )
            ):
                state = converted["state"]
            else:
                state = {
                    name: value
                    for name, value
                    in converted.items()
                    if name
                    != "instrument_key"
                }

            result.append(
                {
                    "instrument_key": (
                        str(key)
                    ),
                    "contract": contract,
                    "state": state,
                }
            )

        return {
            "count": len(result),
            "instruments": result,
        }

    async def get_latest_state(
        self,
        instrument_key: str,
    ) -> dict[str, Any] | None:
        result = await self.get_current_state(
            instrument_key=instrument_key
        )

        instruments = result.get(
            "instruments",
            [],
        )

        if not instruments:
            return None

        return instruments[0]

    async def get_historical_ema(
        self,
        *,
        instrument_key: str | None = None,
        instrument_keys: (
            str
            | Iterable[str]
            | None
        ) = None,
        trading_date: date | str | None = None,
        start_time: Any = None,
        end_time: Any = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        requested_keys = (
            self._normalize_instrument_keys(
                instrument_keys
            )
        )

        if instrument_key:
            requested_keys = (
                self._normalize_instrument_keys(
                    [instrument_key]
                )
            )

        safe_limit = (
            self._normalize_limit(
                limit
            )
        )

        safe_offset = (
            self._normalize_offset(
                offset
            )
        )

        normalized_date = (
            self._normalize_trading_date(
                trading_date
            )
        )

        normalized_start = (
            self._normalize_datetime(
                start_time
            )
        )

        normalized_end = (
            self._normalize_datetime(
                end_time
            )
        )

        records = await (
            self._call_first_available(
                [
                    self.repository,
                    self.candle_repository,
                    self.state_repository,
                ],
                [
                    "get_historical_ema",
                    "fetch_historical_ema",
                    "query_historical_ema",
                    "get_ema_history",
                    "fetch_ema_history",
                    "get_historical_records",
                    "find_historical_records",
                ],
                instrument_key=(
                    instrument_key
                ),
                instrument_keys=(
                    requested_keys
                ),
                trading_date=(
                    normalized_date
                ),
                start_time=(
                    normalized_start
                ),
                end_time=(
                    normalized_end
                ),
                limit=safe_limit,
                offset=safe_offset,
            )
        )

        filtered_records = (
            self._filter_instruments(
                records,
                requested_keys,
            )
        )

        converted = [
            self._to_dict(item)
            for item in filtered_records
        ]

        return {
            "count": len(converted),
            "limit": safe_limit,
            "offset": safe_offset,
            "trading_date": (
                normalized_date.isoformat()
                if normalized_date
                else None
            ),
            "instruments": requested_keys,
            "data": converted,
        }

    async def get_intraday_ema(
        self,
        *,
        instrument_key: str | None = None,
        instrument_keys: (
            str
            | Iterable[str]
            | None
        ) = None,
        trading_date: date | str | None = None,
        start_time: Any = None,
        end_time: Any = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        requested_keys = (
            self._normalize_instrument_keys(
                instrument_keys
            )
        )

        if instrument_key:
            requested_keys = (
                self._normalize_instrument_keys(
                    [instrument_key]
                )
            )

        safe_limit = (
            self._normalize_limit(
                limit
            )
        )

        safe_offset = (
            self._normalize_offset(
                offset
            )
        )

        normalized_date = (
            self._normalize_trading_date(
                trading_date
            )
        )

        normalized_start = (
            self._normalize_datetime(
                start_time
            )
        )

        normalized_end = (
            self._normalize_datetime(
                end_time
            )
        )

        records = await (
            self._call_first_available(
                [
                    self.repository,
                    self.candle_repository,
                    self.state_repository,
                ],
                [
                    "get_intraday_ema",
                    "fetch_intraday_ema",
                    "query_intraday_ema",
                    "get_intraday_records",
                    "fetch_intraday_records",
                    "find_intraday_records",
                ],
                instrument_key=(
                    instrument_key
                ),
                instrument_keys=(
                    requested_keys
                ),
                trading_date=(
                    normalized_date
                ),
                start_time=(
                    normalized_start
                ),
                end_time=(
                    normalized_end
                ),
                limit=safe_limit,
                offset=safe_offset,
            )
        )

        filtered_records = (
            self._filter_instruments(
                records,
                requested_keys,
            )
        )

        converted = [
            self._to_dict(item)
            for item in filtered_records
        ]

        return {
            "count": len(converted),
            "limit": safe_limit,
            "offset": safe_offset,
            "trading_date": (
                normalized_date.isoformat()
                if normalized_date
                else None
            ),
            "instruments": requested_keys,
            "data": converted,
        }

    def _load_file_crossovers(
        self,
        *,
        contracts: list[dict],
        cross_scope: str,
        trading_date: date | None,
    ) -> list:
        records: list[dict] = []

        for contract in contracts:
            instrument_key = str(
                contract.get(
                    "instrument_key"
                )
                or ""
            ).strip()

            if not instrument_key:
                continue

            scopes = (
                ["historical", "intraday"]
                if cross_scope == "all"
                else [cross_scope]
            )

            for scope in scopes:
                if scope == "historical":
                    payload = (
                        load_historical_crosses(
                            contract
                        )
                    )
                else:
                    payload = (
                        load_intraday_crosses(
                            contract,
                            trading_date,
                        )
                    )

                if not isinstance(
                    payload,
                    dict,
                ):
                    continue

                rows = payload.get(
                    "crossovers",
                    [],
                )

                if not isinstance(
                    rows,
                    list,
                ):
                    continue

                for row in rows:
                    if not isinstance(
                        row,
                        dict,
                    ):
                        continue

                    record = dict(row)

                    record.setdefault(
                        "instrument_key",
                        instrument_key,
                    )

                    record.setdefault(
                        "trading_symbol",
                        contract.get(
                            "trading_symbol"
                        ),
                    )

                    record.setdefault(
                        "strike_price",
                        contract.get(
                            "strike_price"
                        ),
                    )

                    record.setdefault(
                        "option_type",
                        contract.get(
                            "option_type"
                        ),
                    )

                    record[
                        "cross_scope"
                    ] = scope

                    records.append(record)

        records.sort(
            key=lambda item: str(
                item.get("timestamp")
                or ""
            ),
            reverse=True,
        )

        return records

    async def get_crossovers(
        self,
        *,
        instrument_key: str | None = None,
        instrument_keys: (
            str
            | Iterable[str]
            | None
        ) = None,
        trading_date: date | str | None = None,
        cross_type: str | None = None,
        cross_scope: str = "all",
        start_time: Any = None,
        end_time: Any = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        requested_keys = (
            self._normalize_instrument_keys(
                instrument_keys
            )
        )

        if instrument_key:
            requested_keys = (
                self._normalize_instrument_keys(
                    [instrument_key]
                )
            )

        normalized_scope = (
            self._normalize_cross_scope(
                cross_scope
            )
        )

        normalized_cross_type = (
            self._normalize_cross_type(
                cross_type
            )
        )

        normalized_date = (
            self._normalize_trading_date(
                trading_date
            )
        )

        normalized_start = (
            self._normalize_datetime(
                start_time
            )
        )

        normalized_end = (
            self._normalize_datetime(
                end_time
            )
        )

        safe_limit = (
            self._normalize_limit(
                limit
            )
        )

        safe_offset = (
            self._normalize_offset(
                offset
            )
        )

        contracts = self._selected_contracts(
            requested_keys
        )

        records = self._load_file_crossovers(
            contracts=contracts,
            cross_scope=normalized_scope,
            trading_date=normalized_date,
        )

        if not records:
            repository_records = await (
                self._call_first_available(
                    [
                        self.crossover_repository,
                        self.repository,
                        self.state_repository,
                    ],
                    [
                        "get_crossovers",
                        "fetch_crossovers",
                        "query_crossovers",
                        "get_latest_crosses",
                        "fetch_latest_crosses",
                        "find_crossovers",
                    ],
                    instrument_key=(
                        instrument_key
                    ),
                    instrument_keys=(
                        requested_keys
                    ),
                    trading_date=(
                        normalized_date
                    ),
                    cross_type=(
                        normalized_cross_type
                    ),
                    cross_scope=(
                        normalized_scope
                    ),
                    start_time=(
                        normalized_start
                    ),
                    end_time=(
                        normalized_end
                    ),
                    limit=safe_limit,
                    offset=safe_offset,
                )
            )

            records = [
                self._to_dict(item)
                for item
                in self._filter_instruments(
                    repository_records,
                    requested_keys,
                )
                if isinstance(
                    self._to_dict(item),
                    dict,
                )
            ]

        records = self._filter_by_time(
            records,
            trading_date=normalized_date,
            start_time=normalized_start,
            end_time=normalized_end,
        )

        if normalized_cross_type:
            records = [
                record
                for record in records
                if str(
                    record.get(
                        "cross_type"
                    )
                    or record.get(
                        "signal"
                    )
                    or ""
                ).strip().lower()
                == normalized_cross_type
            ]

        total_count = len(records)

        paginated = (
            self._apply_pagination(
                records,
                limit=safe_limit,
                offset=safe_offset,
            )
        )

        bullish_count = sum(
            str(
                record.get(
                    "cross_type"
                )
                or ""
            ).lower()
            == "bullish"
            for record in records
        )

        bearish_count = sum(
            str(
                record.get(
                    "cross_type"
                )
                or ""
            ).lower()
            == "bearish"
            for record in records
        )

        return {
            "count": len(paginated),
            "total_count": total_count,
            "limit": safe_limit,
            "offset": safe_offset,
            "trading_date": (
                normalized_date.isoformat()
                if normalized_date
                else None
            ),
            "cross_scope": (
                normalized_scope
            ),
            "cross_type": (
                normalized_cross_type
            ),
            "instrument_keys": (
                requested_keys
            ),
            "bullish_cross_count": (
                bullish_count
            ),
            "bearish_cross_count": (
                bearish_count
            ),
            "latest_cross": (
                records[0]
                if records
                else None
            ),
            "data": paginated,
        }

    async def query(
        self,
        *,
        query_type: str = "current",
        data_type: str | None = None,
        instrument_key: str | None = None,
        instrument_keys: (
            str
            | Iterable[str]
            | None
        ) = None,
        trading_date: date | str | None = None,
        start_time: Any = None,
        end_time: Any = None,
        cross_type: str | None = None,
        cross_scope: str = "all",
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        selected_type = (
            data_type
            if data_type is not None
            else query_type
        )

        normalized_type = str(
            selected_type
        ).strip().lower()

        if normalized_type in {
            "current",
            "latest",
            "state",
            "live",
        }:
            return await self.get_current_state(
                instrument_key=instrument_key,
                instrument_keys=instrument_keys,
            )

        if normalized_type in {
            "historical",
            "history",
        }:
            return await self.get_historical_ema(
                instrument_key=instrument_key,
                instrument_keys=instrument_keys,
                trading_date=trading_date,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                offset=offset,
            )

        if normalized_type in {
            "intraday",
            "intra_day",
        }:
            return await self.get_intraday_ema(
                instrument_key=instrument_key,
                instrument_keys=instrument_keys,
                trading_date=trading_date,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                offset=offset,
            )

        if normalized_type in {
            "crossovers",
            "crosses",
            "signals",
        }:
            return await self.get_crossovers(
                instrument_key=instrument_key,
                instrument_keys=instrument_keys,
                trading_date=trading_date,
                cross_type=cross_type,
                cross_scope=cross_scope,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                offset=offset,
            )

        raise ValueError(
            "Unsupported EMA query type: "
            f"{selected_type}"
        )

    async def query_many(
        self,
        *,
        query_type: str = "current",
        data_type: str | None = None,
        instrument_keys: Iterable[str],
        trading_date: date | str | None = None,
        start_time: Any = None,
        end_time: Any = None,
        cross_type: str | None = None,
        cross_scope: str = "all",
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        normalized_keys = (
            self._normalize_instrument_keys(
                instrument_keys
            )
        )

        return await self.query(
            query_type=query_type,
            data_type=data_type,
            instrument_keys=normalized_keys,
            trading_date=trading_date,
            start_time=start_time,
            end_time=end_time,
            cross_type=cross_type,
            cross_scope=cross_scope,
            limit=limit,
            offset=offset,
        )

    async def query_all(
        self,
        *,
        query_type: str = "current",
        data_type: str | None = None,
        trading_date: date | str | None = None,
        start_time: Any = None,
        end_time: Any = None,
        cross_type: str | None = None,
        cross_scope: str = "all",
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        return await self.query(
            query_type=query_type,
            data_type=data_type,
            trading_date=trading_date,
            start_time=start_time,
            end_time=end_time,
            cross_type=cross_type,
            cross_scope=cross_scope,
            limit=limit,
            offset=offset,
        )


ema_query_service = EmaQueryService()