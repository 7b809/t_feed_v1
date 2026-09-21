import time
from datetime import datetime
from threading import Event, Lock

from core import config
from core.logger import get_logger
from services.contracts import fetch_and_select
from utils.json_store import read_json, write_json_atomic

logger = get_logger(__file__)


class Scheduler:
    def __init__(
        self,
        token_service,
        runtime,
        telegram_service,
    ):
        self.token_service = token_service
        self.runtime = runtime
        self.telegram = telegram_service
        self.state_path = (
            config.RUNTIME_ROOT / "service_state.json"
        )
        self.last_poll_minute = None
        self.stop_event = Event()
        self.refresh_lock = Lock()

    def is_weekday(self, now: datetime) -> bool:
        return now.weekday() < 5

    def selected_for_today(self, today) -> list:
        payload = read_json(
            config.RUNTIME_ROOT / "selected_contracts.json",
            {},
        )

        generated_date = str(
            payload.get("generated_at") or ""
        )[:10]

        contracts = payload.get("data", [])

        if (
            generated_date == today.isoformat()
            and isinstance(contracts, list)
        ):
            return contracts

        return []

    def refresh_day(
        self,
        now: datetime,
        force: bool = False,
        trigger: str = "scheduler",
    ) -> dict:
        if not self.refresh_lock.acquire(blocking=False):
            logger.warning(
                "Refresh request ignored because another refresh "
                "is already running trigger=%s",
                trigger,
            )

            return {
                "status": "already_running",
                "message": "A refresh is already in progress",
            }

        started_at = datetime.now(
            config.MARKET_TIMEZONE
        )

        try:
            self.telegram.send(
                "NIFTY EMA hard refresh started\n"
                f"Trigger: {trigger}\n"
                f"Time: {started_at.isoformat()}"
            )

            state = read_json(
                self.state_path,
                {},
            )

            already_refreshed = (
                state.get("last_daily_refresh_date")
                == now.date().isoformat()
            )

            if already_refreshed and not force:
                contracts = self.selected_for_today(
                    now.date()
                )

                if contracts:
                    self.runtime.replace_contracts(
                        contracts
                    )

                    results = (
                        self.runtime.restore_or_initialize(
                            now.date()
                        )
                    )

                    restored_summary = {
                        "status": "restored",
                        "selected_contract_count": len(
                            contracts
                        ),
                        "results": results,
                    }

                    logger.info(
                        "Daily runtime state restored "
                        "trigger=%s selected_instruments=%s",
                        trigger,
                        len(contracts),
                    )

                    return restored_summary

            token = self.token_service.get_access_token(
                force_refresh=True
            )

            payload, contracts = fetch_and_select(
                token
            )

            self.runtime.replace_contracts(
                contracts
            )

            results = self.runtime.initialize(
                now.date()
            )

            completed_at = datetime.now(
                config.MARKET_TIMEZONE
            )

            initialized_count = sum(
                result.get("status") == "success"
                for result in results
            )

            failed_count = sum(
                result.get("status") == "failed"
                for result in results
            )

            summary = {
                "status": "success",
                "trigger": trigger,
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "last_daily_refresh_date": (
                    now.date().isoformat()
                ),
                "last_daily_refresh_at": (
                    completed_at.isoformat()
                ),
                "nearest_expiry": payload.get(
                    "nearest_expiry"
                ),
                "test_flag": config.TEST_FLAG,
                "selected_contract_count": len(
                    contracts
                ),
                "initialized_contract_count": (
                    initialized_count
                ),
                "failed_initialization_count": (
                    failed_count
                ),
                "results": results,
            }

            write_json_atomic(
                self.state_path,
                summary,
            )

            logger.info(
                "Daily refresh completed trigger=%s "
                "selected=%s initialized=%s failed=%s",
                trigger,
                len(contracts),
                initialized_count,
                failed_count,
            )

            self.telegram.send(
                "NIFTY EMA refresh completed\n"
                f"Trigger: {trigger}\n"
                f"Selected: {len(contracts)}\n"
                f"Ready: {initialized_count}\n"
                f"Failed: {failed_count}"
            )

            return summary

        except Exception as ex:
            logger.exception(
                "Refresh failed trigger=%s",
                trigger,
            )

            self.telegram.send(
                "NIFTY EMA refresh failed\n"
                f"Trigger: {trigger}\n"
                f"Error: {ex}"
            )

            raise

        finally:
            self.refresh_lock.release()

    def should_poll(
        self,
        now: datetime,
    ) -> bool:
        if not self.is_weekday(now):
            return False

        if not (
            config.MARKET_OPEN_TIME
            < now.time()
            <= config.MARKET_CLOSE_TIME
        ):
            return False

        if (
            now.second
            < config.LIVE_POLL_DELAY_SECONDS
        ):
            return False

        minute_key = now.strftime(
            "%Y-%m-%dT%H:%M"
        )

        if minute_key == self.last_poll_minute:
            return False

        self.last_poll_minute = minute_key
        return True

    def save_poll_summary(
        self,
        poll_summary: dict,
    ) -> None:
        state = read_json(
            self.state_path,
            {},
        )

        state.update(
            {
                "status": "running",
                "last_ohlc_poll_at": (
                    poll_summary.get(
                        "poll_completed_at"
                    )
                    or datetime.now(
                        config.MARKET_TIMEZONE
                    ).isoformat()
                ),
                "last_ohlc_poll_summary": {
                    "status": poll_summary.get(
                        "status"
                    ),
                    "interval": poll_summary.get(
                        "interval"
                    ),
                    "poll_started_at": (
                        poll_summary.get(
                            "poll_started_at"
                        )
                    ),
                    "poll_completed_at": (
                        poll_summary.get(
                            "poll_completed_at"
                        )
                    ),
                    "total_instruments": (
                        poll_summary.get(
                            "total_instruments",
                            0,
                        )
                    ),
                    "processed_instruments": (
                        poll_summary.get(
                            "processed_instruments",
                            0,
                        )
                    ),
                    "successful_instruments": (
                        poll_summary.get(
                            "successful_instruments",
                            0,
                        )
                    ),
                    "failed_instruments": (
                        poll_summary.get(
                            "failed_instruments",
                            0,
                        )
                    ),
                    "skipped_instruments": (
                        poll_summary.get(
                            "skipped_instruments",
                            0,
                        )
                    ),
                },
            }
        )

        write_json_atomic(
            self.state_path,
            state,
        )

    def log_poll_summary(
        self,
        poll_summary: dict,
        poll_time: datetime,
    ) -> None:
        total_instruments = int(
            poll_summary.get(
                "total_instruments",
                0,
            )
        )

        processed_instruments = int(
            poll_summary.get(
                "processed_instruments",
                poll_summary.get(
                    "successful_instruments",
                    0,
                ),
            )
        )

        successful_instruments = int(
            poll_summary.get(
                "successful_instruments",
                0,
            )
        )

        failed_instruments = int(
            poll_summary.get(
                "failed_instruments",
                0,
            )
        )

        skipped_instruments = int(
            poll_summary.get(
                "skipped_instruments",
                0,
            )
        )

        interval = poll_summary.get(
            "interval",
            config.OHLC_INTERVAL,
        )

        candle_minute = poll_time.strftime(
            "%Y-%m-%d %H:%M"
        )

        logger.info(
            "Live EMA candle cycle completed "
            "candle_minute=%s interval=%s "
            "processed=%s out_of=%s "
            "success=%s failure=%s skipped=%s",
            candle_minute,
            interval,
            processed_instruments,
            total_instruments,
            successful_instruments,
            failed_instruments,
            skipped_instruments,
        )

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        logger.info("Scheduler started")

        self.telegram.send(
            "NIFTY EMA crossover service started"
        )

        while not self.stop_event.is_set():
            now = datetime.now(
                config.MARKET_TIMEZONE
            )

            try:
                if self.is_weekday(now):
                    state = read_json(
                        self.state_path,
                        {},
                    )

                    needs_refresh = (
                        state.get(
                            "last_daily_refresh_date"
                        )
                        != now.date().isoformat()
                    )

                    if (
                        needs_refresh
                        and now.time()
                        >= config.DAILY_REFRESH_TIME
                    ):
                        self.refresh_day(
                            now,
                            trigger="daily_scheduler",
                        )

                    elif (
                        not self.runtime.contracts
                        and now.time()
                        >= config.DAILY_REFRESH_TIME
                    ):
                        self.refresh_day(
                            now,
                            trigger="restart_restore",
                        )

                    if (
                        self.runtime.contracts
                        and self.should_poll(now)
                    ):
                        poll_time = datetime.now(
                            config.MARKET_TIMEZONE
                        )

                        poll_summary = (
                            self.runtime
                            .poll_completed_candles(
                                now.date()
                            )
                        )

                        if not isinstance(
                            poll_summary,
                            dict,
                        ):
                            logger.warning(
                                "Live EMA polling returned an "
                                "invalid summary candle_minute=%s",
                                poll_time.strftime(
                                    "%Y-%m-%d %H:%M"
                                ),
                            )

                            poll_summary = {
                                "status": (
                                    "invalid_summary"
                                ),
                                "interval": (
                                    config.OHLC_INTERVAL
                                ),
                                "poll_started_at": (
                                    poll_time.isoformat()
                                ),
                                "poll_completed_at": (
                                    datetime.now(
                                        config.MARKET_TIMEZONE
                                    ).isoformat()
                                ),
                                "total_instruments": len(
                                    self.runtime.contracts
                                ),
                                "processed_instruments": 0,
                                "successful_instruments": 0,
                                "failed_instruments": 0,
                                "skipped_instruments": len(
                                    self.runtime.contracts
                                ),
                            }

                        self.log_poll_summary(
                            poll_summary,
                            poll_time,
                        )

                        self.save_poll_summary(
                            poll_summary
                        )

            except Exception as ex:
                logger.exception(
                    "Scheduler cycle failed"
                )

                self.telegram.send(
                    "NIFTY EMA scheduler error\n"
                    f"{ex}"
                )

            self.stop_event.wait(
                config.SCHEDULER_IDLE_SECONDS
            )

        logger.info("Scheduler stopped")