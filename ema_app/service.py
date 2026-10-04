"""
Live EMA crossover service — core logic.

Responsibilities
----------------
  - Track every enabled index + option contract
  - Aggregate ticks into per-minute candles
  - Detect 9/21 EMA crosses on candle close
  - Persist only crosses to per-contract intraday_cross.json files,
    alongside the day's opening-range levels
  - Broadcast crosses to connected WebSocket clients
  - Restart-safe: on boot / hard refresh, rebuild EMA state and re-detect
    crosses up to "now" using historical disk candles + today's intraday
    fetched via the existing `candle_service._fetch_intraday` helper
    (reuses the SDK client, rate limiter, and retry policy)

Listener hooks exposed for the isolation layer:
  - register_cross_listener(fn)   -> every EMA cross (already existed)
  - register_candle_listener(fn)  -> every finalized minute candle (new)

Designed to run inside the FastAPI event loop. Tick ingestion is safe to
call from SDK background threads.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.logger import get_logger
from ema_app.config import ema_config
from ema_app.opening_range import compute_opening_range_levels
from ema_app.state import InstrumentState, MinuteBar

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
READONLY_DIR = DATA_DIR / "readonly"
RUNTIME_DIR = DATA_DIR / "runtime"


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def now_ist() -> datetime:
    return datetime.now(IST)


def format_ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d %H:%M:%S")


def today_ist_str() -> str:
    return now_ist().strftime("%Y-%m-%d")


def ist_date_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d")


def parse_hhmm(hhmm: str, ref: Optional[datetime] = None) -> datetime:
    ref = ref or now_ist()
    hh, mm = hhmm.split(":")
    return ref.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)


def is_inside_session(ref: Optional[datetime] = None) -> bool:
    """True when `ref` (or now) is inside the configured trading window on a weekday."""
    ref = ref or now_ist()
    if ref.weekday() >= 5:
        return False
    open_dt = parse_hhmm(ema_config.market_open, ref)
    close_dt = parse_hhmm(ema_config.market_close, ref)
    return open_dt <= ref <= close_dt


# ---------------------------------------------------------------------------
# Raw Upstox row -> internal candle dict
# ---------------------------------------------------------------------------
def _raw_row_to_candle(row: Any) -> Optional[Dict[str, Any]]:
    """
    Convert an Upstox raw candle row into {time, open, high, low, close}.

    Upstox v3 format: [ISO timestamp, open, high, low, close, volume, oi]
    Non-market-day placeholder rows (all-zero OHLC) are dropped.
    """
    if not isinstance(row, (list, tuple)) or len(row) < 5:
        return None
    try:
        ts_iso = row[0]
        dt = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
        ts = int(dt.timestamp())
        o = float(row[1])
        h = float(row[2])
        l = float(row[3])
        c = float(row[4])
    except (ValueError, TypeError, IndexError):
        return None
    # Drop non-market-day placeholder rows
    if o == 0 and h == 0 and l == 0 and c == 0:
        return None
    return {"time": ts, "open": o, "high": h, "low": l, "close": c}


# ---------------------------------------------------------------------------
# Intraday fetch — delegates to candle_service._fetch_intraday
# ---------------------------------------------------------------------------
def _fetch_intraday_via_candle_service(
    instrument_key: str,
    unit: str = "minutes",
    interval: str = "1",
) -> List[Dict[str, Any]]:
    """
    Fetch today's intraday 1-minute candles using the project's existing
    `candle_service._fetch_intraday` helper.

    This reuses:
      - the SDK's HistoryV3Api client (thread-local)
      - the process-wide RateLimiter (Cloudflare 429 handling)
      - the configured request delay / retry policy

    Returns a sorted list of {time, open, high, low, close}. Never raises.
    """
    try:
        from upstox_app.candle.candle_service import candle_service  # type: ignore
    except Exception as exc:
        logger.warning("EMA intraday: candle_service import failed | err=%s", exc)
        return []

    try:
        rows, errors, ok = candle_service._fetch_intraday(
            instrument_key, unit, interval
        )
    except Exception as exc:
        logger.debug(
            "EMA intraday: candle_service._fetch_intraday raised | key=%s | err=%s",
            instrument_key, exc,
        )
        return []

    if errors:
        logger.debug(
            "EMA intraday: upstream errors | key=%s | count=%d | first=%s",
            instrument_key, len(errors), errors[0],
        )

    out: List[Dict[str, Any]] = []
    for row in rows or []:
        candle = _raw_row_to_candle(row)
        if candle is not None:
            out.append(candle)

    out.sort(key=lambda x: x["time"])
    return out


# ---------------------------------------------------------------------------
# Merge helper
# ---------------------------------------------------------------------------
def _merge_and_dedupe(*lists: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge candle lists, dedupe by time, sort ascending."""
    merged: Dict[int, Dict[str, Any]] = {}
    for lst in lists:
        if not lst:
            continue
        for c in lst:
            if not isinstance(c, dict):
                continue
            try:
                ts = int(c["time"])
            except (KeyError, TypeError, ValueError):
                continue
            merged[ts] = {
                "time": ts,
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
            }
    return [merged[k] for k in sorted(merged.keys())]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class EmaService:
    def __init__(self) -> None:
        self._lock = threading.RLock()

        self._instruments: Dict[str, InstrumentState] = {}
        self._last_cross_at: Optional[int] = None
        self._started_at: Optional[datetime] = None
        self._session_date: Optional[str] = None
        self._streamer_attached = False

        # Listener hooks
        self._cross_listener = None
        self._candle_listener = None

        self._fast_alpha = 2.0 / (ema_config.fast_period + 1)
        self._slow_alpha = 2.0 / (ema_config.slow_period + 1)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._started_at is not None:
                return
            self._started_at = now_ist()
            self._session_date = today_ist_str()
            logger.info(
                "EMA app starting | fast=%d slow=%d session=%s",
                ema_config.fast_period, ema_config.slow_period, self._session_date,
            )

    def stop(self) -> None:
        with self._lock:
            self._started_at = None
            logger.info("EMA app stopped")

    def is_running(self) -> bool:
        return self._started_at is not None

    # ------------------------------------------------------------------
    # Instrument registry
    # ------------------------------------------------------------------
    def register_instruments(self, instruments: Iterable[Dict[str, Any]]) -> int:
        count = 0
        with self._lock:
            for raw in instruments:
                if not isinstance(raw, dict):
                    continue

                key = raw.get("instrument_key") or raw.get("instrumentKey")
                if not key:
                    continue

                underlying = str(
                    raw.get("underlying_symbol")
                    or raw.get("underlying")
                    or raw.get("index")
                    or ""
                ).upper()

                strike = raw.get("strike_price")
                try:
                    strike = float(strike) if strike not in (None, "") else None
                except (TypeError, ValueError):
                    strike = None

                option_type = raw.get("option_type") or raw.get("instrument_type")
                if option_type:
                    option_type = str(option_type).upper()
                    if option_type not in ("CE", "PE"):
                        option_type = None

                state = self._instruments.get(key)
                if state is None:
                    self._instruments[key] = InstrumentState(
                        instrument_key=key,
                        underlying=underlying,
                        strike=strike,
                        option_type=option_type,
                        expiry=raw.get("expiry"),
                        trading_symbol=raw.get("trading_symbol") or raw.get("short_name"),
                    )
                    count += 1
                else:
                    if underlying:
                        state.underlying = underlying
                    if strike is not None:
                        state.strike = strike
                    if option_type:
                        state.option_type = option_type
                    if raw.get("expiry"):
                        state.expiry = raw.get("expiry")
                    if raw.get("trading_symbol"):
                        state.trading_symbol = raw.get("trading_symbol")

        logger.info(
            "EMA app registered instruments | total=%d new=%d",
            len(self._instruments), count,
        )
        return count

    def instrument_count(self) -> int:
        return len(self._instruments)

    def list_instruments(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [s.snapshot() for s in self._instruments.values()]

    def instrument_keys(self) -> List[str]:
        with self._lock:
            return list(self._instruments.keys())

    # ------------------------------------------------------------------
    # Historical load from disk
    # ------------------------------------------------------------------
    def _load_historical_from_disk(self, state: InstrumentState) -> List[Dict[str, Any]]:
        if not state.underlying or state.strike is None or not state.option_type:
            return []

        path = (
            READONLY_DIR
            / state.underlying.upper()
            / "candles"
            / f"{int(state.strike)}_{state.option_type}.json"
        )
        if not path.exists():
            return []

        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as exc:
            logger.warning("EMA backfill: failed to read %s: %s", path, exc)
            return []

        raw = payload.get("candles") if isinstance(payload, dict) else payload
        if not isinstance(raw, list):
            return []

        out: List[Dict[str, Any]] = []
        for c in raw:
            if not isinstance(c, dict):
                continue
            try:
                out.append(
                    {
                        "time": int(c.get("time") or c.get("timestamp") or c.get("ts")),
                        "open": float(c["open"]),
                        "high": float(c["high"]),
                        "low": float(c["low"]),
                        "close": float(c["close"]),
                    }
                )
            except Exception:
                continue

        out.sort(key=lambda x: x["time"])
        return out

    # ------------------------------------------------------------------
    # Persistence — intraday_cross.json
    # ------------------------------------------------------------------
    def _cross_file_path(self, state: InstrumentState) -> Optional[Path]:
        if not state.underlying or state.strike is None or not state.option_type:
            return None
        return (
            RUNTIME_DIR
            / state.underlying.upper()
            / f"{int(state.strike)}_{state.option_type}"
            / "intraday_cross.json"
        )

    def _read_file_crosses(self, state: InstrumentState) -> List[Dict[str, Any]]:
        path = self._cross_file_path(state)
        if path is None or not path.exists():
            return []
        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception:
            return []
        raw = payload.get("crossovers") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            return []
        return [c for c in raw if isinstance(c, dict)]

    def load_persisted_crosses_for_today(self) -> None:
        with self._lock:
            instruments = list(self._instruments.values())

        today_open_ts = int(parse_hhmm(ema_config.market_open).timestamp())

        for state in instruments:
            path = self._cross_file_path(state)
            if path is None or not path.exists():
                continue

            try:
                with path.open("r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except Exception as exc:
                logger.warning("EMA persist: cannot read %s: %s", path, exc)
                continue

            raw_crosses = payload.get("crossovers") if isinstance(payload, dict) else None
            if not isinstance(raw_crosses, list):
                raw_crosses = []

            kept: List[Dict[str, Any]] = []
            for entry in raw_crosses:
                if not isinstance(entry, dict):
                    continue
                try:
                    ts = int(entry.get("cross_time"))
                except (TypeError, ValueError):
                    continue
                if ts < today_open_ts:
                    continue
                kept.append(entry)

            state.persisted_cross_times = {
                int(e["cross_time"]) for e in kept if "cross_time" in e
            }
            state.crosses_today_records = kept

            if isinstance(payload, dict):
                orl = payload.get("opening_range_levels")
                if isinstance(orl, dict) and orl:
                    state.opening_range_levels = orl

            self._persist_intraday_file(state)

    def _persist_intraday_file(self, state: InstrumentState) -> None:
        if not ema_config.persist_crosses:
            return
        path = self._cross_file_path(state)
        if path is None:
            return

        existing = self._read_file_crosses(state)
        by_time: Dict[int, Dict[str, Any]] = {}
        for c in existing + state.crosses_today_records:
            if not isinstance(c, dict):
                continue
            try:
                ts = int(c["cross_time"])
            except (KeyError, TypeError, ValueError):
                continue
            by_time[ts] = c
        merged = [by_time[k] for k in sorted(by_time.keys())]
        state.crosses_today_records = merged

        bullish_count = sum(1 for c in merged if c.get("cross_type") == "bullish_cross")
        bearish_count = sum(1 for c in merged if c.get("cross_type") == "bearish_cross")

        from_date = (
            ist_date_str(state.first_candle_time_today)
            if state.first_candle_time_today
            else None
        )
        to_date = (
            ist_date_str(state.last_candle_time)
            if state.last_candle_time
            else None
        )

        status = "ok" if state.candles_considered_today > 0 else "empty"

        payload = {
            "index_name": state.underlying,
            "strike_price": state.strike,
            "option_type": state.option_type,
            "instrument_key": state.instrument_key,
            "expiry": state.expiry,
            "trading_symbol": state.trading_symbol,
            "source": "intraday",
            "unit": "minutes",
            "interval": "1",
            "ema_fast": ema_config.fast_period,
            "ema_slow": ema_config.slow_period,
            "computed_at": now_ist().isoformat(),
            "status": status,
            "candles_considered": state.candles_considered_today,
            "from_date": from_date,
            "to_date": to_date,
            "bullish_count": bullish_count,
            "bearish_count": bearish_count,
            "opening_range_levels": state.opening_range_levels or {},
            "crossovers": merged,
        }

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, default=str)
        except Exception as exc:
            logger.warning("EMA persist: cannot write %s: %s", path, exc)

    # ------------------------------------------------------------------
    # Backfill
    # ------------------------------------------------------------------
    def backfill_instrument(self, instrument_key: str, force: bool = False) -> int:
        with self._lock:
            state = self._instruments.get(instrument_key)
        if state is None:
            return 0

        now_ts = time.time()
        if not force and state.last_backfilled_at is not None:
            if now_ts - state.last_backfilled_at < ema_config.backfill_cooldown_sec:
                return 0

        # 1) Historical from disk
        hist = self._load_historical_from_disk(state)

        # 2) Today's intraday via candle_service._fetch_intraday
        intraday = _fetch_intraday_via_candle_service(
            instrument_key,
            unit="minutes",
            interval="1",
        )

        # 3) Merge
        merged = _merge_and_dedupe(hist, intraday)
        if not merged:
            state.last_backfilled_at = now_ts
            state.candles_considered_today = 0
            self._persist_intraday_file(state)
            return 0

        today_str = today_ist_str()
        today_candles = [c for c in merged if ist_date_str(c["time"]) == today_str]

        # 4) Opening range from first today candle
        if today_candles:
            first = today_candles[0]
            levels = compute_opening_range_levels(first)
            if levels:
                state.opening_range_levels = levels
                state.opening_range_candle_time = int(first["time"])
            state.first_candle_time_today = int(first["time"])

        state.candles_considered_today = len(today_candles)

        # 5) Reset EMA and replay
        state.ema_fast = None
        state.ema_slow = None
        state.prev_diff = None
        state.last_candle_time = None
        state.last_close = None
        state.active_bar = None

        for c in merged:
            ts = int(c["time"])
            close = float(c["close"])

            prev_diff = state.prev_diff
            state.apply_close(ts, close, self._fast_alpha, self._slow_alpha)
            curr_diff = state.current_diff()
            state.prev_diff = curr_diff

            if ist_date_str(ts) != today_str:
                continue
            if prev_diff is None or curr_diff is None:
                continue

            o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
            if o == h == l == cl:
                continue

            cross_type: Optional[str] = None
            if prev_diff <= 0 and curr_diff > 0:
                cross_type = "bullish_cross"
            elif prev_diff >= 0 and curr_diff < 0:
                cross_type = "bearish_cross"
            if cross_type is None:
                continue

            if ts in state.persisted_cross_times:
                continue

            state.persisted_cross_times.add(ts)
            record = self._build_cross_record(state, c, cross_type)
            state.crosses_today_records.append(record)

        state.last_backfilled_at = now_ts
        self._persist_intraday_file(state)

        return len(merged)

    def backfill_all(self, force: bool = False) -> int:
        keys = self.instrument_keys()
        done = 0
        for key in keys:
            try:
                n = self.backfill_instrument(key, force=force)
                if n:
                    done += 1
            except Exception as exc:
                logger.warning("EMA backfill %s failed: %s", key, exc)
        return done

    # ------------------------------------------------------------------
    # Cross record builder
    # ------------------------------------------------------------------
    def _build_cross_record(
        self,
        state: InstrumentState,
        candle: Dict[str, Any],
        cross_type: str,
    ) -> Dict[str, Any]:
        ts = int(candle["time"])
        return {
            "instrument_key": state.instrument_key,
            "underlying": state.underlying,
            "strike": state.strike,
            "option_type": state.option_type,
            "expiry": state.expiry,
            "trading_symbol": state.trading_symbol,
            "cross_time": ts,
            "cross_time_iso": format_ist(ts),
            "cross_type": cross_type,
            "close": float(candle["close"]),
            "ema_fast": float(state.ema_fast or 0.0),
            "ema_slow": float(state.ema_slow or 0.0),
            "ema_calculation_mode": "candle_close",
        }

    # ------------------------------------------------------------------
    # Tick ingestion
    # ------------------------------------------------------------------
    def ingest_tick(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return

        key = (
            payload.get("instrument_key")
            or payload.get("instrumentKey")
            or payload.get("key")
        )
        if not key:
            return

        with self._lock:
            state = self._instruments.get(key)
        if state is None:
            return

        completed = (
            payload.get("completed_candle")
            or payload.get("completedCandle")
            or payload.get("latest_candle")
            or payload.get("latestCandle")
        )
        if isinstance(completed, dict):
            candle = self._extract_candle(completed)
            if candle is not None:
                self._on_completed_candle(state, candle)
                return

        ltp = payload.get("ltp") or payload.get("live_ltp") or payload.get("last_price")
        try:
            ltp = float(ltp)
        except (TypeError, ValueError):
            return

        ts_raw = (
            payload.get("timestamp")
            or payload.get("time")
            or payload.get("ts")
            or payload.get("updated_at")
        )
        ts = self._parse_timestamp(ts_raw) or int(time.time())

        # Feed index LTP to the isolation layer (best-effort, non-fatal)
        self._maybe_feed_index_ltp(state, ltp)

        self._on_tick(state, ts, ltp)

    def _extract_candle(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            return {
                "time": int(raw.get("time") or raw.get("timestamp") or raw.get("ts")),
                "open": float(raw["open"]),
                "high": float(raw["high"]),
                "low": float(raw["low"]),
                "close": float(raw["close"]),
            }
        except Exception:
            return None

    def _parse_timestamp(self, value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            n = float(value)
            if n > 1e12:
                n = n / 1000.0
            return int(n)
        except (TypeError, ValueError):
            pass
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return int(dt.timestamp())
        except Exception:
            return None

    def _on_tick(self, state: InstrumentState, ts: int, price: float) -> None:
        minute_ts = (ts // 60) * 60

        with self._lock:
            bar = state.active_bar
            if bar is None or bar.minute_ts == minute_ts:
                if bar is None:
                    bar = MinuteBar(minute_ts=minute_ts)
                    state.active_bar = bar
                bar.add_tick(price)
                return

            if minute_ts > bar.minute_ts:
                finalized = bar
                new_bar = MinuteBar(minute_ts=minute_ts)
                new_bar.add_tick(price)
                state.active_bar = new_bar

        self._finalize_bar(state, finalized)

    def _on_completed_candle(self, state: InstrumentState, candle: Dict[str, Any]) -> None:
        ts = int(candle["time"])

        today_str = today_ist_str()
        if ist_date_str(ts) == today_str and state.first_candle_time_today is None:
            levels = compute_opening_range_levels(candle)
            if levels:
                state.opening_range_levels = levels
                state.opening_range_candle_time = ts
            state.first_candle_time_today = ts

        close = float(candle["close"])
        with self._lock:
            prev_diff = state.prev_diff
            state.apply_close(ts, close, self._fast_alpha, self._slow_alpha)
            curr_diff = state.current_diff()
            state.prev_diff = curr_diff
            state.candles_considered_today += 1

        # Fan-out to the isolation layer (best-effort, non-fatal)
        self._notify_candle_listeners(state, candle)

        if prev_diff is None or curr_diff is None:
            return

        if candle["open"] == candle["high"] == candle["low"] == candle["close"]:
            return

        if prev_diff <= 0 and curr_diff > 0:
            cross_type = "bullish_cross"
        elif prev_diff >= 0 and curr_diff < 0:
            cross_type = "bearish_cross"
        else:
            return

        self._on_cross(state, candle, cross_type, curr_diff)

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------
    def finalize_current_minute(self, now_ts: Optional[int] = None) -> int:
        now_ts = now_ts or int(time.time())
        current_minute = (now_ts // 60) * 60

        finalized: List[tuple] = []
        with self._lock:
            for state in self._instruments.values():
                bar = state.active_bar
                if bar is None:
                    continue
                if bar.minute_ts < current_minute:
                    finalized.append((state, bar))
                    state.active_bar = None

        for state, bar in finalized:
            self._finalize_bar(state, bar)

        return len(finalized)

    def _finalize_bar(self, state: InstrumentState, bar: MinuteBar) -> None:
        candle = bar.to_candle()

        with self._lock:
            prev_close = state.last_close

        if bar.ticks == 0:
            if prev_close is None:
                return
            candle["open"] = prev_close
            candle["high"] = prev_close
            candle["low"] = prev_close
            candle["close"] = prev_close

        close_price = float(candle["close"])

        today_str = today_ist_str()
        ts = int(candle["time"])
        if ist_date_str(ts) == today_str and state.first_candle_time_today is None:
            levels = compute_opening_range_levels(candle)
            if levels:
                state.opening_range_levels = levels
                state.opening_range_candle_time = ts
            state.first_candle_time_today = ts

        with self._lock:
            prev_diff = state.prev_diff
            state.apply_close(ts, close_price, self._fast_alpha, self._slow_alpha)
            curr_diff = state.current_diff()
            state.prev_diff = curr_diff
            state.candles_considered_today += 1

        # Fan-out to the isolation layer (best-effort, non-fatal)
        self._notify_candle_listeners(state, candle)

        if bar.is_flat():
            self._persist_intraday_file(state)
            return

        if curr_diff is None or prev_diff is None:
            self._persist_intraday_file(state)
            return

        cross_type: Optional[str] = None
        if prev_diff <= 0 and curr_diff > 0:
            cross_type = "bullish_cross"
        elif prev_diff >= 0 and curr_diff < 0:
            cross_type = "bearish_cross"

        if cross_type is None:
            self._persist_intraday_file(state)
            return

        self._on_cross(state, candle, cross_type, curr_diff)

    # ------------------------------------------------------------------
    # Cross handling
    # ------------------------------------------------------------------
    def _on_cross(
        self,
        state: InstrumentState,
        candle: Dict[str, Any],
        cross_type: str,
        diff: float,
    ) -> None:
        ts = int(candle["time"])

        with self._lock:
            if ts in state.persisted_cross_times:
                return
            state.persisted_cross_times.add(ts)

        record = self._build_cross_record(state, candle, cross_type)

        with self._lock:
            state.crosses_today_records.append(record)
            self._last_cross_at = ts

        self._persist_intraday_file(state)

        logger.info(
            "EMA cross | %s | %s | %s | close=%.4f diff=%.6f",
            state.instrument_key, state.underlying, cross_type,
            record["close"], diff,
        )

        listener = self._cross_listener
        if listener is not None:
            try:
                listener(record)
            except Exception as exc:
                logger.warning("EMA cross listener failed: %s", exc)

    # ------------------------------------------------------------------
    # Listener registration (cross + candle)
    # ------------------------------------------------------------------
    def register_cross_listener(self, callback) -> None:
        self._cross_listener = callback
        logger.info("EMA cross listener registered")

    def register_candle_listener(self, callback) -> None:
        """
        Register a callable that receives every finalized minute candle.

        Called once per bar per instrument, AFTER EMA state has been
        advanced. Payload shape:

            {
                "instrument_key": str,
                "underlying":     str,
                "strike":         float | None,
                "option_type":    "CE" | "PE" | None,
                "expiry":         str | None,
                "trading_symbol": str | None,
                "time":           unix seconds,
                "open":           float,
                "high":           float,
                "low":            float,
                "close":          float,
                "ema_fast":       float,
                "ema_slow":       float,
            }
        """
        self._candle_listener = callback
        logger.info("EMA candle listener registered")

    def _notify_candle_listeners(
        self, state: InstrumentState, candle: Dict[str, Any]
    ) -> None:
        """Fan-out to the candle listener; never raises to the caller."""
        listener = getattr(self, "_candle_listener", None)
        if not callable(listener):
            return
        try:
            listener(
                {
                    "instrument_key": state.instrument_key,
                    "underlying": state.underlying,
                    "strike": state.strike,
                    "option_type": state.option_type,
                    "expiry": state.expiry,
                    "trading_symbol": state.trading_symbol,
                    "time": int(candle["time"]),
                    "open": float(candle["open"]),
                    "high": float(candle["high"]),
                    "low": float(candle["low"]),
                    "close": float(candle["close"]),
                    "ema_fast": float(state.ema_fast or 0.0),
                    "ema_slow": float(state.ema_slow or 0.0),
                }
            )
        except Exception as exc:
            logger.warning("EMA candle listener failed: %s", exc)

    # ------------------------------------------------------------------
    # Index LTP feed for the isolation layer
    # ------------------------------------------------------------------
    def _maybe_feed_index_ltp(
        self, state: InstrumentState, ltp: float
    ) -> None:
        """
        Best-effort push of an index instrument's LTP into the isolation
        layer. Used for the distance tiebreak when picking the day's
        isolated instrument. Never raises.
        """
        try:
            opt = str(state.option_type or "").upper()
            key = str(state.instrument_key or "")
            is_index = opt in ("", "INDEX") or "_INDEX|" in key
            if not is_index:
                return
            from ema_app.isolation.service import isolation_service  # type: ignore

            isolation_service.update_index_ltp(
                state.underlying or state.instrument_key, float(ltp)
            )
        except Exception:
            pass

    def mark_streamer_attached(self, attached: bool) -> None:
        self._streamer_attached = attached

    def streamer_attached(self) -> bool:
        return self._streamer_attached

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------
    def crosses_today(self) -> List[Dict[str, Any]]:
        with self._lock:
            out: List[Dict[str, Any]] = []
            for s in self._instruments.values():
                out.extend(s.crosses_today_records)
        out.sort(key=lambda c: int(c.get("cross_time") or 0))
        return out

    def crosses_for_instrument(self, instrument_key: str) -> List[Dict[str, Any]]:
        with self._lock:
            s = self._instruments.get(instrument_key)
            return list(s.crosses_today_records) if s else []

    def get_state(self, instrument_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            state = self._instruments.get(instrument_key)
            return state.snapshot() if state else None

    def get_opening_range(self, instrument_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            state = self._instruments.get(instrument_key)
            if state is None:
                return None
            return {
                "instrument_key": state.instrument_key,
                "underlying": state.underlying,
                "strike": state.strike,
                "option_type": state.option_type,
                "expiry": state.expiry,
                "trading_symbol": state.trading_symbol,
                "candle_time": state.opening_range_candle_time,
                "candle_time_iso": (
                    format_ist(state.opening_range_candle_time)
                    if state.opening_range_candle_time
                    else None
                ),
                "levels": state.opening_range_levels or {},
            }

    def status(self) -> Dict[str, Any]:
        with self._lock:
            now = now_ist()
            open_dt = parse_hhmm(ema_config.market_open, now)
            close_dt = parse_hhmm(ema_config.market_close, now)
            inside = open_dt <= now <= close_dt and now.weekday() < 5
            total_crosses = sum(
                len(s.crosses_today_records) for s in self._instruments.values()
            )
            return {
                "enabled": ema_config.enabled,
                "running": self.is_running(),
                "market_open": now.weekday() < 5 and open_dt <= now <= close_dt,
                "inside_session": inside,
                "instrument_count": len(self._instruments),
                "crosses_today": total_crosses,
                "last_cross_at": (
                    format_ist(self._last_cross_at) if self._last_cross_at else None
                ),
                "started_at": (
                    self._started_at.strftime("%Y-%m-%d %H:%M:%S")
                    if self._started_at
                    else None
                ),
                "streamer_attached": self._streamer_attached,
                "session_date": self._session_date,
            }


# Module-level singleton
ema_service = EmaService()