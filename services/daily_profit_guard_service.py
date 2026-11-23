"""Daily Upstox P&L snapshot and persistent new-entry lockout."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

from core.config import (
    DAILY_PROFIT_GUARD_COLLECTION,
    DAILY_PROFIT_GUARD_ENABLED,
    DAILY_PROFIT_GUARD_FAIL_CLOSED,
    DAILY_PROFIT_INCLUDE_UNREALISED,
    DAILY_PROFIT_LOCKOUT_UNTIL_NEXT_DAY,
    DAILY_PROFIT_TARGET,
    TIMEZONE,
    UPSTOX_HTTP_TIMEOUT_SECONDS,
    UPSTOX_SANDBOX,
)
from core.logger import get_logger
from core.mongo_client import get_mongo_db
from core.token_service import fetch_access_token

logger = get_logger(__file__)
POSITIONS_URL = "https://api.upstox.com/v2/portfolio/short-term-positions"


class DailyProfitGuardError(RuntimeError):
    """The daily P&L could not be verified or the profit target blocks entries."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 503,
        code: str = "DAILY_PROFIT_CHECK_FAILED",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details or {}


class DailyProfitGuardService:
    """Calculate account P&L and latch a profit-target lock for the IST trade date.

    All rows from Upstox are included, including zero-net-quantity rows which
    can contain realised P&L from positions closed earlier in the day.
    """

    def __init__(
        self,
        *,
        positions_loader: Callable[[], list[dict[str, Any]]] | None = None,
        lock_collection: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._positions_loader = positions_loader
        self._lock_collection_override = lock_collection
        self._clock = clock or (lambda: datetime.now(ZoneInfo(TIMEZONE)))
        self._timezone = ZoneInfo(TIMEZONE)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=self._timezone)
        return value.astimezone(self._timezone)

    def _load_positions(self) -> list[dict[str, Any]]:
        if self._positions_loader is not None:
            rows = self._positions_loader()
            if not isinstance(rows, list):
                raise DailyProfitGuardError("The positions provider returned an invalid response.")
            return rows

        if UPSTOX_SANDBOX:
            raise DailyProfitGuardError(
                "Daily P&L guard requires a live Upstox account. Set UPSTOX_SANDBOX=false.",
                status_code=409,
                code="DAILY_PROFIT_LIVE_ACCOUNT_REQUIRED",
            )

        try:
            token = fetch_access_token()
            response = requests.get(
                POSITIONS_URL,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                timeout=UPSTOX_HTTP_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("Daily positions request failed: %s", type(exc).__name__)
            raise DailyProfitGuardError(
                "Could not retrieve current Upstox positions; new entries are blocked."
            ) from None

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if not response.ok:
            logger.warning("Upstox positions request returned HTTP %s", response.status_code)
            raise DailyProfitGuardError(
                "Upstox did not return a successful positions response; new entries are blocked.",
                status_code=502,
                details={"upstream_http_status": response.status_code},
            )

        rows = payload.get("data") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or str(payload.get("status", "")).lower() != "success"
            or not isinstance(rows, list)
        ):
            raise DailyProfitGuardError(
                "Upstox positions response was missing valid position data; new entries are blocked.",
                status_code=502,
            )
        return rows

    @property
    def _lock_collection(self):
        if self._lock_collection_override is not None:
            return self._lock_collection_override
        return get_mongo_db()[DAILY_PROFIT_GUARD_COLLECTION]

    @staticmethod
    def _pnl_value(position: dict[str, Any], field: str, index: int) -> float:
        value = position.get(field)
        if value is None:
            return 0.0
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise DailyProfitGuardError(
                f"Upstox returned an invalid {field} value for position {index}.",
                status_code=502,
                details={"position_index": index, "field": field},
            ) from None
        if number != number or number in (float("inf"), float("-inf")):
            raise DailyProfitGuardError(
                f"Upstox returned a non-finite {field} value for position {index}.",
                status_code=502,
                details={"position_index": index, "field": field},
            )
        return number

    def _read_lock(self, trade_date: str) -> dict[str, Any] | None:
        try:
            return self._lock_collection.find_one({"_id": trade_date})
        except Exception as exc:
            logger.warning("Could not read daily profit lock from MongoDB: %s", type(exc).__name__)
            if DAILY_PROFIT_GUARD_FAIL_CLOSED:
                raise DailyProfitGuardError(
                    "Could not verify the persisted daily profit lock; new entries are blocked."
                ) from None
            return None

    def _persist_lock(
        self,
        trade_date: str,
        *,
        guard_pnl: float,
        realised_pnl: float,
        unrealised_pnl: float,
        now: datetime,
    ) -> dict[str, Any]:
        record = {
            "_id": trade_date,
            "trade_date": trade_date,
            "locked": True,
            "reason": "DAILY_PROFIT_TARGET_REACHED",
            "target": DAILY_PROFIT_TARGET,
            "guard_pnl": round(guard_pnl, 2),
            "realised_pnl": round(realised_pnl, 2),
            "unrealised_pnl": round(unrealised_pnl, 2),
            "locked_at": now.isoformat(),
        }
        try:
            self._lock_collection.update_one(
                {"_id": trade_date},
                {"$setOnInsert": record},
                upsert=True,
            )
            stored = self._lock_collection.find_one({"_id": trade_date})
            return dict(stored or record)
        except Exception as exc:
            # A concurrent request may have created the same lock just before
            # this upsert returned a duplicate-key error. Read it once more.
            try:
                stored = self._lock_collection.find_one({"_id": trade_date})
                if stored and stored.get("locked"):
                    return dict(stored)
            except Exception:
                pass
            logger.warning("Could not persist daily profit lock: %s", type(exc).__name__)
            if DAILY_PROFIT_GUARD_FAIL_CLOSED:
                raise DailyProfitGuardError(
                    "Daily profit target was reached but its lock could not be persisted; new entries are blocked."
                ) from None
            return record

    def get_today_pnl(self) -> dict[str, Any]:
        now = self._now()
        trade_date = now.date().isoformat()
        rows = self._load_positions()

        realised = 0.0
        unrealised = 0.0
        for index, position in enumerate(rows):
            if not isinstance(position, dict):
                raise DailyProfitGuardError(
                    f"Upstox returned an invalid position at index {index}.",
                    status_code=502,
                )
            realised += self._pnl_value(position, "realised", index)
            unrealised += self._pnl_value(position, "unrealised", index)

        total = realised + unrealised
        guard_pnl = total if DAILY_PROFIT_INCLUDE_UNREALISED else realised
        threshold_reached_now = guard_pnl >= DAILY_PROFIT_TARGET
        existing_lock = None
        if DAILY_PROFIT_GUARD_ENABLED and DAILY_PROFIT_LOCKOUT_UNTIL_NEXT_DAY:
            existing_lock = self._read_lock(trade_date)

        lock_active = False
        lock_record = existing_lock
        if DAILY_PROFIT_GUARD_ENABLED:
            if DAILY_PROFIT_LOCKOUT_UNTIL_NEXT_DAY:
                if existing_lock and existing_lock.get("locked"):
                    lock_active = True
                elif threshold_reached_now:
                    lock_record = self._persist_lock(
                        trade_date,
                        guard_pnl=guard_pnl,
                        realised_pnl=realised,
                        unrealised_pnl=unrealised,
                        now=now,
                    )
                    lock_active = True
            else:
                lock_active = threshold_reached_now

        target_reached_today = threshold_reached_now or bool(
            existing_lock and existing_lock.get("locked")
        )
        return {
            "status": "success",
            "trade_date": trade_date,
            "currency": "INR",
            "realised_pnl": round(realised, 2),
            "unrealised_pnl": round(unrealised, 2),
            "total_pnl": round(total, 2),
            "guard_pnl": round(guard_pnl, 2),
            "daily_profit_target": round(DAILY_PROFIT_TARGET, 2),
            "guard_enabled": DAILY_PROFIT_GUARD_ENABLED,
            "include_unrealised": DAILY_PROFIT_INCLUDE_UNREALISED,
            "target_reached": target_reached_today,
            "lockout_active": lock_active,
            "new_entries_allowed": not lock_active if DAILY_PROFIT_GUARD_ENABLED else True,
            "positions_count": len(rows),
            "source": "upstox",
            "updated_at": now.isoformat(),
            "lockout": (
                {
                    "locked_at": (lock_record or {}).get("locked_at"),
                    "reason": (lock_record or {}).get("reason") or "DAILY_PROFIT_TARGET_REACHED",
                }
                if lock_active
                else None
            ),
        }

    def assert_new_entries_allowed(self) -> dict[str, Any]:
        if not DAILY_PROFIT_GUARD_ENABLED:
            return {
                "status": "disabled",
                "guard_enabled": False,
                "new_entries_allowed": True,
            }
        try:
            snapshot = self.get_today_pnl()
        except DailyProfitGuardError:
            if DAILY_PROFIT_GUARD_FAIL_CLOSED:
                raise
            logger.warning("Daily P&L guard unavailable; fail-closed disabled, allowing entry.")
            return {
                "status": "unavailable",
                "new_entries_allowed": True,
                "guard_enabled": True,
                "guard_error": "P&L check unavailable; fail-closed mode disabled",
            }

        if not snapshot["new_entries_allowed"]:
            raise DailyProfitGuardError(
                (
                    f"Daily profit target of INR {DAILY_PROFIT_TARGET:.2f} has been reached. "
                    "New BUY orders and GTT placements are blocked until the next IST date."
                ),
                status_code=403,
                code="DAILY_PROFIT_TARGET_REACHED",
                details=snapshot,
            )
        return snapshot


_default_service = DailyProfitGuardService()


def get_today_pnl_status() -> dict[str, Any]:
    return _default_service.get_today_pnl()


def assert_new_entries_allowed() -> dict[str, Any]:
    return _default_service.assert_new_entries_allowed()
