import inspect
import time
from datetime import datetime
from threading import Event, Lock

from core import config
from core.logger import get_logger
from services.contracts import fetch_and_select
from services.notification_service import NotificationService
from utils.json_store import read_json, write_json_atomic

logger = get_logger(__file__)


class Scheduler:
    def __init__(
        self, token_service, runtime, telegram_service=None, notification_service=None
    ):
        self.token_service = token_service
        self.runtime = runtime

        # Backward compatibility with the existing Telegram service.
        self.telegram = telegram_service

        # Centralized notification service.
        self.notifications = notification_service or NotificationService(
            telegram_service=telegram_service
        )

        self.state_path = config.RUNTIME_ROOT / "service_state.json"

        self.last_poll_minute = None
        self.stop_event = Event()
        self.refresh_lock = Lock()

        # Tracks whether we have attempted a startup bootstrap in this process.
        self._startup_bootstrap_attempted = False

    def _call_callable_safely(self, fn, **kwargs):
        """
        Safely invoke a method by mapping kwargs to its explicit parameter signature,
        falling back to positional args or filtering unsupported keywords.
        """
        try:
            sig = inspect.signature(fn)
            params = sig.parameters

            # If method accepts **kwargs, pass everything as-is
            if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
                return fn(**kwargs)

            # Map arguments based on signature variations
            call_args = {}
            for name, param in params.items():
                if name in kwargs:
                    call_args[name] = kwargs[name]
                elif name in ("event", "type") and "event_type" in kwargs:
                    call_args[name] = kwargs["event_type"]

            # If all parameters matched keyword args, invoke
            if call_args:
                return fn(**call_args)

            # Fallback to positional invocation if keywords don't match signature parameters
            positional_vals = list(kwargs.values())
            req_params_count = len(
                [
                    p
                    for p in params.values()
                    if p.default == inspect.Parameter.empty
                    and p.kind
                    in (
                        inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    )
                ]
            )
            return fn(*positional_vals[:req_params_count])
        except Exception:
            # Final attempt: direct call with keyword parameters
            return fn(**kwargs)

    def _notify(
        self,
        event_type: str,
        title: str,
        message: str,
        source: str,
        silent: bool = False,
    ) -> bool:
        """
        Send notifications through the centralized notification service.

        Supports different NotificationService implementations through
        safe method detection while preserving backward compatibility.
        """
        try:
            notify_event = getattr(self.notifications, "notify_event", None)
            if callable(notify_event):
                result = self._call_callable_safely(
                    notify_event,
                    event_type=event_type,
                    title=title,
                    message=message,
                    source=source,
                    silent=silent,
                )
                return result is not False

            if event_type == "error":
                notify_error = getattr(self.notifications, "notify_error", None)
                if callable(notify_error):
                    result = self._call_callable_safely(
                        notify_error,
                        title=title,
                        message=message,
                        source=source,
                        silent=silent,
                    )
                    return result is not False

            if event_type == "hard_refresh":
                notify_refresh = getattr(
                    self.notifications, "notify_hard_refresh", None
                )
                if callable(notify_refresh):
                    result = self._call_callable_safely(
                        notify_refresh,
                        title=title,
                        message=message,
                        source=source,
                        silent=silent,
                    )
                    return result is not False

            notify_lifecycle = getattr(self.notifications, "notify_lifecycle", None)
            if callable(notify_lifecycle):
                result = self._call_callable_safely(
                    notify_lifecycle,
                    title=title,
                    message=message,
                    source=source,
                    silent=silent,
                )
                return result is not False

            send_method = getattr(self.notifications, "send", None)
            if callable(send_method):
                result = self._call_callable_safely(
                    send_method,
                    message=message,
                    silent=silent,
                )
                return result is not False

            # Final backward-compatible fallback.
            if self.telegram:
                return bool(self.telegram.send(message, silent=silent))

            logger.warning(
                "Notification skipped because no supported notification method is available event_type=%s",
                event_type,
            )

            return False

        except Exception:
            logger.exception(
                "Notification failed event_type=%s source=%s", event_type, source
            )
            return False

    def is_weekday(self, now: datetime) -> bool:
        """
        Return True when the current day is Monday to Friday.
        """
        return now.weekday() < 5

    def selected_for_today(self, today) -> list:
        """
        Load contracts selected for the current trading date.
        """
        payload = read_json(config.RUNTIME_ROOT / "selected_contracts.json", {})

        generated_date = str(payload.get("generated_at") or "")[:10]
        contracts = payload.get("data", [])

        if generated_date == today.isoformat() and isinstance(contracts, list):
            return contracts

        return []

    def _contracts_file_exists(self) -> bool:
        """
        Return True when the full contracts file exists on disk.
        """
        try:
            return config.CONTRACTS_FILE.exists()
        except Exception:
            logger.exception("Failed to check contracts file existence")
            return False

    def _bootstrap_contracts_only(
        self,
        now: datetime,
        trigger: str = "startup_bootstrap",
    ) -> dict:
        """
        Fetch and persist the nearest NIFTY option contracts without running
        the heavy EMA warmup.

        This is used so that candle discovery and candle routes work
        immediately on startup, regardless of the current time.
        """
        if not self.refresh_lock.acquire(blocking=False):
            logger.warning(
                "Bootstrap ignored because another refresh is already running trigger=%s",
                trigger,
            )
            return {
                "status": "already_running",
                "message": "A refresh is already in progress",
            }

        started_at = datetime.now(config.MARKET_TIMEZONE)

        try:
            logger.info(
                "Bootstrap contracts fetch started trigger=%s time=%s",
                trigger,
                started_at.isoformat(),
            )

            token = self.token_service.get_access_token(force_refresh=True)

            payload, contracts = fetch_and_select(token)

            self.runtime.replace_contracts(contracts)

            completed_at = datetime.now(config.MARKET_TIMEZONE)

            summary = {
                "status": "success",
                "trigger": trigger,
                "bootstrap": True,
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "nearest_expiry": payload.get("nearest_expiry"),
                "test_flag": config.TEST_FLAG,
                "selected_contract_count": len(contracts),
                "contracts_file": str(config.CONTRACTS_FILE),
            }

            logger.info(
                "Bootstrap contracts fetch completed trigger=%s selected=%s nearest_expiry=%s contracts_file=%s",
                trigger,
                len(contracts),
                payload.get("nearest_expiry"),
                config.CONTRACTS_FILE,
            )

            return summary

        except Exception as ex:
            logger.exception("Bootstrap contracts fetch failed trigger=%s", trigger)

            self._notify(
                event_type="error",
                title="NIFTY EMA Bootstrap Failed",
                message=(
                    "NIFTY EMA bootstrap contracts fetch failed\n"
                    f"Trigger: {trigger}\n"
                    f"Error: {ex}"
                ),
                source="scheduler._bootstrap_contracts_only",
            )

            return {
                "status": "failed",
                "trigger": trigger,
                "bootstrap": True,
                "error": str(ex),
            }

        finally:
            self.refresh_lock.release()

    def refresh_day(
        self, now: datetime, force: bool = False, trigger: str = "scheduler"
    ) -> dict:
        """
        Refresh the selected contracts and initialize EMA states.

        If the current day has already been refreshed and force=False,
        restore today's selected contracts and saved EMA states.
        """
        if not self.refresh_lock.acquire(blocking=False):
            logger.warning(
                "Refresh request ignored because another refresh is already running trigger=%s",
                trigger,
            )

            return {
                "status": "already_running",
                "message": "A refresh is already in progress",
            }

        started_at = datetime.now(config.MARKET_TIMEZONE)

        self._notify(
            event_type="hard_refresh",
            title="NIFTY EMA Hard Refresh Started",
            message=(
                "NIFTY EMA hard refresh started\n"
                f"Trigger: {trigger}\n"
                f"Time: {started_at.isoformat()}"
            ),
            source="scheduler.refresh_day",
        )

        try:
            state = read_json(self.state_path, {})

            already_refreshed = (
                state.get("last_daily_refresh_date") == now.date().isoformat()
            )

            if already_refreshed and not force:
                contracts = self.selected_for_today(now.date())

                if contracts:
                    self.runtime.replace_contracts(contracts)
                    results = self.runtime.restore_or_initialize(now.date())

                    restored_summary = {
                        "status": "restored",
                        "trigger": trigger,
                        "selected_contract_count": len(contracts),
                        "results": results,
                    }

                    logger.info(
                        "Daily runtime state restored trigger=%s selected_instruments=%s",
                        trigger,
                        len(contracts),
                    )

                    self._notify(
                        event_type="hard_refresh",
                        title="NIFTY EMA State Restored",
                        message=(
                            "NIFTY EMA state restored\n"
                            f"Trigger: {trigger}\n"
                            f"Selected: {len(contracts)}"
                        ),
                        source="scheduler.refresh_day",
                    )

                    return restored_summary

            token = self.token_service.get_access_token(force_refresh=True)

            payload, contracts = fetch_and_select(token)

            self.runtime.replace_contracts(contracts)
            results = self.runtime.initialize(now.date())

            completed_at = datetime.now(config.MARKET_TIMEZONE)

            initialized_count = sum(
                result.get("status") == "success" for result in results
            )
            failed_count = sum(result.get("status") == "failed" for result in results)

            summary = {
                "status": "success",
                "trigger": trigger,
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "last_daily_refresh_date": now.date().isoformat(),
                "last_daily_refresh_at": completed_at.isoformat(),
                "nearest_expiry": payload.get("nearest_expiry"),
                "test_flag": config.TEST_FLAG,
                "selected_contract_count": len(contracts),
                "initialized_contract_count": initialized_count,
                "failed_initialization_count": failed_count,
                "results": results,
            }

            write_json_atomic(self.state_path, summary)

            logger.info(
                "Daily refresh completed trigger=%s selected=%s initialized=%s failed=%s",
                trigger,
                len(contracts),
                initialized_count,
                failed_count,
            )

            self._notify(
                event_type="hard_refresh",
                title="NIFTY EMA Refresh Completed",
                message=(
                    "NIFTY EMA refresh completed\n"
                    f"Trigger: {trigger}\n"
                    f"Selected: {len(contracts)}\n"
                    f"Ready: {initialized_count}\n"
                    f"Failed: {failed_count}"
                ),
                source="scheduler.refresh_day",
            )

            return summary

        except Exception as ex:
            logger.exception("Refresh failed trigger=%s", trigger)

            self._notify(
                event_type="error",
                title="NIFTY EMA Refresh Failed",
                message=(
                    "NIFTY EMA refresh failed\n" f"Trigger: {trigger}\n" f"Error: {ex}"
                ),
                source="scheduler.refresh_day",
            )

            raise

        finally:
            self.refresh_lock.release()

    def should_poll(self, now: datetime) -> bool:
        """
        Determine whether the completed one-minute candle
        should be polled at the current time.
        """
        if not self.is_weekday(now):
            return False

        if not (config.MARKET_OPEN_TIME < now.time() <= config.MARKET_CLOSE_TIME):
            return False

        if now.second < config.LIVE_POLL_DELAY_SECONDS:
            return False

        minute_key = now.strftime("%Y-%m-%dT%H:%M")

        if minute_key == self.last_poll_minute:
            return False

        self.last_poll_minute = minute_key

        return True

    def save_poll_summary(self, poll_summary: dict) -> None:
        """
        Persist the latest OHLC polling summary.
        """
        state = read_json(self.state_path, {})

        state.update(
            {
                "status": "running",
                "last_ohlc_poll_at": (
                    poll_summary.get("poll_completed_at")
                    or datetime.now(config.MARKET_TIMEZONE).isoformat()
                ),
                "last_ohlc_poll_summary": {
                    "status": poll_summary.get("status"),
                    "interval": poll_summary.get("interval"),
                    "poll_started_at": poll_summary.get("poll_started_at"),
                    "poll_completed_at": poll_summary.get("poll_completed_at"),
                    "total_instruments": poll_summary.get("total_instruments", 0),
                    "processed_instruments": poll_summary.get(
                        "processed_instruments", 0
                    ),
                    "successful_instruments": poll_summary.get(
                        "successful_instruments", 0
                    ),
                    "failed_instruments": poll_summary.get("failed_instruments", 0),
                    "skipped_instruments": poll_summary.get("skipped_instruments", 0),
                },
            }
        )

        write_json_atomic(self.state_path, state)

    def log_poll_summary(self, poll_summary: dict, poll_time: datetime) -> None:
        """
        Log a readable summary after every live candle cycle.
        """
        total_instruments = int(poll_summary.get("total_instruments", 0))
        processed_instruments = int(
            poll_summary.get(
                "processed_instruments", poll_summary.get("successful_instruments", 0)
            )
        )
        successful_instruments = int(poll_summary.get("successful_instruments", 0))
        failed_instruments = int(poll_summary.get("failed_instruments", 0))
        skipped_instruments = int(poll_summary.get("skipped_instruments", 0))
        interval = poll_summary.get("interval", config.OHLC_INTERVAL)
        candle_minute = poll_time.strftime("%Y-%m-%d %H:%M")

        logger.info(
            "Live EMA candle cycle completed candle_minute=%s interval=%s processed=%s out_of=%s success=%s failure=%s skipped=%s",
            candle_minute,
            interval,
            processed_instruments,
            total_instruments,
            successful_instruments,
            failed_instruments,
            skipped_instruments,
        )

    def stop(self) -> None:
        """
        Stop the scheduler loop.
        """
        self.stop_event.set()

    def _handle_startup_bootstrap(self, now: datetime) -> None:
        """
        On the first loop iteration, ensure contract discovery works.

        Behavior:
          - If the full contracts file is missing, fetch and persist it now,
            regardless of the current time.
          - If the file exists but nothing is loaded into the runtime, try to
            restore the current day's selection from disk without hitting Upstox.
        """
        if self._startup_bootstrap_attempted:
            return

        self._startup_bootstrap_attempted = True

        try:
            contracts_file_exists = self._contracts_file_exists()

            logger.info(
                "Startup bootstrap evaluation contracts_file=%s exists=%s runtime_contracts=%s",
                config.CONTRACTS_FILE,
                contracts_file_exists,
                len(self.runtime.contracts),
            )

            if not contracts_file_exists:
                self._bootstrap_contracts_only(
                    now,
                    trigger="startup_bootstrap_missing_file",
                )
                return

            if not self.runtime.contracts:
                contracts = self.selected_for_today(now.date())

                if contracts:
                    self.runtime.replace_contracts(contracts)

                    logger.info(
                        "Startup bootstrap restored selected contracts from disk count=%s",
                        len(contracts),
                    )
                else:
                    logger.info(
                        "Startup bootstrap found contracts file but no selection for today; "
                        "leaving runtime empty until scheduler window opens"
                    )

        except Exception:
            logger.exception("Startup bootstrap failed")

    def run(self) -> None:
        """
        Start the scheduler loop.

        Responsibilities:
        - Startup contract bootstrap.
        - Daily refresh.
        - Runtime restoration.
        - One-minute OHLC polling.
        - Poll summary persistence.
        - Error notifications.
        """
        logger.info("Scheduler started")

        self._notify(
            event_type="lifecycle",
            title="NIFTY EMA Service Started",
            message="NIFTY EMA crossover service started",
            source="scheduler.run",
        )

        while not self.stop_event.is_set():
            now = datetime.now(config.MARKET_TIMEZONE)

            try:
                # Ensure contract discovery works even outside market hours.
                self._handle_startup_bootstrap(now)

                if self.is_weekday(now):
                    state = read_json(self.state_path, {})

                    needs_refresh = (
                        state.get("last_daily_refresh_date") != now.date().isoformat()
                    )

                    if needs_refresh and now.time() >= config.DAILY_REFRESH_TIME:
                        self.refresh_day(now, trigger="daily_scheduler")

                    elif (
                        not self.runtime.contracts
                        and now.time() >= config.DAILY_REFRESH_TIME
                    ):
                        self.refresh_day(now, trigger="restart_restore")

                    if self.runtime.contracts and self.should_poll(now):
                        poll_time = datetime.now(config.MARKET_TIMEZONE)

                        poll_summary = self.runtime.poll_completed_candles(now.date())

                        if not isinstance(poll_summary, dict):
                            logger.warning(
                                "Live EMA polling returned an invalid summary candle_minute=%s",
                                poll_time.strftime("%Y-%m-%d %H:%M"),
                            )

                            poll_summary = {
                                "status": "invalid_summary",
                                "interval": config.OHLC_INTERVAL,
                                "poll_started_at": poll_time.isoformat(),
                                "poll_completed_at": datetime.now(
                                    config.MARKET_TIMEZONE
                                ).isoformat(),
                                "total_instruments": len(self.runtime.contracts),
                                "processed_instruments": 0,
                                "successful_instruments": 0,
                                "failed_instruments": 0,
                                "skipped_instruments": len(self.runtime.contracts),
                            }

                        self.log_poll_summary(poll_summary, poll_time)
                        self.save_poll_summary(poll_summary)

            except Exception as ex:
                logger.exception("Scheduler cycle failed")

                self._notify(
                    event_type="error",
                    title="NIFTY EMA Scheduler Error",
                    message=("NIFTY EMA scheduler error\n" f"{ex}"),
                    source="scheduler.run",
                )

            self.stop_event.wait(config.SCHEDULER_IDLE_SECONDS)

        logger.info("Scheduler stopped")

        self._notify(
            event_type="lifecycle",
            title="NIFTY EMA Service Stopped",
            message="NIFTY EMA crossover service stopped",
            source="scheduler.run",
        )