import asyncio
import json
import threading
from concurrent.futures import Future
from typing import Any

import httpx
import upstox_client

from app.config import settings
from app.database import Database
from app.logger import get_logger
from app.websockets import WebSocketHub

logger = get_logger(__file__)


class UpstoxMarketService:
    OPTION_CONTRACT_URL = "https://api.upstox.com/v2/option/contract"

    INSTRUMENT_SEARCH_URL = "https://api.upstox.com/v2/instruments/search"

    VALID_OPTION_TYPES = {
        "CE",
        "PE",
    }

    VALID_SUBSCRIPTION_MODES = {
        "ltpc",
        "full",
        "option_greeks",
        "full_d30",
    }

    def __init__(
        self,
        database: Database,
        hub: WebSocketHub,
    ) -> None:
        self.database = database
        self.hub = hub
        self.streamer: Any | None = None
        self.access_token = ""
        self.connected = False
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.RLock()

        logger.info("UpstoxMarketService initialized")

    def start(
        self,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        logger.info("Starting Upstox market service")

        try:
            if loop.is_closed():
                raise RuntimeError("Cannot start service with a closed event loop")

            self.loop = loop

            result = self.refresh_token_and_reconnect()

            logger.info(
                "Upstox market service start requested | "
                "status=%s | token_changed=%s",
                result.get("status"),
                result.get("token_changed"),
            )

        except Exception:
            self.connected = False

            logger.exception("Failed to start Upstox market service")

            raise

    def _validate_mode(
        self,
        mode: str,
        description: str,
    ) -> str:
        normalized_mode = str(mode or "").strip()

        if normalized_mode not in self.VALID_SUBSCRIPTION_MODES:
            raise ValueError(
                f"Invalid {description}: {normalized_mode}. "
                "Allowed modes: ltpc, full, "
                "option_greeks, full_d30"
            )

        return normalized_mode

    def _build_streamer(self) -> None:
        logger.info("Building Upstox market data streamer")

        try:
            if not self.access_token:
                raise RuntimeError("Upstox access token is empty")

            configuration = upstox_client.Configuration()

            configuration.access_token = self.access_token

            api_client = upstox_client.ApiClient(configuration)

            self.streamer = upstox_client.MarketDataStreamerV3(api_client)

            self.streamer.on(
                "open",
                self._on_open,
            )

            self.streamer.on(
                "message",
                self._on_message,
            )

            self.streamer.on(
                "error",
                self._on_error,
            )

            self.streamer.on(
                "close",
                self._on_close,
            )

            self.streamer.auto_reconnect(
                True,
                5,
                20,
            )

            logger.info(
                "Upstox market data streamer built successfully | "
                "auto_reconnect=true"
            )

        except Exception:
            self.streamer = None

            logger.exception("Failed to build Upstox market data streamer")

            raise

    def fetch_option_contracts(
        self,
        instrument_key: str,
        expiry_date: str,
    ) -> list[dict[str, Any]]:
        if not self.access_token:
            raise RuntimeError(
                "Cannot fetch option contracts because " "the access token is empty"
            )

        logger.info(
            "Fetching option contracts | " "underlying=%s | expiry=%s",
            instrument_key,
            expiry_date,
        )

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": (f"Bearer {self.access_token}"),
        }

        params = {
            "instrument_key": instrument_key,
            "expiry_date": expiry_date,
        }

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(
                    self.OPTION_CONTRACT_URL,
                    params=params,
                    headers=headers,
                )

            response.raise_for_status()

            try:
                payload = response.json()

            except ValueError as exc:
                raise RuntimeError(
                    "Upstox option contracts API " "returned invalid JSON"
                ) from exc

            if payload.get("status") != "success":
                raise RuntimeError(
                    "Upstox option contracts API " f"returned failure: {payload}"
                )

            contracts = payload.get(
                "data",
                [],
            )

            if not isinstance(
                contracts,
                list,
            ):
                raise RuntimeError("Option contracts response data " "is not a list")

            logger.info(
                "Option contracts fetched | " "underlying=%s | expiry=%s | count=%s",
                instrument_key,
                expiry_date,
                len(contracts),
            )

            return contracts

        except httpx.TimeoutException:
            logger.exception(
                "Option contracts request timed out | " "underlying=%s | expiry=%s",
                instrument_key,
                expiry_date,
            )

            raise

        except httpx.HTTPStatusError as exc:
            logger.error(
                "Option contracts HTTP error | "
                "underlying=%s | expiry=%s | "
                "status_code=%s | response=%s",
                instrument_key,
                expiry_date,
                exc.response.status_code,
                exc.response.text[:1000],
                exc_info=True,
            )

            raise

        except httpx.RequestError:
            logger.exception(
                "Option contracts request failed | " "underlying=%s | expiry=%s",
                instrument_key,
                expiry_date,
            )

            raise

        except Exception:
            logger.exception(
                "Unexpected option contracts failure | " "underlying=%s | expiry=%s",
                instrument_key,
                expiry_date,
            )

            raise

    def filter_option_contracts(
        self,
        contracts: list[dict[str, Any]],
        start_range: float,
        end_range: float,
    ) -> list[dict[str, Any]]:
        selected_contracts: list[dict[str, Any]] = []

        for contract in contracts:
            instrument_key = contract.get("instrument_key")

            instrument_type = contract.get("instrument_type")

            strike_price = contract.get("strike_price")

            if not instrument_key:
                continue

            if instrument_type not in self.VALID_OPTION_TYPES:
                continue

            if strike_price is None:
                continue

            try:
                numeric_strike = float(strike_price)

            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring option contract with "
                    "invalid strike | instrument_key=%s | "
                    "strike_price=%s",
                    instrument_key,
                    strike_price,
                )

                continue

            if start_range <= numeric_strike <= end_range:
                selected_contracts.append(contract)

        selected_contracts.sort(
            key=lambda item: (
                float(
                    item.get(
                        "strike_price",
                        0,
                    )
                ),
                item.get(
                    "instrument_type",
                    "",
                ),
            )
        )

        return selected_contracts

    def prepare_startup_subscriptions(
        self,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        default_index_mode = self._validate_mode(
            settings.startup_index_subscription_mode,
            "startup index subscription mode",
        )

        default_option_mode = self._validate_mode(
            settings.startup_option_subscription_mode,
            "startup option subscription mode",
        )

        expiry_date = str(settings.startup_option_expiry).strip()

        if not expiry_date:
            raise ValueError("Startup option expiry is empty")

        desired_subscriptions: list[dict[str, Any]] = []

        underlying_results: list[dict[str, Any]] = []

        configured_underlyings: set[str] = set()

        for startup_item in settings.startup_subscribe_list:
            underlying_key = str(
                startup_item.get(
                    "instrument_name",
                    "",
                )
            ).strip()

            if not underlying_key:
                raise ValueError(
                    "Startup subscription configuration " "is missing instrument_name"
                )

            if underlying_key in configured_underlyings:
                logger.warning(
                    "Duplicate startup underlying ignored | " "underlying=%s",
                    underlying_key,
                )

                continue

            configured_underlyings.add(underlying_key)

            start_range = startup_item.get("start_range")

            end_range = startup_item.get("end_range")

            if start_range is None or end_range is None:
                raise ValueError(
                    "Startup subscription configuration "
                    f"for {underlying_key} is missing "
                    "start_range or end_range"
                )

            index_mode = self._validate_mode(
                startup_item.get(
                    "index_mode",
                    default_index_mode,
                ),
                ("index subscription mode for " f"{underlying_key}"),
            )

            option_mode = self._validate_mode(
                startup_item.get(
                    "option_mode",
                    default_option_mode,
                ),
                ("option subscription mode for " f"{underlying_key}"),
            )

            try:
                numeric_start_range = float(start_range)

                numeric_end_range = float(end_range)

            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Invalid startup strike range for " f"{underlying_key}"
                ) from exc

            if numeric_start_range > numeric_end_range:
                logger.warning(
                    "Startup strike range reversed | "
                    "underlying=%s | start_range=%s | "
                    "end_range=%s",
                    underlying_key,
                    numeric_start_range,
                    numeric_end_range,
                )

                (
                    numeric_start_range,
                    numeric_end_range,
                ) = (
                    numeric_end_range,
                    numeric_start_range,
                )

            contracts = self.fetch_option_contracts(
                instrument_key=underlying_key,
                expiry_date=expiry_date,
            )

            selected_contracts = self.filter_option_contracts(
                contracts=contracts,
                start_range=numeric_start_range,
                end_range=numeric_end_range,
            )

            desired_subscriptions.append(
                {
                    "instrument_key": (underlying_key),
                    "mode": index_mode,
                    "source": ("startup_underlying"),
                    "underlying_key": (underlying_key),
                }
            )

            ce_count = 0
            pe_count = 0

            for contract in selected_contracts:
                instrument_type = contract.get("instrument_type")

                if instrument_type == "CE":
                    ce_count += 1

                elif instrument_type == "PE":
                    pe_count += 1

                desired_subscriptions.append(
                    {
                        "instrument_key": contract["instrument_key"],
                        "mode": option_mode,
                        "source": ("startup_option"),
                        "underlying_key": (underlying_key),
                        "instrument_type": (instrument_type),
                        "strike_price": (contract.get("strike_price")),
                        "expiry": (contract.get("expiry")),
                        "trading_symbol": (contract.get("trading_symbol")),
                    }
                )

            underlying_results.append(
                {
                    "underlying_key": (underlying_key),
                    "index_mode": index_mode,
                    "option_mode": option_mode,
                    "start_range": (numeric_start_range),
                    "end_range": (numeric_end_range),
                    "expiry": expiry_date,
                    "call_contracts": ce_count,
                    "put_contracts": pe_count,
                    "total_option_contracts": len(selected_contracts),
                }
            )

            logger.info(
                "Startup option contracts selected | "
                "underlying=%s | expiry=%s | "
                "index_mode=%s | option_mode=%s | "
                "start_range=%s | end_range=%s | "
                "CE=%s | PE=%s | total=%s",
                underlying_key,
                expiry_date,
                index_mode,
                option_mode,
                numeric_start_range,
                numeric_end_range,
                ce_count,
                pe_count,
                len(selected_contracts),
            )

        return (
            desired_subscriptions,
            underlying_results,
        )

    def synchronize_startup_subscriptions(
        self,
    ) -> dict[str, Any]:
        logger.info("Startup subscription synchronization started")

        (
            desired_subscriptions,
            underlying_results,
        ) = self.prepare_startup_subscriptions()

        desired_by_key: dict[
            str,
            dict[str, Any],
        ] = {}

        for subscription in desired_subscriptions:
            instrument_key = subscription["instrument_key"]

            desired_by_key[instrument_key] = subscription

        for subscription in desired_by_key.values():
            self.database.upsert_managed_subscription(
                instrument_key=subscription["instrument_key"],
                mode=subscription["mode"],
                source=subscription["source"],
                underlying_key=subscription.get("underlying_key"),
                instrument_type=subscription.get("instrument_type"),
                strike_price=subscription.get("strike_price"),
                expiry=subscription.get("expiry"),
                trading_symbol=subscription.get("trading_symbol"),
            )

        desired_instrument_keys = set(desired_by_key.keys())

        stale_instrument_keys = self.database.remove_managed_not_in(
            desired_instrument_keys
        )

        index_subscription_count = sum(
            1
            for subscription in desired_by_key.values()
            if (subscription.get("source") == "startup_underlying")
        )

        option_subscription_count = sum(
            1
            for subscription in desired_by_key.values()
            if (subscription.get("source") == "startup_option")
        )

        result = {
            "expiry": (settings.startup_option_expiry),
            "default_index_mode": (settings.startup_index_subscription_mode),
            "default_option_mode": (settings.startup_option_subscription_mode),
            "underlyings": underlying_results,
            "index_subscription_count": (index_subscription_count),
            "option_subscription_count": (option_subscription_count),
            "desired_subscription_count": len(desired_instrument_keys),
            "stale_removed_count": len(stale_instrument_keys),
            "stale_removed": (stale_instrument_keys),
        }

        logger.info(
            "Startup subscription synchronization completed | "
            "index_count=%s | option_count=%s | "
            "desired_count=%s | stale_removed=%s",
            index_subscription_count,
            option_subscription_count,
            len(desired_instrument_keys),
            len(stale_instrument_keys),
        )

        return result

    def refresh_token_and_reconnect(
        self,
    ) -> dict[str, Any]:
        logger.info("Token refresh and streamer reconnection started")

        with self._lock:
            try:
                token = self.database.load_access_token()

                if not token:
                    raise RuntimeError(
                        "No Upstox access token " "found in the database"
                    )

                token_changed = token != self.access_token

                self.access_token = token

                logger.info(
                    "Access token loaded | " "token_changed=%s",
                    token_changed,
                )

                if self.streamer is not None:
                    logger.info("Disconnecting existing Upstox " "market data streamer")

                    try:
                        self.streamer.disconnect()

                        logger.info(
                            "Existing Upstox market data " "streamer disconnected"
                        )

                    except Exception:
                        logger.exception(
                            "Failed to disconnect existing "
                            "streamer; continuing with refresh"
                        )

                self.connected = False
                self.streamer = None

                synchronization_result = self.synchronize_startup_subscriptions()

                self._build_streamer()

                if self.streamer is None:
                    raise RuntimeError(
                        "Upstox market data streamer " "was not initialized"
                    )

                logger.info("Connecting Upstox market data streamer")

                self.streamer.connect()

                result = {
                    "status": "reconnecting",
                    "token_changed": token_changed,
                    "startup_subscriptions": (synchronization_result),
                }

                logger.info(
                    "Streamer reconnection requested | "
                    "token_changed=%s | "
                    "managed_subscription_count=%s",
                    token_changed,
                    synchronization_result.get("desired_subscription_count"),
                )

                return result

            except Exception:
                self.connected = False

                logger.exception("Token refresh and streamer " "reconnection failed")

                raise

    def _on_open(
        self,
        *args: Any,
    ) -> None:
        logger.info("Upstox streamer open event received")

        try:
            self.connected = True

            subscriptions = self.database.list_active()

            logger.info(
                "Restoring active subscriptions | " "count=%s",
                len(subscriptions),
            )

            if not subscriptions:
                logger.info("No active subscriptions to restore")

                return

            if self.streamer is None:
                raise RuntimeError(
                    "Cannot restore subscriptions " "because streamer is unavailable"
                )

            grouped: dict[
                str,
                list[str],
            ] = {}

            for item in subscriptions:
                instrument_key = str(
                    item.get(
                        "instrument_key",
                        "",
                    )
                ).strip()

                mode = str(
                    item.get(
                        "mode",
                        "",
                    )
                ).strip()

                if not instrument_key or not mode:
                    logger.warning(
                        "Skipping invalid subscription " "record | record=%s",
                        item,
                    )

                    continue

                try:
                    validated_mode = self._validate_mode(
                        mode,
                        ("stored subscription " "mode"),
                    )

                except ValueError:
                    logger.exception(
                        "Skipping subscription with " "invalid mode | record=%s",
                        item,
                    )

                    continue

                grouped.setdefault(
                    validated_mode,
                    [],
                ).append(instrument_key)

            for mode, instrument_keys in grouped.items():
                try:
                    self.streamer.subscribe(
                        instrument_keys,
                        mode,
                    )

                    logger.info(
                        "Subscriptions restored | " "mode=%s | count=%s",
                        mode,
                        len(instrument_keys),
                    )

                except Exception:
                    logger.exception(
                        "Failed to restore subscription " "group | mode=%s | count=%s",
                        mode,
                        len(instrument_keys),
                    )

            logger.info(
                "Active subscription restoration " "completed | groups=%s",
                len(grouped),
            )

        except Exception:
            self.connected = False

            logger.exception("Failed while processing Upstox " "streamer open event")

    def _on_message(
        self,
        message: Any,
    ) -> None:
        try:
            if isinstance(
                message,
                str,
            ):
                try:
                    payload = json.loads(message)

                except json.JSONDecodeError:
                    logger.warning(
                        "Received non-JSON string from "
                        "Upstox streamer | "
                        "message_length=%s",
                        len(message),
                    )

                    return

            else:
                payload = message

            if not isinstance(
                payload,
                dict,
            ):
                logger.warning(
                    "Ignoring unexpected Upstox " "message type | type=%s",
                    type(payload).__name__,
                )

                return

            feeds = payload.get(
                "feeds",
                {},
            )

            if not isinstance(
                feeds,
                dict,
            ):
                logger.warning(
                    "Ignoring message with invalid " "feeds value | type=%s",
                    type(feeds).__name__,
                )

                return

            if not feeds:
                return

            if self.loop is None:
                logger.error(
                    "Cannot broadcast market feeds " "because event loop is not set"
                )

                return

            if self.loop.is_closed():
                logger.error(
                    "Cannot broadcast market feeds " "because event loop is closed"
                )

                return

            logger.debug(
                "Market feed message received | " "instrument_count=%s",
                len(feeds),
            )

            for instrument_key, feed in feeds.items():
                event = {
                    "instrument_key": instrument_key,
                    "feed": feed,
                }

                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self.hub.broadcast(
                            instrument_key,
                            event,
                        ),
                        self.loop,
                    )

                    future.add_done_callback(
                        lambda completed_future, key=instrument_key: self._on_broadcast_complete(
                            key,
                            completed_future,
                        )
                    )

                except Exception:
                    logger.exception(
                        "Failed to schedule market feed "
                        "broadcast | instrument_key=%s",
                        instrument_key,
                    )

        except Exception:
            logger.exception("Failed to process Upstox " "market feed message")

    def _on_broadcast_complete(
        self,
        instrument_key: str,
        future: Future[Any],
    ) -> None:
        try:
            future.result()

        except asyncio.CancelledError:
            logger.debug(
                "Market feed broadcast cancelled | " "instrument_key=%s",
                instrument_key,
            )

        except Exception:
            logger.exception(
                "Market feed broadcast failed | " "instrument_key=%s",
                instrument_key,
            )

    def _on_error(
        self,
        error: Any,
    ) -> None:
        self.connected = False

        if isinstance(
            error,
            BaseException,
        ):
            logger.error(
                "Upstox streamer error | " "error_type=%s | error=%s",
                type(error).__name__,
                error,
                exc_info=(
                    type(error),
                    error,
                    error.__traceback__,
                ),
            )

        else:
            logger.error(
                "Upstox streamer error | error=%s",
                error,
            )

    def _on_close(
        self,
        *args: Any,
    ) -> None:
        self.connected = False

        logger.warning(
            "Upstox streamer connection closed | " "details=%s",
            args if args else "not provided",
        )

    def _require_streamer(
        self,
    ) -> Any:
        if self.streamer is None:
            raise RuntimeError("Upstox streamer is not initialized")

        return self.streamer

    def subscribe(
        self,
        instrument_keys: list[str],
        mode: str,
    ) -> dict[str, list[str]]:
        validated_mode = self._validate_mode(
            mode,
            "subscription mode",
        )

        normalized_keys = list(
            dict.fromkeys(
                str(key).strip() for key in instrument_keys if str(key).strip()
            )
        )

        logger.info(
            "Subscription operation started | " "requested_count=%s | mode=%s",
            len(normalized_keys),
            validated_mode,
        )

        with self._lock:
            try:
                streamer = self._require_streamer()

                active = {
                    item["instrument_key"]: item for item in self.database.list_active()
                }

                new_keys: list[str] = []
                unchanged: list[str] = []
                mode_changes: list[str] = []

                for key in normalized_keys:
                    if key not in active:
                        new_keys.append(key)

                    elif active[key]["mode"] == validated_mode:
                        unchanged.append(key)

                    else:
                        mode_changes.append(key)

                if new_keys:
                    streamer.subscribe(
                        new_keys,
                        validated_mode,
                    )

                    for key in new_keys:
                        self.database.upsert_active(
                            instrument_key=key,
                            mode=validated_mode,
                            source="manual",
                        )

                if mode_changes:
                    streamer.change_mode(
                        mode_changes,
                        validated_mode,
                    )

                    for key in mode_changes:
                        self.database.change_mode(
                            key,
                            validated_mode,
                        )

                result = {
                    "subscribed": new_keys,
                    "mode_changed": mode_changes,
                    "already_subscribed": unchanged,
                }

                logger.info(
                    "Subscription operation completed | "
                    "subscribed=%s | mode_changed=%s | "
                    "unchanged=%s",
                    len(new_keys),
                    len(mode_changes),
                    len(unchanged),
                )

                return result

            except Exception:
                logger.exception(
                    "Subscription operation failed | " "requested_count=%s | mode=%s",
                    len(normalized_keys),
                    validated_mode,
                )

                raise

    def unsubscribe(
        self,
        instrument_keys: list[str],
    ) -> dict[str, list[str]]:
        normalized_keys = list(
            dict.fromkeys(
                str(key).strip() for key in instrument_keys if str(key).strip()
            )
        )

        logger.info(
            "Unsubscribe operation started | " "requested_count=%s",
            len(normalized_keys),
        )

        with self._lock:
            try:
                streamer = self._require_streamer()

                active_keys = {
                    item["instrument_key"] for item in self.database.list_active()
                }

                removable = [key for key in normalized_keys if key in active_keys]

                missing = [key for key in normalized_keys if key not in active_keys]

                if removable:
                    streamer.unsubscribe(removable)

                    for key in removable:
                        self.database.remove_active(key)

                logger.info(
                    "Unsubscribe operation completed | "
                    "unsubscribed=%s | "
                    "not_subscribed=%s",
                    len(removable),
                    len(missing),
                )

                return {
                    "unsubscribed": removable,
                    "not_subscribed": missing,
                }

            except Exception:
                logger.exception(
                    "Unsubscribe operation failed | " "requested_count=%s",
                    len(normalized_keys),
                )

                raise

    def change_mode(
        self,
        instrument_keys: list[str],
        mode: str,
    ) -> dict[str, list[str]]:
        validated_mode = self._validate_mode(
            mode,
            "subscription mode",
        )

        normalized_keys = list(
            dict.fromkeys(
                str(key).strip() for key in instrument_keys if str(key).strip()
            )
        )

        logger.info(
            "Mode change operation started | " "requested_count=%s | mode=%s",
            len(normalized_keys),
            validated_mode,
        )

        with self._lock:
            try:
                streamer = self._require_streamer()

                active = {
                    item["instrument_key"]: item for item in self.database.list_active()
                }

                changeable = [
                    key
                    for key in normalized_keys
                    if (key in active and active[key]["mode"] != validated_mode)
                ]

                unchanged = [
                    key
                    for key in normalized_keys
                    if (key in active and active[key]["mode"] == validated_mode)
                ]

                missing = [key for key in normalized_keys if key not in active]

                if changeable:
                    streamer.change_mode(
                        changeable,
                        validated_mode,
                    )

                    for key in changeable:
                        self.database.change_mode(
                            key,
                            validated_mode,
                        )

                logger.info(
                    "Mode change operation completed | "
                    "changed=%s | unchanged=%s | "
                    "missing=%s | mode=%s",
                    len(changeable),
                    len(unchanged),
                    len(missing),
                    validated_mode,
                )

                return {
                    "mode_changed": changeable,
                    "unchanged": unchanged,
                    "not_subscribed": missing,
                }

            except Exception:
                logger.exception(
                    "Mode change operation failed | " "requested_count=%s | mode=%s",
                    len(normalized_keys),
                    validated_mode,
                )

                raise

    async def search_instruments(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        filtered_params = {
            key: value
            for key, value in params.items()
            if (value is not None and value != "")
        }

        logger.info(
            "Instrument search started | " "query=%s | page=%s | records=%s",
            filtered_params.get("query"),
            filtered_params.get("page_number"),
            filtered_params.get("records"),
        )

        try:
            if not self.access_token:
                raise RuntimeError(
                    "Cannot search instruments " "because access token is empty"
                )

            headers = {
                "Accept": "application/json",
                "Authorization": (f"Bearer {self.access_token}"),
            }

            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.get(
                    self.INSTRUMENT_SEARCH_URL,
                    params=filtered_params,
                    headers=headers,
                )

            response.raise_for_status()

            try:
                result = response.json()

            except ValueError as exc:
                raise RuntimeError(
                    "Upstox instrument search " "returned an invalid response"
                ) from exc

            logger.info(
                "Instrument search completed | " "query=%s | status_code=%s",
                filtered_params.get("query"),
                response.status_code,
            )

            return result

        except httpx.TimeoutException:
            logger.exception(
                "Upstox instrument search timed out | " "query=%s",
                filtered_params.get("query"),
            )

            raise

        except httpx.HTTPStatusError as exc:
            logger.error(
                "Upstox instrument search HTTP error | "
                "query=%s | status_code=%s | "
                "response=%s",
                filtered_params.get("query"),
                exc.response.status_code,
                exc.response.text[:1000],
                exc_info=True,
            )

            raise

        except httpx.RequestError:
            logger.exception(
                "Upstox instrument search request " "failed | query=%s",
                filtered_params.get("query"),
            )

            raise

        except Exception:
            logger.exception(
                "Unexpected instrument search failure | " "query=%s",
                filtered_params.get("query"),
            )

            raise

    def stop(self) -> None:
        logger.info("Stopping Upstox market service")

        with self._lock:
            self.connected = False

            if self.streamer is None:
                logger.info(
                    "Upstox market service already "
                    "stopped; streamer is not initialized"
                )

                return

            try:
                self.streamer.disconnect()

                logger.info("Upstox streamer disconnected successfully")

            except Exception:
                logger.exception(
                    "Failed to disconnect Upstox " "streamer during shutdown"
                )

            finally:
                self.streamer = None

                logger.info("Upstox market service stopped")
