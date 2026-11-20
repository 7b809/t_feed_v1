"""Local Upstox HistoryV3 Supertrend bootstrap, recovery and live polling."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Settings
from .supertrend_calculator import calculate_supertrend, signal_identity
from .upstox_history_service import UpstoxError, UpstoxHistoryService

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def _now() -> datetime:
    return datetime.now(IST)


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=IST)
    return result.astimezone(IST)


class LocalSupertrendService:
    """Per-isolation Upstox candle backfill and confirmed local Supertrend feed."""
    def __init__(self, config: Settings, order_engine: Any,
                 upstox: UpstoxHistoryService | None = None) -> None:
        self.config = config
        self.order_engine = order_engine
        self.upstox = upstox or UpstoxHistoryService(config)
        self._tasks: dict[str, asyncio.Task] = {}
        self._stopped = False
        self._semaphore = asyncio.Semaphore(4)

    async def start_isolation(self, isolation_id: str, legs: dict[str, str],
                              isolation_time: datetime) -> None:
        if self._stopped or not self.config.supertrend_enabled:
            return
        task = self._tasks.get(isolation_id)
        if task and not task.done():
            return
        cutoff = isolation_time.astimezone(IST) if isolation_time.tzinfo else isolation_time.replace(tzinfo=IST)
        recovery_end = cutoff + timedelta(seconds=max(0, int(self.config.supertrend_recovery_buffer_seconds)))
        await self.order_engine.prepare_supertrend_recovery(isolation_id, cutoff, recovery_end)
        self._tasks[isolation_id] = asyncio.create_task(
            self._run_isolation(isolation_id, dict(legs), cutoff, recovery_end),
            name=f"local-supertrend:{isolation_id}",
        )
        logger.info("Local Supertrend scheduled iso=%s cutoff=%s recovery_end=%s",
                    isolation_id, cutoff.isoformat(), recovery_end.isoformat())

    async def stop_isolation(self, isolation_id: str) -> None:
        task = self._tasks.pop(isolation_id, None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def stop(self) -> None:
        self._stopped = True
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _merge(*collections: list[dict]) -> list[dict]:
        rows: dict[str, dict] = {}
        for collection in collections:
            for candle in collection or []:
                timestamp = candle.get("timestamp")
                if timestamp is not None:
                    rows[str(timestamp)] = candle
        return sorted(rows.values(), key=lambda row: str(row.get("timestamp")))

    @staticmethod
    def _confirmed(candles: list[dict], now: datetime) -> list[dict]:
        output = []
        for candle in candles:
            stamp = _parse_ts(candle.get("timestamp"))
            if stamp and stamp + timedelta(minutes=1) <= now:
                output.append(candle)
        return output

    @staticmethod
    def _today_signals(signals: list[dict], today) -> list[dict]:
        return [item for item in signals
                if (stamp := _parse_ts(item.get("confirmed_time"))) is not None
                and stamp.date() == today]

    async def _fetch_full_day(self, key: str) -> list[dict]:
        async with self._semaphore:
            history, intraday = await asyncio.gather(
                asyncio.wait_for(asyncio.to_thread(
                    self.upstox.historical_window, key, int(self.config.supertrend_history_days)
                ), timeout=float(self.config.supertrend_fetch_timeout_seconds) * 4),
                asyncio.wait_for(asyncio.to_thread(
                    self.upstox.intraday_1m, key
                ), timeout=float(self.config.supertrend_fetch_timeout_seconds)),
            )
        return self._confirmed(self._merge(history, intraday), _now())

    async def _fetch_intraday(self, key: str) -> list[dict]:
        async with self._semaphore:
            rows = await asyncio.wait_for(
                asyncio.to_thread(self.upstox.intraday_1m, key),
                timeout=float(self.config.supertrend_fetch_timeout_seconds),
            )
        return self._confirmed(rows, _now())

    async def _run_isolation(self, isolation_id: str, legs: dict[str, str],
                             cutoff: datetime, recovery_end: datetime) -> None:
        try:
            wait_seconds = (recovery_end - _now()).total_seconds()
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)

            # Fail closed and retry transient API failures; do not silently
            # switch to incomplete candles or generate synthetic entry prices.
            while not self._stopped:
                try:
                    keys = [legs.get("isolated"), legs.get("opposite")]
                    results = await asyncio.gather(*(
                        self._fetch_full_day(key) if key else asyncio.sleep(0, result=[])
                        for key in keys
                    ), return_exceptions=True)
                    errors = [item for item in results if isinstance(item, BaseException)]
                    if errors:
                        raise UpstoxError("; ".join(str(item) for item in errors))
                    isolated_candles, opposite_candles = results
                    if not isolated_candles:
                        raise UpstoxError(f"No confirmed candles for isolated instrument {keys[0]}")

                    calculated, indicator_state = calculate_supertrend(
                        isolated_candles,
                        int(self.config.supertrend_atr_period),
                        float(self.config.supertrend_multiplier),
                        bool(self.config.supertrend_change_atr),
                    )
                    today_signals = self._today_signals(calculated, _now().date())
                    as_of = _now()
                    # Upstox confirmed_time is the candle OPEN. The actual
                    # confirmation occurs at candle-open + 1 minute. Convert
                    # wall-clock isolation/as-of bounds to candle-open bounds.
                    recovery_signals = [
                        item for item in today_signals
                        if (stamp := _parse_ts(item.get("confirmed_time"))) is not None
                        and cutoff - timedelta(minutes=1) < stamp <= as_of - timedelta(minutes=1)
                    ]
                    snapshot = {
                        "event": "snapshot", "indicator": "supertrend",
                        "instrument_key": legs.get("isolated"), "data_scope": "today_only",
                        "today_ist_date": as_of.date().isoformat(),
                        "signals": recovery_signals, "signal_count": len(recovery_signals),
                        "trend": "up" if indicator_state.get("trend", 1) == 1 else "down",
                        "supertrend": indicator_state.get("supertrend"),
                        "atr": indicator_state.get("atr"), "last_ts": indicator_state.get("last_ts"),
                        "last_close": indicator_state.get("last_close"),
                    }
                    await self.order_engine._recover_supertrend_history(
                        isolation_id, snapshot,
                        candles_by_role={"isolated": isolated_candles, "opposite": opposite_candles},
                        recovery_start=cutoff - timedelta(minutes=1),
                        recovery_end=as_of - timedelta(minutes=1),
                        recovery_as_of=as_of,
                        recovery_source="upstox_history_v3_local_supertrend",
                    )
                    # If the latest confirmed flip has no following candle yet,
                    # the order engine deliberately remains WAITING_FOR_HISTORY.
                    # Re-fetch and retry so the first post-signal candle can price
                    # the simulated entry instead of dropping that signal forever.
                    recovery_state = await asyncio.to_thread(
                        self.order_engine._store.load, isolation_id
                    )
                    recovery_status = ((recovery_state or {}).get("supertrend_startup_recovery") or {}).get("status")
                    if recovery_status == "WAITING_FOR_HISTORY":
                        logger.info("Local Supertrend recovery awaiting next candle iso=%s; retrying", isolation_id)
                        await asyncio.sleep(max(5, int(self.config.supertrend_poll_seconds)))
                        continue
                    logger.info(
                        "Local Supertrend recovery iso=%s candles=%d today_signals=%d replay_signals=%d trend=%s",
                        isolation_id, len(isolated_candles), len(today_signals), len(recovery_signals),
                        indicator_state.get("trend"),
                    )
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Local Supertrend bootstrap failed iso=%s; retrying in 15s: %s",
                                     isolation_id, exc)
                    await asyncio.sleep(15)

            seen = {signal_identity(item) for item in today_signals}
            history = isolated_candles
            while not self._stopped:
                await asyncio.sleep(max(5, int(self.config.supertrend_poll_seconds)))
                try:
                    intraday = await self._fetch_intraday(legs["isolated"])
                    merged = self._confirmed(self._merge(history, intraday), _now())
                    if not merged:
                        continue
                    calculated, current_state = calculate_supertrend(
                        merged, int(self.config.supertrend_atr_period),
                        float(self.config.supertrend_multiplier),
                        bool(self.config.supertrend_change_atr),
                    )
                    current_signals = self._today_signals(calculated, _now().date())
                    fresh = [item for item in current_signals
                             if signal_identity(item) not in seen]
                    history = merged
                    for item in fresh:
                        seen.add(signal_identity(item))
                        stamp = _parse_ts(item.get("confirmed_time"))
                        if stamp is None or stamp <= recovery_end:
                            continue
                        frame = {
                            "event": "supertrend_signal", "indicator": "supertrend",
                            "instrument_key": legs["isolated"], "data_scope": "today_only",
                            "today_ist_date": _now().date().isoformat(), "confirmed": True,
                            **item,
                        }
                        await self.order_engine.on_supertrend_frame(
                            frame, legs["isolated"], "isolated", isolation_id,
                            event_origin="local_upstox",
                            live_event_received_at=_now().isoformat(),
                        )
                    logger.debug("Local Supertrend poll iso=%s candles=%d today_signals=%d new=%d trend=%s",
                                 isolation_id, len(merged), len(current_signals), len(fresh),
                                 current_state.get("trend"))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Local Supertrend polling failed iso=%s", isolation_id)
        except asyncio.CancelledError:
            logger.info("Local Supertrend task cancelled iso=%s", isolation_id)
            raise
        except Exception:
            logger.exception("Local Supertrend task terminated iso=%s", isolation_id)
        finally:
            if self._tasks.get(isolation_id) is asyncio.current_task():
                self._tasks.pop(isolation_id, None)
