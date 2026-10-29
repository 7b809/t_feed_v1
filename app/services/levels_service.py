# app/services/levels_service.py
#
# Job 3 orchestrator.
#
# Responsibilities
# ----------------
#   * On startup: load persisted levels for all 4 indexes, then (if current
#     IST time is inside LEVELS_STARTUP_WINDOW_START → LEVELS_STARTUP_WINDOW_END)
#     recompute fresh levels for all indexes.
#   * Daily at LEVELS_COMPUTE_HOUR:LEVELS_COMPUTE_MINUTE IST (default 09:17),
#     the scheduler calls ``compute_all(trigger="scheduled")``.
#   * On scheduled trigger:
#       - Retry the WHOLE batch up to LEVELS_SCHEDULED_RETRIES additional
#         times with LEVELS_SCHEDULED_RETRY_GAP_SECONDS between attempts.
#       - On success (all indexes "ok"):
#             * Persist to MongoDB (fixed _id, upsert)
#             * Send a Telegram notification
#           Both run IN PARALLEL and are best-effort.
#       - On final failure: send a Telegram failure notification.
#   * For manual and startup triggers: no batch retries; on success, DB
#     is persisted (idempotent), but Telegram is skipped (to avoid spam).
#
# MongoDB persistence
# -------------------
# Writes to ``LEVELS_PROJECT_INFO_COLLECTION`` (default "project_info")
# with _id = ``LEVELS_PROJECT_INFO_DOCUMENT_ID`` (default "pivot_levels").
# One fixed document, upserted daily — no new docs accumulate.

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, TYPE_CHECKING

from app.config import INDEX_METADATA, Settings
from app.services.ema_calculator import parse_ts
from app.services.levels_calculator import (
    LevelsCalculationError,
    calculate_levels,
    summarize_levels,
)
from app.services.levels_state_store import LevelsStateStore
from app.services.upstox_client_service import UpstoxError, UpstoxService

if TYPE_CHECKING:  # pragma: no cover
    from app.services.project_info_service import ProjectInfoService
    from app.services.telegram_service import ProjectNotifier

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


class LevelsError(RuntimeError):
    """Raised when pivot levels cannot be computed for an instrument."""


def _now_ist() -> datetime:
    return datetime.now(tz=IST)


class LevelsService:
    def __init__(
        self,
        config: Settings,
        upstox: UpstoxService,
        store: LevelsStateStore,
        notifier: "ProjectNotifier | None" = None,
        project_info: "ProjectInfoService | None" = None,
    ) -> None:
        self.config = config
        self.upstox = upstox
        self.store = store
        self.notifier = notifier
        self.project_info = project_info
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    async def _notify(self, method: str, **kwargs) -> None:
        if self.notifier is None:
            return
        try:
            await asyncio.to_thread(getattr(self.notifier, method), **kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("Telegram %s notification failed", method)

    # ------------------------------------------------------------------ #
    # Bootstrap
    # ------------------------------------------------------------------ #
    async def bootstrap(self) -> None:
        if not self.config.levels_enabled:
            logger.info("Levels service disabled (LEVELS_ENABLED=false)")
            return

        # Load persisted levels so GET works immediately after startup.
        for key, meta in self._ordered_indexes():
            persisted = self.store.load(key, meta)
            if persisted:
                self._cache[key] = persisted
                logger.info(
                    "Loaded persisted levels for %s (trade_date=%s)",
                    key, persisted.get("trade_date_ist"),
                )

        now = _now_ist()
        if self._is_in_startup_window(now):
            logger.info(
                "Startup inside levels window (%s IST); computing all indexes now",
                now.strftime("%H:%M:%S"),
            )
            try:
                await self.compute_all(trigger="startup")
            except Exception as exc:  # noqa: BLE001
                logger.exception("Startup levels compute failed; serving persisted levels")
                await self._notify(
                    "notify_exception",
                    where="levels_service.bootstrap",
                    exc=exc,
                )
        else:
            logger.info(
                "Startup before levels window (%s IST); waiting for %02d:%02d IST scheduler",
                now.strftime("%H:%M:%S"),
                self.config.levels_compute_hour,
                self.config.levels_compute_minute,
            )

    # ------------------------------------------------------------------ #
    # Public entry points
    # ------------------------------------------------------------------ #
    async def compute_all(self, trigger: str = "manual") -> dict[str, Any]:
        if not self.config.levels_enabled:
            raise LevelsError("Levels service is disabled")

        async with self._lock:
            if trigger == "scheduled":
                result = await self._compute_with_batch_retries(trigger)
            else:
                result = await self._compute_all_once(trigger)

        await self._post_compute(result, trigger)
        return result

    async def compute_one(
        self, canonical_or_key: str, trigger: str = "manual"
    ) -> dict[str, Any]:
        key, meta = self._resolve_index(canonical_or_key)
        async with self._lock:
            return await self._compute_one_with_retry(key, meta, trigger)

    # ------------------------------------------------------------------ #
    # Read API
    # ------------------------------------------------------------------ #
    def get_all(self) -> dict[str, Any]:
        today = _now_ist().date().isoformat()
        indexes: dict[str, Any] = {}
        for key, _meta in self._ordered_indexes():
            record = self._cache.get(key)
            if record is None:
                indexes[key] = {"status": "missing"}
                continue
            indexes[key] = {
                "status": "ok",
                "is_current_day": record.get("trade_date_ist") == today,
                **record,
            }
        return {
            "trade_date_ist": today,
            "indexes": indexes,
        }

    def get_one(self, canonical_or_key: str) -> dict[str, Any]:
        key, _meta = self._resolve_index(canonical_or_key)
        record = self._cache.get(key)
        if record is None:
            raise LevelsError(
                f"No levels computed yet for {key}; POST /api/v1/levels/refresh "
                f"or wait for the 09:17 scheduler"
            )
        today = _now_ist().date().isoformat()
        return {
            "status": "ok",
            "is_current_day": record.get("trade_date_ist") == today,
            **record,
        }

    # ------------------------------------------------------------------ #
    # Batched retries for scheduled trigger
    # ------------------------------------------------------------------ #
    async def _compute_with_batch_retries(self, trigger: str) -> dict[str, Any]:
        max_attempts = 1 + max(0, self.config.levels_scheduled_retries)
        last_result: dict[str, Any] = {"trigger": trigger, "computed_at": _now_ist().isoformat(), "results": {}}

        for attempt in range(1, max_attempts + 1):
            logger.info(
                "Levels batch attempt %d/%d trigger=%s",
                attempt, max_attempts, trigger,
            )
            last_result = await self._compute_all_once(trigger)

            if self._is_all_success(last_result):
                logger.info(
                    "Levels batch attempt %d/%d succeeded", attempt, max_attempts,
                )
                return last_result

            failed = [
                k for k, v in (last_result.get("results") or {}).items()
                if not (isinstance(v, dict) and v.get("status") == "ok")
            ]
            logger.warning(
                "Levels batch attempt %d/%d incomplete: failed=%s",
                attempt, max_attempts, failed,
            )

            if attempt < max_attempts:
                gap = max(1.0, self.config.levels_scheduled_retry_gap_seconds)
                logger.info("Retrying levels batch in %.1fs", gap)
                await asyncio.sleep(gap)

        return last_result

    # ------------------------------------------------------------------ #
    # Single gather pass
    # ------------------------------------------------------------------ #
    async def _compute_all_once(self, trigger: str) -> dict[str, Any]:
        indexes = self._ordered_indexes()
        coros = [
            self._compute_one_with_retry(key, meta, trigger)
            for key, meta in indexes
        ]
        raw = await asyncio.gather(*coros, return_exceptions=True)

        results: dict[str, Any] = {}
        for (key, _meta), outcome in zip(indexes, raw):
            if isinstance(outcome, BaseException):
                logger.warning("Levels compute failed for %s: %s", key, outcome)
                results[key] = {
                    "status": "error",
                    "error": str(outcome)[:300] or type(outcome).__name__,
                }
            else:
                results[key] = {"status": "ok", **outcome}

        return {
            "trigger": trigger,
            "computed_at": _now_ist().isoformat(),
            "results": results,
        }

    # ------------------------------------------------------------------ #
    # Post-compute: DB + Telegram
    # ------------------------------------------------------------------ #
    async def _post_compute(self, result: dict[str, Any], trigger: str) -> None:
        succeeded = self._is_all_success(result)

        if not succeeded:
            if trigger == "scheduled":
                await self._notify_failure_safe(result)
            return

        # Success path — always persist to DB; only Telegram on scheduled.
        coros: list = [self._persist_db_safe(result)]
        if trigger == "scheduled":
            coros.append(self._notify_success_safe(result))
        await asyncio.gather(*coros, return_exceptions=True)

    async def _persist_db_safe(self, result: dict[str, Any]) -> None:
        if self.project_info is None:
            logger.debug("ProjectInfoService not configured; skipping DB persist")
            return
        try:
            await asyncio.to_thread(self.project_info.upsert_levels, result)
        except Exception:  # noqa: BLE001
            logger.exception("Levels DB persist failed (best-effort)")

    async def _notify_success_safe(self, result: dict[str, Any]) -> None:
        if self.notifier is None:
            return
        try:
            await asyncio.to_thread(
                self.notifier.notify_levels_success, result=result
            )
        except Exception:  # noqa: BLE001
            logger.exception("Telegram levels success notification failed")

    async def _notify_failure_safe(self, result: dict[str, Any]) -> None:
        if self.notifier is None:
            return
        try:
            await asyncio.to_thread(
                self.notifier.notify_levels_failure, result=result
            )
        except Exception:  # noqa: BLE001
            logger.exception("Telegram levels failure notification failed")

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _is_all_success(self, result: dict[str, Any]) -> bool:
        results = result.get("results") or {}
        if not results:
            return False
        return all(
            isinstance(v, dict) and v.get("status") == "ok"
            for v in results.values()
        )

    def _ordered_indexes(self) -> list[tuple[str, dict[str, Any]]]:
        return sorted(
            INDEX_METADATA.items(),
            key=lambda kv: int(kv[1].get("priority", 999)),
        )

    def _resolve_index(self, name: str) -> tuple[str, dict[str, Any]]:
        normalized = (name or "").strip().lower()
        if not normalized:
            raise LevelsError("Missing index name")
        for key, meta in INDEX_METADATA.items():
            if (meta.get("folder") or "").lower() == normalized:
                return key, meta
            if (meta.get("canonical") or "").lower() == normalized:
                return key, meta
            if key.lower() == normalized:
                return key, meta
        raise LevelsError(f"Unknown index '{name}'")

    def _is_in_startup_window(self, now: datetime) -> bool:
        start = dtime(
            self.config.levels_startup_window_start_hour,
            self.config.levels_startup_window_start_minute,
        )
        end = dtime(
            self.config.levels_startup_window_end_hour,
            self.config.levels_startup_window_end_minute,
        )
        current = now.time()
        return start <= current <= end

    async def _compute_one_with_retry(
        self,
        key: str,
        meta: dict[str, Any],
        trigger: str,
    ) -> dict[str, Any]:
        # Per-instrument retries:
        #   * scheduled → 1 attempt (batch retries handle the retries)
        #   * startup   → 1 attempt (fast boot)
        #   * manual    → refresh_retries attempts (immediate user feedback)
        if trigger in ("startup", "scheduled"):
            max_attempts = 1
        else:
            max_attempts = max(1, self.config.refresh_retries)

        last_error: LevelsError | None = None
        for attempt in range(max_attempts):
            try:
                return await self._compute_one(key, meta, trigger)
            except LevelsError as exc:
                last_error = exc
                if attempt + 1 < max_attempts:
                    delay = self.config.retry_backoff_seconds * (attempt + 1)
                    logger.warning(
                        "Levels attempt %d/%d failed for %s: %s; retrying in %.1fs",
                        attempt + 1, max_attempts, key, exc, delay,
                    )
                    await asyncio.sleep(delay)
        assert last_error is not None
        raise last_error

    async def _compute_one(
        self,
        key: str,
        meta: dict[str, Any],
        trigger: str,
    ) -> dict[str, Any]:
        try:
            candles = await asyncio.wait_for(
                asyncio.to_thread(self.upstox.intraday_1m, key),
                timeout=self.config.ema_fetch_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise LevelsError(
                f"intraday fetch timed out for {key} "
                f"after {self.config.ema_fetch_timeout_seconds}s"
            ) from exc
        except UpstoxError as exc:
            raise LevelsError(f"intraday fetch failed for {key}: {exc}") from exc

        if not candles:
            raise LevelsError(
                f"no intraday candles for {key} "
                f"(market not open, or non-trading day)"
            )

        first = self._find_first_candle(candles)
        if first is None:
            raise LevelsError(
                f"first candle "
                f"({self.config.levels_first_candle_hour:02d}:"
                f"{self.config.levels_first_candle_minute:02d}) not yet "
                f"available for {key}"
            )

        try:
            levels = calculate_levels(first["high"], first["low"])
        except LevelsCalculationError as exc:
            raise LevelsError(f"level calculation failed for {key}: {exc}") from exc

        trade_date = _now_ist().date().isoformat()
        record = {
            "instrument_key": key,
            "canonical": meta.get("canonical"),
            "folder": meta.get("folder"),
            "underlying": meta.get("underlying"),
            "trading_symbol": meta.get("trading_symbol"),
            "priority": meta.get("priority"),
            "trade_date_ist": trade_date,
            "first_candle": {
                "timestamp": first.get("timestamp"),
                "open": first.get("open"),
                "high": first.get("high"),
                "low": first.get("low"),
                "close": first.get("close"),
                "volume": first.get("volume"),
            },
            "levels": levels,
            "levels_summary": summarize_levels(levels),
            "computed_at": _now_ist().isoformat(),
            "trigger": trigger,
        }

        path = self.store.save(key, meta, record)
        self._cache[key] = record

        logger.info(
            "Levels computed %s (%s) trigger=%s H=%.2f L=%.2f A=%.2f file=%s",
            key, meta.get("canonical"), trigger,
            levels["H"], levels["L"], levels["A"], path,
        )
        return record

    def _find_first_candle(self, candles: list[dict]) -> dict | None:
        today = _now_ist().date()
        start_h = self.config.levels_first_candle_hour
        start_m = self.config.levels_first_candle_minute
        window = max(1, self.config.levels_first_candle_window_minutes)

        start_total_min = start_h * 60 + start_m
        end_total_min = start_total_min + window
        start = dtime(start_h, start_m)
        end_h, end_m = divmod(end_total_min, 60)
        end = dtime(end_h, end_m)

        candidates: list[tuple[datetime, dict]] = []
        for candle in candles:
            ts = parse_ts(candle.get("timestamp"))
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            try:
                local = ts.astimezone(IST)
            except (ValueError, OSError):
                continue
            if local.date() != today:
                continue
            if not (start <= local.time() < end):
                continue
            candidates.append((local, candle))

        if not candidates:
            return None
        candidates.sort(key=lambda c: c[0])
        return candidates[0][1]