from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from typing import Any

import upstox_client
from upstox_client.rest import ApiException

from core.config import settings
from core.database import get_upstox_tokens_collection
from core.logger import get_logger

logger = get_logger("token_service")


class TokenService:
    def __init__(self) -> None:
        self._access_token: str | None = None

        self._token_updated_at: str | None = None
        self._last_validated_at: str | None = None
        self._last_validation_status: str | None = None

        self._cache_refreshed_at: datetime | None = None
        self._last_refresh_error: str | None = None

        self._profile_user_id: str | None = None
        self._profile_user_name: str | None = None
        self._profile_broker: str | None = None

        self._refresh_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

        self._lock = threading.RLock()

    async def start(self) -> bool:
        if self._refresh_task is not None and not self._refresh_task.done():
            logger.info("Upstox token refresh service already running.")
            return self.has_access_token()

        token_loaded = await self.refresh_access_token()

        self._stop_event = asyncio.Event()

        self._refresh_task = asyncio.create_task(
            self._refresh_loop(),
            name="upstox-token-refresh",
        )

        logger.info(
            "Upstox token refresh service started "
            "interval_seconds=%s token_available=%s",
            settings.upstox_token_refresh_interval_seconds,
            token_loaded,
        )

        return token_loaded

    async def stop(self) -> None:
        if self._refresh_task is None:
            return

        if self._stop_event is not None:
            self._stop_event.set()

        self._refresh_task.cancel()

        try:
            await self._refresh_task
        except asyncio.CancelledError:
            pass

        self._refresh_task = None
        self._stop_event = None

        logger.info("Upstox token refresh service stopped.")

    async def _refresh_loop(self) -> None:
        while True:
            try:
                interval = settings.upstox_token_refresh_interval_seconds

                if self._stop_event is None:
                    return

                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=interval,
                )

                return

            except asyncio.TimeoutError:
                pass

            except asyncio.CancelledError:
                raise

            try:
                await self.refresh_access_token()

            except asyncio.CancelledError:
                raise

            except Exception:
                logger.exception("Unexpected token refresh loop error.")

    def _validate_access_token(
        self,
        access_token: str,
    ) -> tuple[bool, dict[str, Any]]:
        try:
            configuration = upstox_client.Configuration()

            configuration.access_token = access_token

            api_instance = upstox_client.UserApi(upstox_client.ApiClient(configuration))

            profile = api_instance.get_profile(
                settings.upstox_token_validation_api_version
            )

            profile_data = getattr(
                profile,
                "data",
                None,
            )

            if hasattr(profile_data, "to_dict"):
                profile_data = profile_data.to_dict()

            if not isinstance(profile_data, dict):
                profile_data = {}

            return (
                True,
                {
                    "user_id": profile_data.get("user_id"),
                    "user_name": profile_data.get("user_name"),
                    "broker": profile_data.get("broker"),
                    "email": profile_data.get("email"),
                    "active": profile_data.get("is_active"),
                },
            )

        except ApiException as exc:
            return (
                False,
                {
                    "error": str(exc),
                },
            )

        except Exception as exc:
            return (
                False,
                {
                    "error": str(exc),
                },
            )

    async def _update_validation_status(
        self,
        success: bool,
        validation_result: dict[str, Any],
    ) -> None:
        try:
            collection = get_upstox_tokens_collection()

            now = datetime.now(settings.timezone).isoformat()

            if success:
                await collection.update_one(
                    {
                        "_id": settings.upstox_access_token_document_id,
                    },
                    {
                        "$set": {
                            "last_validation_status": "success",
                            "last_validation_status_text": "profile_success",
                            "last_validated_at": now,
                            "last_validation_error": None,
                            "last_profile_user_id": validation_result.get("user_id"),
                            "last_profile_user_name": validation_result.get(
                                "user_name"
                            ),
                            "last_profile_broker": validation_result.get("broker"),
                        }
                    },
                )
            else:
                await collection.update_one(
                    {
                        "_id": settings.upstox_access_token_document_id,
                    },
                    {
                        "$set": {
                            "last_validation_status": "failed",
                            "last_validation_status_text": "profile_failed",
                            "last_validated_at": now,
                            "last_validation_error": validation_result.get("error"),
                        }
                    },
                )

        except Exception:
            logger.exception("Failed to update token validation status.")

    async def refresh_access_token(self) -> bool:
        try:
            collection = get_upstox_tokens_collection()

            document = await collection.find_one(
                {
                    "_id": settings.upstox_access_token_document_id,
                }
            )

            if document is None:
                self._set_refresh_error("Token document not found.")

                logger.error("Token document not found.")

                return False

            access_token = document.get("access_token")

            if not isinstance(
                access_token,
                str,
            ):
                self._set_refresh_error("access_token missing.")

                logger.error("access_token missing.")

                return False

            access_token = access_token.strip()

            if not access_token:
                self._set_refresh_error("access_token empty.")

                logger.error("access_token empty.")

                return False

            if settings.upstox_token_validation_enabled:
                (
                    is_valid,
                    validation_result,
                ) = self._validate_access_token(access_token)

                await self._update_validation_status(
                    is_valid,
                    validation_result,
                )

                if not is_valid:
                    self._set_refresh_error(
                        validation_result.get(
                            "error",
                            "Token validation failed.",
                        )
                    )

                    logger.error("Upstox token validation failed.")

                    return False

            else:
                validation_result = {}

            refreshed_at = datetime.now(settings.timezone)

            with self._lock:
                token_changed = self._access_token != access_token

                self._access_token = access_token

                self._token_updated_at = document.get("updated_at")

                self._last_validated_at = datetime.now(settings.timezone).isoformat()

                self._last_validation_status = "success"

                self._profile_user_id = validation_result.get("user_id")

                self._profile_user_name = validation_result.get("user_name")

                self._profile_broker = validation_result.get("broker")

                self._cache_refreshed_at = refreshed_at

                self._last_refresh_error = None

            # --- LOGGING MODIFIED: removed broker, user_id, etc. ---
            logger.info(
                "Upstox token cache refreshed token_changed=%s",
                token_changed,
            )
            # -------------------------------------------------------

            return True

        except Exception as exc:
            self._set_refresh_error(str(exc))

            logger.exception("Failed to refresh Upstox access token.")

            return False

    def get_access_token(self) -> str | None:
        with self._lock:
            return self._access_token

    def has_access_token(self) -> bool:
        with self._lock:
            return bool(self._access_token)

    def get_cache_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "token_available": bool(self._access_token),
                "token_updated_at": self._token_updated_at,
                "last_validated_at": self._last_validated_at,
                "last_validation_status": self._last_validation_status,
                "last_profile_user_id": self._profile_user_id,
                "last_profile_user_name": self._profile_user_name,
                "last_profile_broker": self._profile_broker,
                "cache_refreshed_at": (
                    self._cache_refreshed_at.isoformat()
                    if self._cache_refreshed_at
                    else None
                ),
                "last_refresh_error": self._last_refresh_error,
                "refresh_interval_seconds": (
                    settings.upstox_token_refresh_interval_seconds
                ),
            }

    def _set_refresh_error(
        self,
        error: str,
    ) -> None:
        with self._lock:
            self._last_refresh_error = error


token_service = TokenService()
