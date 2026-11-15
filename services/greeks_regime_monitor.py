# services/greeks_regime_monitor.py
"""Independent, read-only live options Greeks regime monitor.

Consumes the existing Upstox live-feed callback. It does not create strategy
signals, dummy orders, sandbox executions, or broker orders.

Portable mode is used because fixed local delta/rho thresholds are specific
to the calibration contracts. A per-option baseline is built from completed
minute observations. The first qualifying tick in each option/minute is
persisted to a daily JSONL audit file.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import greeks_regime_config as settings
from services.normalization import normalize_strike_type

logger = logging.getLogger(__name__)


class GreeksRegimeMonitor:
    """Thread-safe portable Greeks evaluator with minute-bucket persistence."""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        output_dir: str | Path | None = None,
        lookback: int | None = None,
        z_threshold: float | None = None,
        std_floor: float | None = None,
        timezone_name: str | None = None,
        fsync: bool | None = None,
    ) -> None:
        self.enabled = settings.GREEKS_REGIME_ENABLED if enabled is None else bool(enabled)
        self.output_dir = Path(output_dir if output_dir is not None else settings.GREEKS_REGIME_OUTPUT_DIR)
        self.lookback = max(5, int(lookback or settings.GREEKS_REGIME_LOOKBACK_MINUTES))
        self.z_threshold = float(z_threshold if z_threshold is not None else settings.GREEKS_REGIME_Z_THRESHOLD)
        self.std_floor = float(std_floor if std_floor is not None else settings.GREEKS_REGIME_STD_FLOOR)
        self.timezone = ZoneInfo(timezone_name or settings.GREEKS_REGIME_TIMEZONE)
        self.fsync = settings.GREEKS_REGIME_FSYNC if fsync is None else bool(fsync)
        self._lock = threading.RLock()
        self._states: dict[str, dict[str, Any]] = {}
        self._loaded_date: str | None = None
        self._state_date: str | None = None
        self._saved_minute_keys: set[tuple[str, str]] = set()
        self._saved_count = 0
        self._skipped_count = 0
        self._error_count = 0

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _side_from_greeks(delta: float, rho: float) -> str:
        if delta > 0.20 and rho > 0.5:
            return "CALL"
        if delta < -0.20 and rho < -0.5:
            return "PUT"
        return "ATYPICAL"

    def _daily_path(self, day_key: str) -> Path:
        return self.output_dir / f"greeks_regime_snapshots_{day_key}.jsonl"

    def _load_day_index(self, day_key: str) -> None:
        if self._loaded_date == day_key:
            return
        self._saved_minute_keys.clear()
        path = self._daily_path(day_key)
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            item = json.loads(line)
                        except (TypeError, ValueError):
                            continue
                        key = str(item.get("instrument_key") or "")
                        minute = str(item.get("minute_bucket") or "")
                        if key and minute:
                            self._saved_minute_keys.add((minute, key))
            except OSError:
                logger.exception("[greeks-regime] unable to load dedup index from %s", path)
        self._loaded_date = day_key

    def _get_state(self, instrument_key: str) -> dict[str, Any]:
        state = self._states.get(instrument_key)
        if state is None:
            state = {
                "history": deque(maxlen=self.lookback),
                "current_bucket": None,
                "current_sample": None,
            }
            self._states[instrument_key] = state
        return state

    def _finalize_previous_minute(self, state: dict[str, Any], new_bucket: datetime) -> None:
        previous_bucket = state.get("current_bucket")
        previous_sample = state.get("current_sample")
        if previous_bucket is not None and previous_sample is not None and new_bucket > previous_bucket:
            # Keep only the last valid tick observed in each minute.
            state["history"].append(previous_sample)

    @staticmethod
    def _premium_delta_5m(history: deque, current_bucket: datetime, premium: float) -> float | None:
        target = current_bucket - timedelta(minutes=5)
        for sample in reversed(history):
            if sample["bucket"] == target:
                return premium - float(sample["premium"])
        return None

    @staticmethod
    def _confidence(score: float) -> str:
        if score >= 65.0 or score <= 35.0:
            return "HIGH"
        if 45.0 < score < 55.0:
            return "LOW"
        return "MEDIUM"

    @staticmethod
    def _classify(z_bull: float, moneyness: float | None, threshold: float) -> tuple[str, str]:
        """Apply the portable verdict rules in greeks_regime_formula.md §4.4."""
        if moneyness is not None:
            if moneyness > 1.02 and z_bull > 0:
                return "BULLISH", "moneyness>1.02_and_z_bull>0"
            if moneyness < 0.98 and z_bull < 0:
                return "BEARISH", "moneyness<0.98_and_z_bull<0"
            if moneyness > 1.02 and z_bull <= 0:
                return "LEAN_BULLISH", "moneyness_only_bullish"
            if moneyness < 0.98 and z_bull >= 0:
                return "LEAN_BEARISH", "moneyness_only_bearish"
            return "NEUTRAL", "moneyness_neutral"

        if z_bull > threshold:
            return "BULLISH", f"z_bull>{threshold:g}"
        if z_bull < -threshold:
            return "BEARISH", f"z_bull<{-threshold:g}"
        return "NEUTRAL", "z_bull_neutral"

    @staticmethod
    def _moneyness(spot: float | None, strike: float | None) -> float | None:
        if spot is None or strike is None or spot <= 0 or strike <= 0:
            return None
        value = spot / strike
        return value if math.isfinite(value) else None

    def _build_snapshot(
        self, *, entry: dict[str, Any], tick: dict[str, Any], side: str,
        greeks: dict[str, float], premium: float, received_at: datetime,
        bucket: datetime, history: deque, z_delta: float, z_bull: float,
        premium_delta_5m: float | None, mean: float, std: float,
        spot: float | None, strike: float | None, moneyness: float | None,
    ) -> dict[str, Any]:
        verdict, condition = self._classify(z_bull, moneyness, self.z_threshold)

        # Formula §3.7: side-signed delta z-score plus the side-independent
        # true-moneyness term. z_bull already equals s * z_delta.
        if moneyness is not None:
            score = (
                50.0
                + 25.0 * math.tanh(z_bull / 1.0)
                + 25.0 * math.tanh((moneyness - 1.0) / 0.05)
            )
        else:
            score = 50.0 + 50.0 * math.tanh(z_bull / 1.0)

        premium_confirmation: str | None = None
        premium_conflict = False
        if premium_delta_5m is not None and verdict != "NEUTRAL":
            if premium_delta_5m == 0:
                premium_confirmation = "FLAT"
            else:
                premium_supports_bull = (
                    premium_delta_5m > 0 if side == "CALL"
                    else premium_delta_5m < 0
                )
                premium_supports_bear = (
                    premium_delta_5m < 0 if side == "CALL"
                    else premium_delta_5m > 0
                )
                is_bullish = verdict in {"BULLISH", "LEAN_BULLISH"}
                premium_supports = (
                    premium_supports_bull if is_bullish
                    else premium_supports_bear
                )
                premium_conflict = not premium_supports
                premium_confirmation = (
                    "SUPPORTS" if premium_supports else "CONTRADICTS"
                )
                if premium_conflict:
                    score -= 15.0

        score = max(0.0, min(100.0, score))
        if score > 65.0 or score < 35.0:
            confidence = "HIGH"
        elif 45.0 < score < 55.0:
            confidence = "LOW"
        else:
            confidence = "MEDIUM"

        samples = list(history)
        return {
            "schema_version": 2,
            "event_type": "GREEKS_REGIME_CONDITION_MET",
            "feed_source": "OPTION_FULL",
            "received_at": received_at.isoformat(),
            "minute_bucket": bucket.isoformat(),
            "instrument_key": str(entry.get("instrument_key") or ""),
            "trading_symbol": entry.get("trading_symbol"),
            "underlying": entry.get("underlying"),
            "strike_price": entry.get("strike_price"),
            "strike_type": normalize_strike_type(entry.get("strike_type")),
            "expiry": entry.get("expiry"),
            "side": side,
            "verdict": verdict,
            "condition": condition,
            "score": round(score, 4),
            "confidence": confidence,
            "z_delta": round(z_delta, 6),
            "z_bull": round(z_bull, 6),
            "spot": spot,
            "moneyness": round(moneyness, 8) if moneyness is not None else None,
            "option_feed": {
                "up": spot,
                "iv": greeks.get("iv"),
                "op": greeks.get("op"),
            },
            "baseline": {
                "lookback_observations": len(samples),
                "delta_mean": round(mean, 8),
                "delta_std": round(std, 8),
                "first_minute": samples[0]["bucket"].isoformat(),
                "last_minute": samples[-1]["bucket"].isoformat(),
            },
            "premium_delta_5m": (
                round(premium_delta_5m, 4)
                if premium_delta_5m is not None else None
            ),
            "premium_confirmation": premium_confirmation,
            "premium_conflict": premium_conflict,
            "premium": premium,
            "ltp": tick.get("ltp"),
            "prev_close": tick.get("prev_close"),
            "volume": tick.get("volume"),
            "candle_1m": tick.get("candle_1m"),
            "greeks": {
                name: greeks.get(name)
                for name in ("delta", "theta", "gamma", "vega", "rho")
            },
            "exchange_ltt": tick.get("ltt"),
        }

    def _append_snapshot(self, snapshot: dict[str, Any], day_key: str) -> bool:
        instrument_key = snapshot["instrument_key"]
        minute_bucket = snapshot["minute_bucket"]
        dedup_key = (minute_bucket, instrument_key)
        self._load_day_index(day_key)
        if dedup_key in self._saved_minute_keys:
            self._skipped_count += 1
            return False
        path = self._daily_path(day_key)
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(encoded + "\n")
                handle.flush()
                if self.fsync:
                    import os
                    os.fsync(handle.fileno())
        except (OSError, TypeError, ValueError):
            self._error_count += 1
            logger.exception("[greeks-regime] snapshot write failed instrument=%s minute=%s", instrument_key, minute_bucket)
            return False
        self._saved_minute_keys.add(dedup_key)
        self._saved_count += 1
        logger.info(
            "[greeks-regime] saved %s %s %s score=%.1f confidence=%s",
            instrument_key, snapshot["verdict"], minute_bucket,
            snapshot["score"], snapshot["confidence"],
        )
        return True

    def on_tick(
        self, entry: dict[str, Any], tick: dict[str, Any],
        received_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Evaluate one Option Full Feed tick; reject LTPC-only updates."""
        if not self.enabled or not isinstance(entry, dict) or not isinstance(tick, dict):
            return None

        # This explicit parser marker is mandatory. Greeks from any other
        # feed branch must not be treated as the required Option Full Feed.
        if tick.get("option_full_feed") is not True:
            self._skipped_count += 1
            return None

        strike_type = normalize_strike_type(entry.get("strike_type"))
        if strike_type not in {"CE", "PE"} or bool(entry.get("is_index")):
            return None

        raw = tick.get("option_greeks")
        if not isinstance(raw, dict):
            return None
        greeks = {
            name: self._number(raw.get(name))
            for name in ("delta", "theta", "gamma", "vega", "rho", "up", "iv", "op")
        }
        delta, rho = greeks["delta"], greeks["rho"]
        if delta is None or rho is None:
            return None

        side = self._side_from_greeks(delta, rho)
        expected_side = "CALL" if strike_type == "CE" else "PUT"
        if side != expected_side:
            self._skipped_count += 1
            return None

        premium = self._number(tick.get("ltp"))
        if premium is None:
            premium = greeks.get("op")
        if premium is None and isinstance(tick.get("candle_1m"), dict):
            premium = self._number(tick["candle_1m"].get("close"))
        if premium is None or premium <= 0:
            return None

        spot = greeks.get("up")
        strike = self._number(entry.get("strike_price"))
        moneyness = self._moneyness(spot, strike)

        now = received_at or datetime.now(self.timezone)
        now = (
            now.replace(tzinfo=self.timezone)
            if now.tzinfo is None else now.astimezone(self.timezone)
        )
        bucket = now.replace(second=0, microsecond=0)
        day_key = bucket.strftime("%Y%m%d")
        instrument_key = str(entry.get("instrument_key") or "").strip()
        if not instrument_key:
            return None

        with self._lock:
            # A fresh rolling baseline is required for every market date.
            if self._state_date != day_key:
                self._states.clear()
                self._state_date = day_key

            state = self._get_state(instrument_key)
            current_bucket = state.get("current_bucket")
            if current_bucket is not None and bucket < current_bucket:
                self._skipped_count += 1
                return None
            if current_bucket is None:
                state["current_bucket"] = bucket
            elif bucket > current_bucket:
                self._finalize_previous_minute(state, bucket)
                state["current_bucket"] = bucket
                state["current_sample"] = None

            history = state["history"]
            if len(history) < self.lookback:
                state["current_sample"] = {
                    "bucket": bucket, "delta": delta, "premium": premium,
                }
                return None

            deltas = [float(item["delta"]) for item in history]
            mean = sum(deltas) / len(deltas)
            variance = sum((value - mean) ** 2 for value in deltas) / len(deltas)
            std = math.sqrt(variance)
            if not math.isfinite(std) or std < self.std_floor:
                state["current_sample"] = {
                    "bucket": bucket, "delta": delta, "premium": premium,
                }
                self._skipped_count += 1
                return None

            z_delta = (delta - mean) / std
            z_bull = z_delta if side == "CALL" else -z_delta
            verdict, _ = self._classify(z_bull, moneyness, self.z_threshold)
            state["current_sample"] = {
                "bucket": bucket, "delta": delta, "premium": premium,
            }
            if verdict == "NEUTRAL":
                return None

            premium_delta_5m = self._premium_delta_5m(history, bucket, premium)
            snapshot = self._build_snapshot(
                entry=entry,
                tick=tick,
                side=side,
                greeks={
                    key: value for key, value in greeks.items()
                    if value is not None
                },
                premium=premium,
                received_at=now,
                bucket=bucket,
                history=history,
                z_delta=z_delta,
                z_bull=z_bull,
                premium_delta_5m=premium_delta_5m,
                mean=mean,
                std=std,
                spot=spot,
                strike=strike,
                moneyness=moneyness,
            )
            return snapshot if self._append_snapshot(snapshot, day_key) else None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "required_feed_source": "OPTION_FULL",
                "moneyness_enabled": True,
                "twin_leg_confirmation": False,
                "tracked_options": len(self._states),
                "saved_events": self._saved_count,
                "deduplicated_events": self._skipped_count,
                "write_errors": self._error_count,
                "lookback_minutes": self.lookback,
                "z_threshold": self.z_threshold,
                "output_dir": str(self.output_dir),
            }


greeks_regime_monitor = GreeksRegimeMonitor()
