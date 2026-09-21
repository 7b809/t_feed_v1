from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from threading import Lock

import upstox_client

from core import config
from core.logger import get_logger
from services.ema import calculate_sequence, normalize_candle, update_state
from services.history import build_history_client, fetch_history, fetch_intraday

from services.state import (
    append_completed_candle,
    append_crossover,
    load_state,
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
    def __init__(self, token_service, telegram_service=None):
        self.token_service = token_service
        self.telegram = telegram_service
        self.contracts: list[dict] = []
        self.states: dict[str, dict] = {}
        self.locks: dict[str, Lock] = {}
        self.quote_client = None
        self.quote_api = None

    def close(self) -> None:
        if self.quote_client:
            self.quote_client.close()
            self.quote_client = None
            self.quote_api = None

    def replace_contracts(self, contracts: list[dict]) -> None:
        self.close()

        self.contracts = list(contracts)
        self.states = {}
        self.locks = {
            row["instrument_key"]: Lock()
            for row in contracts
        }

        token = self.token_service.get_access_token()

        configuration = upstox_client.Configuration()
        configuration.access_token = token

        self.quote_client = upstox_client.ApiClient(configuration)
        self.quote_api = upstox_client.MarketQuoteV3Api(
            self.quote_client
        )

        logger.info(
            "Runtime contracts replaced total_instruments=%s",
            len(self.contracts),
        )

    def initialize(self, trading_date: date) -> list:
        token = self.token_service.get_access_token()
        results = []

        total_instruments = len(self.contracts)
        workers = min(
            config.MAX_WORKERS,
            total_instruments,
        ) if total_instruments else 0

        logger.info(
            "EMA warmup started trading_date=%s total_instruments=%s workers=%s",
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
                            "instrument_key": instrument_key,
                            "status": "success",
                        }
                    )

                    logger.info(
                        "Instrument warmup completed instrument=%s "
                        "status=success",
                        instrument_key,
                    )

                except Exception as ex:
                    logger.exception(
                        "Instrument warmup failed instrument=%s",
                        instrument_key,
                    )

                    results.append(
                        {
                            "instrument_key": instrument_key,
                            "status": "failed",
                            "error": str(ex),
                        }
                    )

        success_count = sum(
            result["status"] == "success"
            for result in results
        )
        failure_count = sum(
            result["status"] == "failed"
            for result in results
        )

        logger.info(
            "EMA warmup summary trading_date=%s "
            "total_instruments=%s success=%s failure=%s",
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
                "Historical fetch started instrument=%s",
                instrument_key,
            )

            historical = fetch_history(
                api,
                instrument_key,
                trading_date,
            )

            logger.info(
                "Historical fetch completed instrument=%s "
                "candle_count=%s",
                instrument_key,
                len(historical),
            )

            logger.info(
                "Intraday fetch started instrument=%s",
                instrument_key,
            )

            intraday = fetch_intraday(
                api,
                instrument_key,
                trading_date,
            )

            logger.info(
                "Intraday fetch completed instrument=%s "
                "candle_count=%s",
                instrument_key,
                len(intraday),
            )

            merged = {
                candle["timestamp"]: candle
                for candle in historical
            }

            merged.update(
                {
                    candle["timestamp"]: candle
                    for candle in intraday
                }
            )

            logger.info(
                "EMA calculation started instrument=%s "
                "historical_candles=%s intraday_candles=%s "
                "merged_candles=%s",
                instrument_key,
                len(historical),
                len(intraday),
                len(merged),
            )

            calculated, state = calculate_sequence(
                list(merged.values())
            )

            if state is None:
                raise RuntimeError(
                    "No candles available for EMA initialization"
                )

            crossover_count = 0

            for candle in calculated:
                if candle.get("cross_type"):
                    append_crossover(
                        contract,
                        trading_date,
                        candle,
                    )
                    crossover_count += 1

            save_state(
                contract,
                trading_date,
                state,
            )

            logger.info(
                "EMA calculation completed instrument=%s "
                "calculated_candles=%s crossovers=%s "
                "ema_fast=%s ema_slow=%s "
                "last_processed_timestamp=%s",
                instrument_key,
                len(calculated),
                crossover_count,
                state.get("ema_9"),
                state.get("ema_21"),
                state.get("last_processed_timestamp"),
            )

            return state

        finally:
            close_method = getattr(client, "close", None)

            if callable(close_method):
                close_method()

    def restore_or_initialize(
        self,
        trading_date: date,
    ) -> list:
        missing = []
        restored_count = 0

        for contract in self.contracts:
            instrument_key = contract["instrument_key"]
            state = load_state(
                contract,
                trading_date,
            )

            if state:
                self.states[instrument_key] = state
                restored_count += 1

                logger.info(
                    "EMA state restored instrument=%s "
                    "last_processed_timestamp=%s",
                    instrument_key,
                    state.get("last_processed_timestamp"),
                )
            else:
                missing.append(contract)

        logger.info(
            "EMA state restore summary trading_date=%s "
            "total_instruments=%s restored=%s missing=%s",
            trading_date.isoformat(),
            len(self.contracts),
            restored_count,
            len(missing),
        )

        if not missing:
            self.reconcile_all(trading_date)
            return []

        original_contracts = self.contracts
        self.contracts = missing

        try:
            return self.initialize(trading_date)
        finally:
            self.contracts = original_contracts

    def reconcile_all(
        self,
        trading_date: date,
    ) -> None:
        token = self.token_service.get_access_token()

        total_instruments = len(self.contracts)
        success_count = 0
        failure_count = 0
        skipped_count = 0

        logger.info(
            "Intraday reconciliation started trading_date=%s "
            "total_instruments=%s",
            trading_date.isoformat(),
            total_instruments,
        )

        for contract in self.contracts:
            instrument_key = contract["instrument_key"]
            state = self.states.get(instrument_key)

            if not state:
                skipped_count += 1

                logger.warning(
                    "Intraday reconciliation skipped instrument=%s "
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

                logger.info(
                    "Reconciliation intraday fetch completed "
                    "instrument=%s candle_count=%s",
                    instrument_key,
                    len(candles),
                )

                processed_count = 0
                instrument_skipped_count = 0

                for candle in candles:
                    result = self.process_candle(
                        contract,
                        trading_date,
                        candle,
                    )

                    if result["status"] == "success":
                        processed_count += 1
                    else:
                        instrument_skipped_count += 1

                success_count += 1

                logger.info(
                    "Intraday reconciliation completed "
                    "instrument=%s fetched_candles=%s "
                    "processed_candles=%s skipped_candles=%s",
                    instrument_key,
                    len(candles),
                    processed_count,
                    instrument_skipped_count,
                )

            except Exception:
                failure_count += 1

                logger.exception(
                    "Intraday reconciliation failed instrument=%s",
                    instrument_key,
                )

            finally:
                close_method = getattr(client, "close", None)

                if callable(close_method):
                    close_method()

        logger.info(
            "Intraday reconciliation summary trading_date=%s "
            "total_instruments=%s success=%s failure=%s skipped=%s",
            trading_date.isoformat(),
            total_instruments,
            success_count,
            failure_count,
            skipped_count,
        )

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
                "reason": "instrument_lock_not_available",
                "instrument_key": instrument_key,
            }

        with instrument_lock:
            state = self.states.get(instrument_key)

            if state is None:
                return {
                    "status": "skipped",
                    "reason": "ema_state_not_available",
                    "instrument_key": instrument_key,
                }

            current = parse_timestamp(
                candle.get("timestamp")
            )
            previous = parse_timestamp(
                state.get("last_processed_timestamp")
            )

            if current is None:
                return {
                    "status": "skipped",
                    "reason": "invalid_candle_timestamp",
                    "instrument_key": instrument_key,
                }

            if previous is not None and current <= previous:
                return {
                    "status": "skipped",
                    "reason": "duplicate_or_old_candle",
                    "instrument_key": instrument_key,
                    "candle_timestamp": current.isoformat(),
                }

            if (
                previous is not None
                and current - previous > timedelta(minutes=1)
            ):
                logger.warning(
                    "Candle gap detected instrument=%s "
                    "previous=%s current=%s",
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

            if enriched.get("cross_type"):
                append_crossover(
                    contract,
                    trading_date,
                    enriched,
                )

                logger.info(
                    "EMA crossover instrument=%s type=%s "
                    "timestamp=%s close=%s ema_fast=%s ema_slow=%s",
                    instrument_key,
                    enriched["cross_type"],
                    enriched["timestamp"],
                    enriched.get("close"),
                    enriched.get("ema_9"),
                    enriched.get("ema_21"),
                )

                if self.telegram:
                    self.telegram.send(
                        f"EMA {enriched['cross_type'].upper()} CROSS\n"
                        f"{contract.get('trading_symbol')}\n"
                        f"Close: {enriched.get('close')}\n"
                        f"EMA 9: {enriched.get('ema_9')}\n"
                        f"EMA 21: {enriched.get('ema_21')}\n"
                        f"Time: {enriched.get('timestamp')}"
                    )

            return {
                "status": "success",
                "instrument_key": instrument_key,
                "candle_timestamp": enriched["timestamp"],
                "cross_type": enriched.get("cross_type"),
            }

    def poll_completed_candles(
        self,
        trading_date: date,
    ) -> dict:
        poll_started_at = datetime.now(
            config.MARKET_TIMEZONE
        )
        total_instruments = len(self.contracts)

        outcomes = {
            contract["instrument_key"]: {
                "status": "skipped",
                "reason": "quote_not_returned",
            }
            for contract in self.contracts
        }

        if not self.quote_api:
            summary = {
                "status": "skipped",
                "trading_date": trading_date.isoformat(),
                "poll_started_at": poll_started_at.isoformat(),
                "poll_completed_at": datetime.now(
                    config.MARKET_TIMEZONE
                ).isoformat(),
                "total_instruments": total_instruments,
                "successful_instruments": 0,
                "failed_instruments": 0,
                "skipped_instruments": total_instruments,
                "results": outcomes,
            }

            logger.warning(
                "One-minute candle processing skipped "
                "total_instruments=%s success=0 failure=0 skipped=%s "
                "reason=quote_api_not_initialized",
                total_instruments,
                total_instruments,
            )

            return summary

        logger.info(
            "One-minute candle polling started trading_date=%s "
            "interval=%s total_instruments=%s",
            trading_date.isoformat(),
            config.OHLC_INTERVAL,
            total_instruments,
        )

        lookup = {
            row["instrument_key"]: row
            for row in self.contracts
        }

        for batch in chunks(
            self.contracts,
            config.OHLC_BATCH_SIZE,
        ):
            batch_keys = [
                row["instrument_key"]
                for row in batch
            ]
            symbols = ",".join(batch_keys)

            try:
                response = call_with_retry(
                    "ohlc_v3",
                    lambda: self.quote_api.get_market_quote_ohlc(
                        config.OHLC_INTERVAL,
                        instrument_key=symbols,
                    ),
                )

                data = object_to_dict(response).get(
                    "data",
                    {},
                )

                if not isinstance(data, dict):
                    for instrument_key in batch_keys:
                        outcomes[instrument_key] = {
                            "status": "failed",
                            "reason": "invalid_ohlc_response",
                        }

                    logger.error(
                        "OHLC batch returned invalid data "
                        "batch_instruments=%s",
                        len(batch_keys),
                    )
                    continue

                for quote in data.values():
                    quote = object_to_dict(quote)

                    instrument_key = str(
                        quote.get("instrument_token")
                        or quote.get("instrument_key")
                        or ""
                    ).strip()

                    contract = lookup.get(instrument_key)

                    if not contract:
                        logger.warning(
                            "OHLC quote skipped because instrument "
                            "was not found instrument=%s",
                            instrument_key,
                        )
                        continue

                    previous = object_to_dict(
                        quote.get("prev_ohlc")
                    )

                    if not previous:
                        outcomes[instrument_key] = {
                            "status": "skipped",
                            "reason": "previous_ohlc_not_available",
                        }
                        continue

                    candle = normalize_candle(
                        previous,
                        "ohlc_prev",
                    )

                    if candle is None:
                        outcomes[instrument_key] = {
                            "status": "skipped",
                            "reason": "invalid_previous_ohlc",
                        }
                        continue

                    try:
                        outcomes[instrument_key] = (
                            self.process_candle(
                                contract,
                                trading_date,
                                candle,
                            )
                        )

                    except Exception as ex:
                        outcomes[instrument_key] = {
                            "status": "failed",
                            "reason": "candle_processing_failed",
                            "error": str(ex),
                        }

                        logger.exception(
                            "One-minute candle processing failed "
                            "instrument=%s",
                            instrument_key,
                        )

            except Exception as ex:
                logger.exception(
                    "OHLC batch fetch failed batch_size=%s",
                    len(batch_keys),
                )

                for instrument_key in batch_keys:
                    outcomes[instrument_key] = {
                        "status": "failed",
                        "reason": "ohlc_batch_fetch_failed",
                        "error": str(ex),
                    }

        successful_instruments = sum(
            result.get("status") == "success"
            for result in outcomes.values()
        )
        failed_instruments = sum(
            result.get("status") == "failed"
            for result in outcomes.values()
        )
        skipped_instruments = sum(
            result.get("status") == "skipped"
            for result in outcomes.values()
        )

        poll_completed_at = datetime.now(
            config.MARKET_TIMEZONE
        )

        summary = {
            "status": (
                "success"
                if failed_instruments == 0
                else "partial_failure"
            ),
            "trading_date": trading_date.isoformat(),
            "interval": config.OHLC_INTERVAL,
            "poll_started_at": poll_started_at.isoformat(),
            "poll_completed_at": poll_completed_at.isoformat(),
            "total_instruments": total_instruments,
            "processed_instruments": successful_instruments,
            "successful_instruments": successful_instruments,
            "failed_instruments": failed_instruments,
            "skipped_instruments": skipped_instruments,
            "results": outcomes,
        }

        logger.info(
            "One-minute candle processing completed "
            "processed=%s out_of=%s success=%s failure=%s skipped=%s "
            "interval=%s started_at=%s completed_at=%s",
            successful_instruments,
            total_instruments,
            successful_instruments,
            failed_instruments,
            skipped_instruments,
            config.OHLC_INTERVAL,
            poll_started_at.isoformat(),
            poll_completed_at.isoformat(),
        )

        return summary