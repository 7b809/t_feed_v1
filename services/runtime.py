from __future__ import annotations

import asyncio
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    as_completed,
)
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any

import upstox_client

from core import config
from core.logger import get_logger
from models.ema_events import EmaEvent
from services.ema import (
    calculate_sequence,
    normalize_candle,
    update_state,
)
from services.history import (
    build_history_client,
    fetch_history,
    fetch_intraday,
)
from services.notification_service import (
    NotificationService,
)
from services.state import (
    append_completed_candle,
    append_intraday_crossover,
    crossover_files_exist,
    load_state,
    save_historical_crosses,
    save_intraday_crosses,
    save_state,
)
from utils.common import (
    call_with_retry,
    chunks,
    object_to_dict,
    parse_timestamp,
)

logger = get_logger(__file__)


class EmaRuntime:
    def __init__(
        self,
        token_service,
        telegram_service=None,
        notification_service=None,
        websocket_manager=None,
        websocket_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.token_service = token_service
        self.telegram = telegram_service
        self.websocket_manager = websocket_manager
        self.websocket_loop = websocket_loop

        self.notifications = notification_service or NotificationService(
            telegram_service=telegram_service
        )

        self.contracts: list[dict] = []
        self.states: dict[str, dict] = {}
        self.locks: dict[str, Lock] = {}

        self.quote_client = None
        self.quote_api = None

        self._event_sequence = 0
        self._event_sequence_lock = Lock()

    def set_websocket_manager(
        self,
        websocket_manager: Any,
    ) -> None:
        self.websocket_manager = websocket_manager

    def set_websocket_loop(
        self,
        websocket_loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.websocket_loop = websocket_loop

    def close(self) -> None:
        if self.quote_client is None:
            return

        close_method = getattr(
            self.quote_client,
            "close",
            None,
        )

        if callable(close_method):
            try:
                close_method()
            except Exception:
                logger.exception("Failed to close quote client")

        self.quote_client = None
        self.quote_api = None

    def replace_contracts(
        self,
        contracts: list[dict],
    ) -> None:
        self.close()

        self.contracts = list(contracts)
        self.states = {}

        self.locks = {
            contract["instrument_key"]: Lock()
            for contract in self.contracts
            if contract.get("instrument_key")
        }

        token = self.token_service.get_access_token()

        configuration = upstox_client.Configuration()

        configuration.access_token = token

        self.quote_client = upstox_client.ApiClient(configuration)

        self.quote_api = upstox_client.MarketQuoteV3Api(self.quote_client)

        logger.info(
            "Runtime contracts replaced " "total_instruments=%s",
            len(self.contracts),
        )

    def initialize(
        self,
        trading_date: date,
    ) -> list:
        token = self.token_service.get_access_token()

        results: list[dict] = []
        total_instruments = len(self.contracts)

        workers = (
            min(
                config.MAX_WORKERS,
                total_instruments,
            )
            if total_instruments
            else 0
        )

        logger.info(
            "EMA warmup started "
            "trading_date=%s "
            "total_instruments=%s "
            "workers=%s",
            trading_date.isoformat(),
            total_instruments,
            workers,
        )

        with ThreadPoolExecutor(
            max_workers=workers or 1,
            thread_name_prefix="warmup",
        ) as pool:
            future_map = {
                pool.submit(
                    self._initialize_one,
                    token,
                    contract,
                    trading_date,
                ): contract
                for contract in self.contracts
            }

            for future in as_completed(future_map):
                contract = future_map[future]

                instrument_key = contract["instrument_key"]

                try:
                    state = future.result()

                    self.states[instrument_key] = state

                    results.append(
                        {
                            "instrument_key": (instrument_key),
                            "status": "success",
                        }
                    )

                    logger.info(
                        "Instrument warmup completed "
                        "instrument=%s "
                        "status=success",
                        instrument_key,
                    )

                except Exception as ex:
                    logger.exception(
                        "Instrument warmup failed " "instrument=%s",
                        instrument_key,
                    )

                    results.append(
                        {
                            "instrument_key": (instrument_key),
                            "status": "failed",
                            "error": str(ex),
                        }
                    )

                    self.notifications.notify_error(
                        title="EMA Warmup Failed",
                        message=(
                            f"Instrument: "
                            f"{instrument_key}\n"
                            f"Trading date: "
                            f"{trading_date.isoformat()}\n"
                            f"Error: {ex}"
                        ),
                        source=("ema_runtime.initialize"),
                    )

        success_count = sum(result.get("status") == "success" for result in results)

        failure_count = sum(result.get("status") == "failed" for result in results)

        logger.info(
            "EMA warmup summary "
            "trading_date=%s "
            "total_instruments=%s "
            "success=%s failure=%s",
            trading_date.isoformat(),
            total_instruments,
            success_count,
            failure_count,
        )

        return results

    def _initialize_one(
        self,
        token: str,
        contract: dict,
        trading_date: date,
    ) -> dict:
        instrument_key = contract["instrument_key"]

        client, api = build_history_client(token)

        try:
            logger.info(
                "Historical fetch started " "instrument=%s",
                instrument_key,
            )

            historical = fetch_history(
                api,
                instrument_key,
                trading_date,
            )

            logger.info(
                "Historical fetch completed " "instrument=%s candle_count=%s",
                instrument_key,
                len(historical),
            )

            logger.info(
                "Intraday fetch started " "instrument=%s",
                instrument_key,
            )

            intraday = fetch_intraday(
                api,
                instrument_key,
                trading_date,
            )

            logger.info(
                "Intraday fetch completed " "instrument=%s candle_count=%s",
                instrument_key,
                len(intraday),
            )

            merged = {
                candle["timestamp"]: candle
                for candle in historical
                if candle.get("timestamp")
            }

            merged.update(
                {
                    candle["timestamp"]: candle
                    for candle in intraday
                    if candle.get("timestamp")
                }
            )

            ordered_candles = sorted(
                merged.values(),
                key=lambda item: str(item.get("timestamp") or ""),
            )

            logger.info(
                "EMA calculation started "
                "instrument=%s "
                "historical_candles=%s "
                "intraday_candles=%s "
                "merged_candles=%s",
                instrument_key,
                len(historical),
                len(intraday),
                len(ordered_candles),
            )

            calculated, state = calculate_sequence(ordered_candles)

            if state is None:
                raise RuntimeError("No candles available for " "EMA initialization")

            historical_crosses: list[dict] = []
            intraday_crosses: list[dict] = []
            invalid_cross_count = 0

            for candle in calculated:
                if not candle.get("cross_type"):
                    continue

                candle_timestamp = parse_timestamp(candle.get("timestamp"))

                if candle_timestamp is None:
                    invalid_cross_count += 1

                    logger.warning(
                        "Crossover skipped because "
                        "timestamp is invalid "
                        "instrument=%s timestamp=%s",
                        instrument_key,
                        candle.get("timestamp"),
                    )
                    continue

                candle_date = candle_timestamp.date()

                if candle_date < trading_date:
                    historical_crosses.append(candle)

                elif candle_date == trading_date:
                    intraday_crosses.append(candle)

                else:
                    invalid_cross_count += 1

                    logger.warning(
                        "Future crossover skipped "
                        "instrument=%s "
                        "candle_date=%s "
                        "trading_date=%s",
                        instrument_key,
                        candle_date.isoformat(),
                        trading_date.isoformat(),
                    )

            save_historical_crosses(
                contract,
                historical_crosses,
            )

            save_intraday_crosses(
                contract,
                trading_date,
                intraday_crosses,
            )

            save_state(
                contract,
                trading_date,
                state,
            )

            total_crosses = len(historical_crosses) + len(intraday_crosses)

            logger.info(
                "EMA calculation completed "
                "instrument=%s "
                "calculated_candles=%s "
                "total_crosses=%s "
                "historical_crosses=%s "
                "intraday_crosses=%s "
                "invalid_crosses=%s "
                "ema_fast=%s "
                "ema_slow=%s "
                "last_processed_timestamp=%s",
                instrument_key,
                len(calculated),
                total_crosses,
                len(historical_crosses),
                len(intraday_crosses),
                invalid_cross_count,
                state.get("ema_9"),
                state.get("ema_21"),
                state.get("last_processed_timestamp"),
            )

            return state

        finally:
            close_method = getattr(
                client,
                "close",
                None,
            )

            if callable(close_method):
                try:
                    close_method()
                except Exception:
                    logger.exception(
                        "Failed to close history " "client instrument=%s",
                        instrument_key,
                    )

    def restore_or_initialize(
        self,
        trading_date: date,
    ) -> list:
        missing: list[dict] = []
        restored_contracts: list[dict] = []

        for contract in self.contracts:
            instrument_key = contract["instrument_key"]

            state = load_state(
                contract,
                trading_date,
            )

            cross_files_ready = crossover_files_exist(
                contract,
                trading_date,
            )

            if state is not None and cross_files_ready:
                self.states[instrument_key] = state

                restored_contracts.append(contract)

                logger.info(
                    "EMA state restored "
                    "instrument=%s "
                    "last_processed_timestamp=%s "
                    "crossover_files_ready=true",
                    instrument_key,
                    state.get("last_processed_timestamp"),
                )

                continue

            missing.append(contract)

            reason = (
                "ema_state_not_available"
                if state is None
                else ("crossover_files_not_available")
            )

            logger.info(
                "EMA instrument requires " "initialization " "instrument=%s reason=%s",
                instrument_key,
                reason,
            )

        logger.info(
            "EMA state restore summary "
            "trading_date=%s "
            "total_instruments=%s "
            "restored=%s missing=%s",
            trading_date.isoformat(),
            len(self.contracts),
            len(restored_contracts),
            len(missing),
        )

        initialization_results: list[dict] = []

        if missing:
            original_contracts = self.contracts

            self.contracts = missing

            try:
                initialization_results = self.initialize(trading_date)
            finally:
                self.contracts = original_contracts

        if restored_contracts:
            self._reconcile_contracts(
                trading_date,
                restored_contracts,
            )

        return initialization_results

    def reconcile_all(
        self,
        trading_date: date,
    ) -> None:
        self._reconcile_contracts(
            trading_date,
            self.contracts,
        )

    def _reconcile_contracts(
        self,
        trading_date: date,
        contracts: list[dict],
    ) -> None:
        if not getattr(
            config,
            "EMA_RECONCILIATION_ENABLED",
            True,
        ):
            logger.info("Intraday reconciliation skipped " "reason=disabled")
            return

        token = self.token_service.get_access_token()

        total_instruments = len(contracts)
        success_count = 0
        failure_count = 0
        skipped_count = 0

        logger.info(
            "Intraday reconciliation started "
            "trading_date=%s "
            "total_instruments=%s",
            trading_date.isoformat(),
            total_instruments,
        )

        for contract in contracts:
            instrument_key = contract["instrument_key"]

            state = self.states.get(instrument_key)

            if not state:
                skipped_count += 1

                logger.warning(
                    "Intraday reconciliation skipped "
                    "instrument=%s "
                    "reason=state_not_available",
                    instrument_key,
                )
                continue

            client, api = build_history_client(token)

            try:
                candles = fetch_intraday(
                    api,
                    instrument_key,
                    trading_date,
                )

                candles = sorted(
                    candles,
                    key=lambda item: str(item.get("timestamp") or ""),
                )

                logger.info(
                    "Reconciliation intraday "
                    "fetch completed "
                    "instrument=%s "
                    "candle_count=%s",
                    instrument_key,
                    len(candles),
                )

                processed_count = 0
                instrument_skipped_count = 0
                crossover_count = 0
                broadcast_count = 0

                for candle in candles:
                    result = self.process_candle(
                        contract,
                        trading_date,
                        candle,
                    )

                    if result.get("status") == "success":
                        processed_count += 1

                        if result.get("cross_type"):
                            crossover_count += 1

                        if result.get("websocket_queued"):
                            broadcast_count += 1
                    else:
                        instrument_skipped_count += 1

                success_count += 1

                logger.info(
                    "Intraday reconciliation "
                    "completed "
                    "instrument=%s "
                    "fetched_candles=%s "
                    "processed_candles=%s "
                    "skipped_candles=%s "
                    "crossovers=%s "
                    "websocket_events=%s",
                    instrument_key,
                    len(candles),
                    processed_count,
                    instrument_skipped_count,
                    crossover_count,
                    broadcast_count,
                )

            except Exception as ex:
                failure_count += 1

                logger.exception(
                    "Intraday reconciliation failed " "instrument=%s",
                    instrument_key,
                )

                self.notifications.notify_error(
                    title=("EMA Reconciliation Failed"),
                    message=(
                        f"Instrument: "
                        f"{instrument_key}\n"
                        f"Trading date: "
                        f"{trading_date.isoformat()}\n"
                        f"Error: {ex}"
                    ),
                    source=("ema_runtime.reconcile_all"),
                )

            finally:
                close_method = getattr(
                    client,
                    "close",
                    None,
                )

                if callable(close_method):
                    try:
                        close_method()
                    except Exception:
                        logger.exception(
                            "Failed to close " "reconciliation client " "instrument=%s",
                            instrument_key,
                        )

        logger.info(
            "Intraday reconciliation summary "
            "trading_date=%s "
            "total_instruments=%s "
            "success=%s failure=%s skipped=%s",
            trading_date.isoformat(),
            total_instruments,
            success_count,
            failure_count,
            skipped_count,
        )

    def _next_event_sequence(
        self,
    ) -> int:
        with self._event_sequence_lock:
            self._event_sequence += 1
            return self._event_sequence

    def _handle_broadcast_future(
        self,
        future: Future,
        *,
        instrument_key: str,
        timestamp: str | None,
    ) -> None:
        try:
            result = future.result()

            logger.debug(
                "EMA WebSocket broadcast completed "
                "instrument=%s timestamp=%s "
                "result=%s",
                instrument_key,
                timestamp,
                result,
            )

        except Exception:
            logger.exception(
                "EMA WebSocket broadcast failed " "instrument=%s timestamp=%s",
                instrument_key,
                timestamp,
            )

    def _handle_broadcast_task(
        self,
        task: asyncio.Task,
        *,
        instrument_key: str,
        timestamp: str | None,
    ) -> None:
        try:
            result = task.result()

            logger.debug(
                "EMA WebSocket broadcast completed "
                "instrument=%s timestamp=%s "
                "result=%s",
                instrument_key,
                timestamp,
                result,
            )

        except asyncio.CancelledError:
            logger.debug(
                "EMA WebSocket broadcast cancelled " "instrument=%s timestamp=%s",
                instrument_key,
                timestamp,
            )

        except Exception:
            logger.exception(
                "EMA WebSocket broadcast failed " "instrument=%s timestamp=%s",
                instrument_key,
                timestamp,
            )

    def _broadcast_ema_event(
        self,
        contract: dict,
        candle: dict,
    ) -> bool:
        if not getattr(
            config,
            "EMA_EVENTS_ENABLED",
            True,
        ):
            return False

        if not getattr(
            config,
            "EMA_WEBSOCKET_ENABLED",
            True,
        ):
            return False

        websocket_manager = self.websocket_manager

        if websocket_manager is None:
            logger.debug("EMA WebSocket event skipped " "reason=manager_not_available")
            return False

        instrument_key = str(contract.get("instrument_key") or "").strip()

        if not instrument_key:
            logger.warning(
                "EMA WebSocket event skipped " "reason=instrument_key_not_available"
            )
            return False

        if getattr(
            config,
            "EMA_BROADCAST_CROSSOVERS_ONLY",
            False,
        ) and not candle.get("cross_type"):
            return False

        sequence = self._next_event_sequence()

        event = EmaEvent.from_candle(
            candle=candle,
            contract=contract,
            mode="live",
            source=(candle.get("source") or "intraday"),
            sequence=sequence,
        )

        broadcast_method = (
            getattr(
                websocket_manager,
                "broadcast_instrument",
                None,
            )
            or getattr(
                websocket_manager,
                "broadcast_to_instrument",
                None,
            )
            or getattr(
                websocket_manager,
                "publish",
                None,
            )
        )

        use_instrument_argument = True

        if not callable(broadcast_method):
            broadcast_method = getattr(
                websocket_manager,
                "broadcast_event",
                None,
            )

            use_instrument_argument = False

        if not callable(broadcast_method):
            broadcast_method = getattr(
                websocket_manager,
                "broadcast",
                None,
            )

            use_instrument_argument = False

        if not callable(broadcast_method):
            logger.warning(
                "EMA WebSocket event skipped " "reason=broadcast_method_not_available"
            )
            return False

        try:
            if use_instrument_argument:
                result = broadcast_method(
                    instrument_key,
                    event,
                )
            else:
                result = broadcast_method(event)

        except Exception:
            logger.exception(
                "EMA WebSocket broadcast "
                "invocation failed "
                "instrument=%s timestamp=%s",
                instrument_key,
                candle.get("timestamp"),
            )
            return False

        if not asyncio.iscoroutine(result):
            return True

        timestamp = candle.get("timestamp")

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is not None and running_loop.is_running():
            task = running_loop.create_task(result)

            task.add_done_callback(
                lambda completed_task: (
                    self._handle_broadcast_task(
                        completed_task,
                        instrument_key=(instrument_key),
                        timestamp=timestamp,
                    )
                )
            )

            return True

        target_loop = self.websocket_loop

        if (
            target_loop is not None
            and target_loop.is_running()
            and not target_loop.is_closed()
        ):
            future = asyncio.run_coroutine_threadsafe(
                result,
                target_loop,
            )

            future.add_done_callback(
                lambda completed_future: (
                    self._handle_broadcast_future(
                        completed_future,
                        instrument_key=(instrument_key),
                        timestamp=timestamp,
                    )
                )
            )

            return True

        result.close()

        logger.warning(
            "EMA WebSocket event skipped "
            "reason=event_loop_not_available "
            "instrument=%s timestamp=%s",
            instrument_key,
            timestamp,
        )

        return False

    def process_candle(
        self,
        contract: dict,
        trading_date: date,
        candle: dict,
    ) -> dict:
        instrument_key = contract["instrument_key"]

        instrument_lock = self.locks.get(instrument_key)

        if instrument_lock is None:
            return {
                "status": "skipped",
                "reason": ("instrument_lock_not_available"),
                "instrument_key": instrument_key,
            }

        with instrument_lock:
            state = self.states.get(instrument_key)

            if state is None:
                return {
                    "status": "skipped",
                    "reason": ("ema_state_not_available"),
                    "instrument_key": (instrument_key),
                }

            current = parse_timestamp(candle.get("timestamp"))

            previous = parse_timestamp(state.get("last_processed_timestamp"))

            if current is None:
                return {
                    "status": "skipped",
                    "reason": ("invalid_candle_timestamp"),
                    "instrument_key": (instrument_key),
                }

            if current.date() != trading_date:
                return {
                    "status": "skipped",
                    "reason": ("candle_trading_date_mismatch"),
                    "instrument_key": (instrument_key),
                    "candle_timestamp": (current.isoformat()),
                }

            if previous is not None and current <= previous:
                return {
                    "status": "skipped",
                    "reason": ("duplicate_or_old_candle"),
                    "instrument_key": (instrument_key),
                    "candle_timestamp": (current.isoformat()),
                }

            if previous is not None and current - previous > timedelta(minutes=1):
                logger.warning(
                    "Candle gap detected " "instrument=%s " "previous=%s current=%s",
                    instrument_key,
                    previous.isoformat(),
                    current.isoformat(),
                )

            enriched, new_state = update_state(
                state,
                candle,
            )

            self.states[instrument_key] = new_state

            save_state(
                contract,
                trading_date,
                new_state,
            )

            append_completed_candle(
                contract,
                trading_date,
                enriched,
            )

            cross_type = enriched.get("cross_type")

            crossover_saved = False

            if cross_type:
                crossover_saved = append_intraday_crossover(
                    contract,
                    trading_date,
                    enriched,
                )

                logger.info(
                    "EMA crossover "
                    "instrument=%s "
                    "type=%s timestamp=%s "
                    "close=%s ema_fast=%s "
                    "ema_slow=%s saved=%s",
                    instrument_key,
                    cross_type,
                    enriched.get("timestamp"),
                    enriched.get("close"),
                    enriched.get("ema_9"),
                    enriched.get("ema_21"),
                    crossover_saved,
                )

                if crossover_saved:
                    self.notifications.notify_crossover(
                        contract=contract,
                        candle=enriched,
                        trading_date=trading_date,
                    )
                else:
                    logger.info(
                        "EMA crossover notification "
                        "skipped because crossover "
                        "was not newly persisted "
                        "instrument=%s "
                        "timestamp=%s type=%s",
                        instrument_key,
                        enriched.get("timestamp"),
                        cross_type,
                    )

            websocket_queued = self._broadcast_ema_event(
                contract,
                enriched,
            )

            return {
                "status": "success",
                "instrument_key": instrument_key,
                "candle_timestamp": (enriched["timestamp"]),
                "cross_type": cross_type,
                "crossover_saved": (crossover_saved),
                "websocket_queued": (websocket_queued),
                "ema_9": enriched.get("ema_9"),
                "ema_21": enriched.get("ema_21"),
                "close": enriched.get("close"),
            }

    def poll_completed_candles(
        self,
        trading_date: date,
    ) -> dict:
        poll_started_at = datetime.now(config.MARKET_TIMEZONE)

        total_instruments = len(self.contracts)

        outcomes = {
            contract["instrument_key"]: {
                "status": "skipped",
                "reason": "quote_not_returned",
            }
            for contract in self.contracts
        }

        if not getattr(
            config,
            "EMA_LIVE_PROCESSING_ENABLED",
            True,
        ):
            poll_completed_at = datetime.now(config.MARKET_TIMEZONE)

            return {
                "status": "skipped",
                "reason": ("live_processing_disabled"),
                "trading_date": (trading_date.isoformat()),
                "interval": config.OHLC_INTERVAL,
                "poll_started_at": (poll_started_at.isoformat()),
                "poll_completed_at": (poll_completed_at.isoformat()),
                "total_instruments": (total_instruments),
                "processed_instruments": 0,
                "successful_instruments": 0,
                "failed_instruments": 0,
                "skipped_instruments": (total_instruments),
                "results": outcomes,
            }

        if not self.quote_api:
            poll_completed_at = datetime.now(config.MARKET_TIMEZONE)

            logger.warning(
                "One-minute candle processing "
                "skipped total_instruments=%s "
                "reason=quote_api_not_initialized",
                total_instruments,
            )

            return {
                "status": "skipped",
                "reason": ("quote_api_not_initialized"),
                "trading_date": (trading_date.isoformat()),
                "interval": config.OHLC_INTERVAL,
                "poll_started_at": (poll_started_at.isoformat()),
                "poll_completed_at": (poll_completed_at.isoformat()),
                "total_instruments": (total_instruments),
                "processed_instruments": 0,
                "successful_instruments": 0,
                "failed_instruments": 0,
                "skipped_instruments": (total_instruments),
                "results": outcomes,
            }

        logger.info(
            "One-minute candle polling started "
            "trading_date=%s interval=%s "
            "total_instruments=%s",
            trading_date.isoformat(),
            config.OHLC_INTERVAL,
            total_instruments,
        )

        lookup = {contract["instrument_key"]: contract for contract in self.contracts}

        for batch in chunks(
            self.contracts,
            config.OHLC_BATCH_SIZE,
        ):
            batch_keys = [contract["instrument_key"] for contract in batch]

            symbols = ",".join(batch_keys)

            try:
                response = call_with_retry(
                    "ohlc_v3",
                    lambda: (
                        self.quote_api.get_market_quote_ohlc(
                            config.OHLC_INTERVAL,
                            instrument_key=symbols,
                        )
                    ),
                )

                response_payload = object_to_dict(response)

                data = response_payload.get(
                    "data",
                    {},
                )

                if not isinstance(data, dict):
                    for instrument_key in batch_keys:
                        outcomes[instrument_key] = {
                            "status": "failed",
                            "reason": ("invalid_ohlc_response"),
                        }

                    logger.error(
                        "OHLC batch returned " "invalid data " "batch_instruments=%s",
                        len(batch_keys),
                    )
                    continue

                for quote_value in data.values():
                    quote = object_to_dict(quote_value)

                    instrument_key = str(
                        quote.get("instrument_token")
                        or quote.get("instrument_key")
                        or ""
                    ).strip()

                    contract = lookup.get(instrument_key)

                    if not contract:
                        logger.warning(
                            "OHLC quote skipped "
                            "because instrument was "
                            "not found instrument=%s",
                            instrument_key,
                        )
                        continue

                    previous_ohlc = object_to_dict(quote.get("prev_ohlc"))

                    if not previous_ohlc:
                        outcomes[instrument_key] = {
                            "status": "skipped",
                            "reason": ("previous_ohlc_" "not_available"),
                        }
                        continue

                    candle = normalize_candle(
                        previous_ohlc,
                        "ohlc_prev",
                    )

                    if candle is None:
                        outcomes[instrument_key] = {
                            "status": "skipped",
                            "reason": ("invalid_previous_ohlc"),
                        }
                        continue

                    try:
                        outcomes[instrument_key] = self.process_candle(
                            contract,
                            trading_date,
                            candle,
                        )

                    except Exception as ex:
                        outcomes[instrument_key] = {
                            "status": "failed",
                            "reason": ("candle_processing_" "failed"),
                            "error": str(ex),
                        }

                        logger.exception(
                            "One-minute candle " "processing failed " "instrument=%s",
                            instrument_key,
                        )

                        self.notifications.notify_error(
                            title=("Candle Processing " "Failed"),
                            message=(
                                f"Instrument: "
                                f"{instrument_key}\n"
                                f"Trading date: "
                                f"{trading_date.isoformat()}\n"
                                f"Error: {ex}"
                            ),
                            source=("ema_runtime." "process_candle"),
                            instrument_key=(instrument_key),
                        )

            except Exception as ex:
                logger.exception(
                    "OHLC batch fetch failed " "batch_size=%s",
                    len(batch_keys),
                )

                for instrument_key in batch_keys:
                    outcomes[instrument_key] = {
                        "status": "failed",
                        "reason": ("ohlc_batch_fetch_failed"),
                        "error": str(ex),
                    }

                self.notifications.notify_error(
                    title=("OHLC Batch Fetch Failed"),
                    message=(
                        f"Batch size: "
                        f"{len(batch_keys)}\n"
                        f"Instruments: "
                        f"{', '.join(batch_keys)}\n"
                        f"Error: {ex}"
                    ),
                    source=("ema_runtime." "poll_completed_candles"),
                )

        successful_instruments = sum(
            result.get("status") == "success" for result in outcomes.values()
        )

        failed_instruments = sum(
            result.get("status") == "failed" for result in outcomes.values()
        )

        skipped_instruments = sum(
            result.get("status") == "skipped" for result in outcomes.values()
        )

        crossover_count = sum(
            bool(result.get("crossover_saved"))
            for result in outcomes.values()
            if isinstance(result, dict)
        )

        websocket_event_count = sum(
            bool(result.get("websocket_queued"))
            for result in outcomes.values()
            if isinstance(result, dict)
        )

        poll_completed_at = datetime.now(config.MARKET_TIMEZONE)

        summary = {
            "status": ("success" if failed_instruments == 0 else "partial_failure"),
            "trading_date": (trading_date.isoformat()),
            "interval": config.OHLC_INTERVAL,
            "poll_started_at": (poll_started_at.isoformat()),
            "poll_completed_at": (poll_completed_at.isoformat()),
            "total_instruments": (total_instruments),
            "processed_instruments": (successful_instruments),
            "successful_instruments": (successful_instruments),
            "failed_instruments": (failed_instruments),
            "skipped_instruments": (skipped_instruments),
            "new_crossovers": (crossover_count),
            "websocket_events_queued": (websocket_event_count),
            "results": outcomes,
        }

        logger.info(
            "One-minute candle processing "
            "completed processed=%s "
            "out_of=%s success=%s "
            "failure=%s skipped=%s "
            "new_crossovers=%s "
            "websocket_events=%s "
            "interval=%s started_at=%s "
            "completed_at=%s",
            successful_instruments,
            total_instruments,
            successful_instruments,
            failed_instruments,
            skipped_instruments,
            crossover_count,
            websocket_event_count,
            config.OHLC_INTERVAL,
            poll_started_at.isoformat(),
            poll_completed_at.isoformat(),
        )

        return summary
