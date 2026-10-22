# File: services/strategy_selection_service.py
"""Stage 1 — index strategy selection.

Filters the strike window around the Opening Range midpoint, fetches
historical 1-min candles via Upstox for every selected strike, and
computes the "last day's last cross" inside the configured tail
window (default: last 30 minutes of the last trading day).

Output layout::

    data/strategy/stage_1_initial/<TICKER>/<INDEX_NAME>/
        stage_1_summary.json
        <strike>_<TYPE>/
            strategy_meta.json
            index_opening_range.json   <- INDEX OR levels (audit copy)
            opening_range.json         <- STRIKE's own OR levels
                                          (written by opening_range_service
                                           and mirrored here by this service)
            candles.json
            ema_crosses.json
            last_day_cross.json
            live_crosses.json

Stage 2 (services/strategy_isolation_service.py) reads this tree,
checks today's first-candle cross, and copies the selected strikes
into data/strategy/stage_2_isolation/.

This service is idempotent. Failures per index/strike are logged and
reported in the summary; the service never raises.

OR retry
--------
The live WebSocket feed can finalize the index 09:15 candle late, so
a fresh `opening_range.json` may not exist yet at 09:16:30. When that
happens, `_process_index` forces a per-instrument OR recalculation
(`opening_range_service.calculate_one(index_key)`) up to
`_OR_RETRY_ATTEMPTS` times, sleeping `_OR_RETRY_SLEEP_SECONDS`
between attempts, and re-reads the OR payload each time. This removes
the `or_unavailable:not_calculated` race that previously caused the
whole strategy pipeline to produce zero contracts when the index
candle arrived a minute or two late.

Live-feed subscription + per-strike OR
--------------------------------------
When a stage-1 strike is resolved to a real Upstox instrument_key
(inside `_enrich_with_upstox`), this service now:

    * subscribes that key to the live WebSocket feed so the S1/S2
      R2/S3 trackers can read its LTP as soon as the market opens, and
    * calculates the strike's OWN opening range (from its own 09:15
      1-minute candle, via the existing REST fallback) and writes the
      result to `<strike folder>/opening_range.json`.

Both were missing previously: the trackers read
`<strike folder>/opening_range.json` to learn each contract's R2/S3
level, and they read `live_feed_service.get_latest_ltp()` to detect
touches. Without (1) every `get_latest_ltp()` returned None, and
without (2) every `isolation_level` came back None — so both
trackers skipped every contract and Stage 4 reported
`no_activations` even though Stage 1/2 had produced 68 contracts.

Catch-up semantics
------------------
The unified refresh calls this with ``mode=reason`` and
``catchup=True`` when the project starts (or daily / hard refresh
fires) after the scheduled 09:16:30 IST time and still inside the
catch-up window (09:15 – 23:59 IST). The summary and the log lines
record:

    mode              : startup | daily | manual | scheduled
    catchup           : bool
    post_market       : bool (true when the run happens after 15:30 IST)
    scheduled_time_ist: "09:16:30"
    actual_time_ist   : "HH:MM:SS"
"""

from __future__ import annotations

import json
import logging
import math
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from core import config
from core.logger import get_logger
from services.instrument_registry import instrument_registry
from services.opening_range_service import opening_range_service
from services.token_service import token_service

logger = get_logger(__file__)

_UPSTOX_BASE = "https://api.upstox.com"
_HTTP_TIMEOUT = 20

# Cash-market close in the market timezone. Anything after this is
# recorded as post-market in the summary.
_MARKET_CLOSE_TIME = dtime(15, 30)

# ---- OR retry tuning ------------------------------------------------------- #
# When the index OR file is missing at Stage-1 time (late 09:15 candle),
# force a single-instrument OR recalculation this many times, sleeping
# _OR_RETRY_SLEEP_SECONDS between attempts. A short retry loop is enough
# because the REST fallback in opening_range_service usually succeeds on
# the first attempt; the retries cover transient REST failures.
_OR_RETRY_ATTEMPTS = 3
_OR_RETRY_SLEEP_SECONDS = 2.0

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _safe_folder(name: Any) -> str:
    text = str(name or "unknown").strip()
    for ch in ("|", "/", "\\", ":", "*", "?", '"', "<", ">", " "):
        text = text.replace(ch, "_")
    return text.strip("._") or "unknown"

def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)

def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _market_tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.MARKET_TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Kolkata")

def _market_now() -> datetime:
    return datetime.now(_market_tz())

def _market_hhmmss() -> str:
    return _market_now().strftime("%H:%M:%S")

def _post_market() -> bool:
    """True after the Indian cash-market close (15:30 IST)."""
    return _market_now().time() > _MARKET_CLOSE_TIME

def _scheduled_stage_1_hhmm() -> str:
    """09:16:30 IST by default (OR + 20 s)."""
    base = (
        config.OPENING_RANGE_HOUR * 3600
        + config.OPENING_RANGE_MINUTE * 60
        + config.OPENING_RANGE_SECOND
    )
    total = (base + 20) % 86400
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"

def _round_levels(levels: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(levels, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in levels.items():
        if isinstance(v, (int, float)):
            out[k] = round(float(v), 4)
        else:
            out[k] = v
    return out

def _resolve_buffer(typical: float, avg: float, mode: str) -> tuple[float, float]:
    if mode == "typical_plus_minus_avg":
        return -(typical + avg), (typical + avg)
    return -typical, typical

def _placeholder_key(ticker: str, strike: int, strike_type: str) -> str:
    return f"STRATEGY|{_safe_folder(ticker)}_{strike}_{strike_type}"

# ---------------------------------------------------------------------------
# EMA math (self-contained — mirrors the live engine)
# ---------------------------------------------------------------------------
def _ema(values: list[float], period: int) -> list[float | None]:
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1.0)
    prev = seed
    for i in range(period, n):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out

def _compute_crosses(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return every fast/slow EMA cross in the candle series.

    Uses the same periods as the live engine (config.EMA_FAST_PERIOD /
    EMA_SLOW_PERIOD) so the stage-1 output is directly comparable to
    what the live engine emits.
    """
    if len(candles) < max(config.EMA_SLOW_PERIOD + 2, 3):
        return []
    closes = [float(c["close"]) for c in candles]
    fast = _ema(closes, config.EMA_FAST_PERIOD)
    slow = _ema(closes, config.EMA_SLOW_PERIOD)

    out: list[dict[str, Any]] = []
    for i in range(1, len(closes)):
        f0, s0 = fast[i - 1], slow[i - 1]
        f1, s1 = fast[i], slow[i]
        if None in (f0, s0, f1, s1):
            continue
        prev_diff = f0 - s0
        now_diff = f1 - s1
        if prev_diff <= 0 < now_diff:
            kind = "bullish"
        elif prev_diff >= 0 > now_diff:
            kind = "bearish"
        else:
            continue
        out.append(
            {
                "timestamp": candles[i]["timestamp"],
                "type": kind,
                "fast_ema": round(f1, 4),
                "slow_ema": round(s1, 4),
                "ema_diff": round(now_diff, 4),
                "close": closes[i],
                "volume": candles[i].get("volume", 0.0),
            }
        )
    return out

def _last_day_last_cross(
    candles: list[dict[str, Any]],
    today_iso: str,
    lookback_minutes: int,
) -> dict[str, Any] | None:
    """Return the last cross in the tail of the last trading day.

    Steps:
      1. Restrict to candles strictly before ``today_iso``.
      2. Find the last distinct trading day.
      3. Take that day's last ``lookback_minutes`` candles.
      4. Compute crosses on the whole prior-day slice (so EMA is warm).
      5. Return the last cross whose timestamp falls in the tail window,
         or None when the tail contains no cross.
    """
    if not candles:
        return None

    prev = [c for c in candles if str(c.get("timestamp", ""))[:10] < today_iso]
    if not prev:
        return None

    days = sorted({str(c["timestamp"])[:10] for c in prev})
    if not days:
        return None
    last_day = days[-1]

    day_candles = [c for c in prev if str(c["timestamp"])[:10] == last_day]
    if len(day_candles) < max(config.EMA_SLOW_PERIOD + 2, 3):
        return None

    tail_start_iso = day_candles[-lookback_minutes]["timestamp"]

    crosses = _compute_crosses(prev)
    tail_crosses = [c for c in crosses if str(c["timestamp"]) >= str(tail_start_iso)]
    if not tail_crosses:
        return None

    last = tail_crosses[-1]
    return {
        "type": last["type"],
        "timestamp": last["timestamp"],
        "fast_ema": last["fast_ema"],
        "slow_ema": last["slow_ema"],
        "ema_diff": last["ema_diff"],
        "close": last["close"],
        "window_start": tail_start_iso,
        "window_minutes": lookback_minutes,
        "day": last_day,
        "crosses_in_window": len(tail_crosses),
    }

# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class StrategySelectionService:
    """Stage 1 — buffer filter + history fetch + last-day-cross analysis."""

    # ------------------------------------------------------------------ #
    # Public entry point                                                 #
    # ------------------------------------------------------------------ #
    def run(
        self,
        reason: str = "manual",
        *,
        mode: str | None = None,
        catchup: bool = False,
    ) -> dict[str, Any]:
        """Stage 1 entry point.

        Parameters
        ----------
        reason  : trigger label ("startup" / "daily" / "manual" / "scheduled")
        mode    : alias for reason; separate so a caller can pass a distinct
                  scheduled mode.
        catchup : True when the run is a late catch-up.
        """
        effective_mode = str(mode or reason or "manual")
        post_market = _post_market()
        scheduled_hhmm = _scheduled_stage_1_hhmm()
        actual_hhmmss = _market_hhmmss()

        logger.info(
            "[strategy:%s] stage_1 run (mode=%s catchup=%s post_market=%s "
            "scheduled=%s actual=%s)",
            reason,
            effective_mode,
            catchup,
            post_market,
            scheduled_hhmm,
            actual_hhmmss,
        )

        if not config.STRATEGY_ENABLED:
            return {
                "status": "disabled",
                "day": _market_now().date().isoformat(),
                "reason": reason,
                "mode": effective_mode,
                "catchup": bool(catchup),
                "post_market": post_market,
                "scheduled_time_ist": scheduled_hhmm,
                "actual_time_ist": actual_hhmmss,
                "indexes": [],
                "stage": "stage_1",
                "generated_at": _now_iso(),
            }

        indexes = list(config.STRATEGY_INDEXES or [])
        if not indexes:
            return {
                "status": "no_indexes",
                "day": _market_now().date().isoformat(),
                "reason": reason,
                "mode": effective_mode,
                "catchup": bool(catchup),
                "post_market": post_market,
                "scheduled_time_ist": scheduled_hhmm,
                "actual_time_ist": actual_hhmmss,
                "indexes": [],
                "stage": "stage_1",
                "generated_at": _now_iso(),
            }

        buffers = [float(i.get("typical_buffer_points") or 0.0) for i in indexes]
        avg_buffer = sum(buffers) / len(buffers) if buffers else 0.0

        logger.info(
            "[strategy:%s] stage_1 START — indexes=%d avg_buffer=%.2f "
            "mode=%s catchup=%s post_market=%s",
            reason,
            len(indexes),
            avg_buffer,
            config.STRATEGY_BUFFER_MODE,
            catchup,
            post_market,
        )

        per_index: list[dict[str, Any]] = []
        for idx in indexes:
            ticker = str(idx.get("ticker") or idx.get("index_name") or "?")
            try:
                result = self._process_index(idx, avg_buffer)
            except Exception as exc:
                logger.exception("[strategy:%s] stage_1 %s failed", reason, ticker)
                result = {
                    "status": "error",
                    "ticker": ticker,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            per_index.append(result)

        # -----------------------------------------------------------------
        # Resolve the summary's top-level "day".
        #
        # The per-index summaries carry their own "day" (the OR day they
        # were computed from), but the watchdog (_stage1_ran_today) and
        # downstream consumers (dashboard, /admin/strategy) check the
        # TOP-LEVEL summary's "day" against today's date. Surface it here
        # so a successful Stage-1 run is correctly recognised.
        #
        # - If at least one index produced a real "day", use that value.
        # - If every index was skipped (e.g. or_unavailable), fall back
        #   to today's calendar date: the pipeline *did* run today, which
        #   is the exact fact the watchdog needs to confirm.
        # -----------------------------------------------------------------
        day_for_summary = next(
            (p.get("day") for p in per_index if p.get("day")),
            _market_now().date().isoformat(),
        )

        summary = {
            "status": "ok",
            "stage": "stage_1",
            "day": day_for_summary,          # <-- top-level day (watchdog fix)
            "reason": reason,
            "mode": effective_mode,
            "catchup": bool(catchup),
            "post_market": post_market,
            "scheduled_time_ist": scheduled_hhmm,
            "actual_time_ist": _market_hhmmss(),
            "generated_at": _now_iso(),
            "avg_buffer_points": avg_buffer,
            "buffer_mode": config.STRATEGY_BUFFER_MODE,
            "indexes": per_index,
        }

        # Write to <stage_1>/stage_1_summary.json (canonical) and also to
        # <STRATEGY_BASE_DIR>/strategy_summary.json (legacy path used by
        # /admin/strategy).
        base = Path(config.STRATEGY_BASE_DIR)
        stage1_dir = base / config.STRATEGY_STAGE_1_NAME
        try:
            stage1_dir.mkdir(parents=True, exist_ok=True)
            _write_json(stage1_dir / "stage_1_summary.json", summary)
            _write_json(base / "strategy_summary.json", summary)
        except Exception:
            logger.exception("[strategy:%s] stage_1 summary write failed", reason)

        ok = sum(1 for p in per_index if p.get("status") == "ok")
        skipped = sum(1 for p in per_index if p.get("status") == "skipped")
        errored = sum(1 for p in per_index if p.get("status") == "error")
        logger.info(
            "[strategy:%s] stage_1 COMPLETE — ok=%d skipped=%d errors=%d "
            "total=%d (mode=%s catchup=%s post_market=%s)",
            reason,
            ok,
            skipped,
            errored,
            len(indexes),
            effective_mode,
            catchup,
            post_market,
        )
        return summary

    # ------------------------------------------------------------------ #
    # Per-index processing                                               #
    # ------------------------------------------------------------------ #
    def _process_index(
        self,
        idx: dict[str, Any],
        avg_buffer: float,
    ) -> dict[str, Any]:
        ticker = str(idx.get("ticker") or "").strip() or "UNKNOWN"
        index_name = str(idx.get("index_name") or ticker).strip()
        index_key = str(idx.get("instrument_key") or "").strip()
        strike_step = int(idx.get("strike_step") or 50)
        typical = float(idx.get("typical_buffer_points") or 0.0)
        denominator = idx.get("denominator_points")

        if not index_key:
            return {
                "status": "skipped",
                "ticker": ticker,
                "reason": "missing_instrument_key",
            }

        # ---- 0. Ensure the index key is registered ----------------------- #
        if instrument_registry.get(index_key) is None:
            try:
                entry = instrument_registry.register_adhoc(
                    index_key,
                    trading_symbol=index_name,
                )
                if entry and not entry.get("ema_path"):
                    canonical_dir = (
                        Path(config.LIVE_DATA_DIR) / "index" / _safe_folder(index_key)
                    )
                    entry["ema_path"] = str(canonical_dir / "ema_crosses.json")
                    entry["candles_path"] = str(canonical_dir / "candles.json")
            except Exception:
                logger.exception("[strategy] failed ad-hoc registering %s", index_key)

        # ---- 1. Opening Range payload (INDEX's own OR) ------------------- #
        # Read once. If the OR file is missing (late 09:15 index candle),
        # force a single-instrument OR recalc and re-read — up to
        # _OR_RETRY_ATTEMPTS times. This is the retry-once guard that
        # closes the OR → Stage-1 race.
        or_payload = opening_range_service.get_opening_range(index_key)
        or_retry_attempts = 0

        if not or_payload.get("available") or not or_payload.get("is_today"):
            for attempt in range(1, _OR_RETRY_ATTEMPTS + 1):
                logger.info(
                    "[strategy] %s: OR not ready (reason=%s is_today=%s); "
                    "forcing recalc attempt %d/%d",
                    ticker,
                    or_payload.get("reason"),
                    or_payload.get("is_today"),
                    attempt,
                    _OR_RETRY_ATTEMPTS,
                )
                try:
                    opening_range_service.calculate_one(index_key)
                except Exception:
                    logger.exception(
                        "[strategy] %s: OR recalc attempt %d failed",
                        ticker,
                        attempt,
                    )
                or_retry_attempts += 1
                or_payload = opening_range_service.get_opening_range(index_key)
                if or_payload.get("available") and or_payload.get("is_today"):
                    logger.info(
                        "[strategy] %s: OR recovered after %d recalc attempt(s)",
                        ticker,
                        attempt,
                    )
                    break
                if attempt < _OR_RETRY_ATTEMPTS:
                    _time.sleep(_OR_RETRY_SLEEP_SECONDS)

        if not or_payload.get("available"):
            return {
                "status": "skipped",
                "ticker": ticker,
                "reason": f"or_unavailable:{or_payload.get('reason')}",
                "or_retry_attempts": or_retry_attempts,
            }
        if not or_payload.get("is_today"):
            return {
                "status": "skipped",
                "ticker": ticker,
                "reason": "or_stale_not_today",
                "or_day": or_payload.get("day"),
                "or_retry_attempts": or_retry_attempts,
            }

        levels = _round_levels(or_payload.get("levels") or {})
        first_candle = or_payload.get("first_candle") or {}
        or_day = or_payload.get("day")
        or_candle_source = or_payload.get("candle_source")

        atm_ref = levels.get("A") or first_candle.get("close")
        if atm_ref is None:
            return {"status": "skipped", "ticker": ticker, "reason": "no_atm_reference"}
        try:
            atm_ref = float(atm_ref)
        except (TypeError, ValueError):
            return {
                "status": "skipped",
                "ticker": ticker,
                "reason": "invalid_atm_reference",
            }

        # ---- 2. Strike window -------------------------------------------- #
        lower_off, upper_off = _resolve_buffer(
            typical=typical,
            avg=avg_buffer,
            mode=config.STRATEGY_BUFFER_MODE,
        )
        lower_bound = atm_ref + lower_off
        upper_bound = atm_ref + upper_off
        lower_strike = int(math.floor(lower_bound / strike_step) * strike_step)
        upper_strike = int(math.ceil(upper_bound / strike_step) * strike_step)
        if upper_strike < lower_strike:
            return {"status": "skipped", "ticker": ticker, "reason": "empty_window"}
        strikes = list(range(lower_strike, upper_strike + strike_step, strike_step))
        atm_strike = int(round(atm_ref / strike_step) * strike_step)

        # ---- 3. Target folder (stage 1) --------------------------------- #
        base_dir = (
            Path(config.STRATEGY_BASE_DIR)
            / config.STRATEGY_STAGE_1_NAME
            / _safe_folder(ticker)
            / _safe_folder(index_name)
        )
        base_dir.mkdir(parents=True, exist_ok=True)

        # ---- 4. Materialise per-strike folders -------------------------- #
        today_iso = or_day or date.today().isoformat()
        records: list[dict[str, Any]] = []

        for strike in strikes:
            for s_type in ("CE", "PE"):
                strike_dir = base_dir / f"{strike}_{s_type}"
                strike_dir.mkdir(parents=True, exist_ok=True)

                placeholder_key = _placeholder_key(ticker, strike, s_type)
                trading_symbol = f"{ticker} {strike} {s_type}"

                meta = {
                    "stage": "stage_1",
                    "index_name": index_name,
                    "ticker": ticker,
                    "index_instrument_key": index_key,
                    "day": or_day,
                    "strike_price": strike,
                    "strike_type": s_type,
                    "strike_step": strike_step,
                    "atm_reference": round(atm_ref, 4),
                    "atm_strike": atm_strike,
                    "buffer_points": typical,
                    "avg_buffer_points": avg_buffer,
                    "buffer_mode": config.STRATEGY_BUFFER_MODE,
                    "denominator_points": denominator,
                    "or_levels": levels,
                    "or_candle_source": or_candle_source,
                    "placeholder_instrument_key": placeholder_key,
                    "generated_at": _now_iso(),
                }
                _write_json(strike_dir / "strategy_meta.json", meta)

                # ------------------------------------------------------------------
                # IMPORTANT: DO NOT clobber the strike's own opening_range.json.
                # ------------------------------------------------------------------
                # `opening_range.json` inside each strike folder is owned by
                # `opening_range_service` and must contain THAT strike's own
                # OR levels (computed from the strike's own 09:15 1-minute
                # candle). The index's OR levels used for strike-window
                # selection are recorded under a separate filename so a reader
                # can always tell the two apart.
                _write_json(
                    strike_dir / "index_opening_range.json",
                    {
                        "instrument_key": index_key,
                        "day": or_day,
                        "levels": levels,
                        "first_candle": first_candle,
                        "candle_source": or_candle_source,
                        "source": "strategy_selection",
                        "generated_at": _now_iso(),
                    },
                )

                # Register in the live registry so /live/* triple lookups
                # work as soon as stage 1 completes.
                self._register_strike(
                    key=placeholder_key,
                    ticker=ticker,
                    strike=strike,
                    strike_type=s_type,
                    trading_symbol=trading_symbol,
                    expiry=or_day,
                )

                # Placeholder files (only created if missing; the strike's
                # own candles / crosses / last-day-cross are written later by
                # _enrich_with_upstox).
                if not (strike_dir / "candles.json").exists():
                    _write_json(
                        strike_dir / "candles.json",
                        {
                            "instrument_key": placeholder_key,
                            "underlying": ticker,
                            "strike_price": strike,
                            "strike_type": s_type,
                            "trading_symbol": trading_symbol,
                            "day": or_day,
                            "candles": [],
                        },
                    )
                if not (strike_dir / "ema_crosses.json").exists():
                    _write_json(
                        strike_dir / "ema_crosses.json",
                        {
                            "instrument_key": placeholder_key,
                            "underlying": ticker,
                            "strike_price": strike,
                            "strike_type": s_type,
                            "trading_symbol": trading_symbol,
                            "expiry": or_day,
                            "crosses": [],
                        },
                    )
                if not (strike_dir / "live_crosses.json").exists():
                    _write_json(
                        strike_dir / "live_crosses.json",
                        {"day": or_day, "cross_count": 0, "crosses": []},
                    )
                if not (strike_dir / "last_day_cross.json").exists():
                    _write_json(
                        strike_dir / "last_day_cross.json",
                        {
                            "type": None,
                            "timestamp": None,
                            "day": None,
                            "window_minutes": config.STRATEGY_ISOLATION_LOOKBACK_MINUTES,
                            "crosses_in_window": 0,
                            "generated_at": _now_iso(),
                        },
                    )

                records.append(
                    {
                        "strike": strike,
                        "type": s_type,
                        "folder": str(strike_dir),
                        "placeholder_instrument_key": placeholder_key,
                    }
                )

        # ---- 5. Upstox enrichment (history + last-day-cross + feed + OR) - #
        enrichment = {
            "resolved": 0,
            "subscribed": 0,
            "or_written": 0,
            "total": 0,
        }
        if config.STRATEGY_FETCH_HISTORY:
            enrichment = self._enrich_with_upstox(
                ticker=ticker,
                base_dir=base_dir,
                strikes=strikes,
                today_iso=today_iso,
            )

        index_summary = {
            "stage": "stage_1",
            "index_name": index_name,
            "ticker": ticker,
            "index_instrument_key": index_key,
            "day": or_day,
            "or_levels": levels,
            "or_first_candle": first_candle,
            "or_candle_source": or_candle_source,
            "or_retry_attempts": or_retry_attempts,
            "atm_reference": round(atm_ref, 4),
            "atm_strike": atm_strike,
            "buffer_points": typical,
            "avg_buffer_points": avg_buffer,
            "buffer_mode": config.STRATEGY_BUFFER_MODE,
            "strike_step": strike_step,
            "lower_strike": lower_strike,
            "upper_strike": upper_strike,
            "strike_count": len(strikes),
            "contract_count": len(strikes) * 2,
            "strikes": strikes,
            "records": records,
            "resolved_count": enrichment.get("resolved", 0),
            "subscribed_count": enrichment.get("subscribed", 0),
            "or_written_count": enrichment.get("or_written", 0),
            "generated_at": _now_iso(),
        }
        _write_json(base_dir / "stage_1_summary.json", index_summary)

        return {
            "status": "ok",
            "stage": "stage_1",
            "ticker": ticker,
            "index_name": index_name,
            "index_instrument_key": index_key,
            "day": or_day,
            "or_levels": levels,
            "or_first_candle": first_candle,
            "or_candle_source": or_candle_source,
            "or_retry_attempts": or_retry_attempts,
            "atm_reference": round(atm_ref, 4),
            "atm_strike": atm_strike,
            "buffer_points": typical,
            "avg_buffer_points": avg_buffer,
            "buffer_mode": config.STRATEGY_BUFFER_MODE,
            "strike_step": strike_step,
            "lower_strike": lower_strike,
            "upper_strike": upper_strike,
            "strike_count": len(strikes),
            "contract_count": len(strikes) * 2,
            "strikes": strikes,
            "resolved_count": enrichment.get("resolved", 0),
            "subscribed_count": enrichment.get("subscribed", 0),
            "or_written_count": enrichment.get("or_written", 0),
            "base_dir": str(base_dir),
        }

    # ------------------------------------------------------------------ #
    # Registry helper                                                    #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _register_strike(
        *,
        key: str,
        ticker: str,
        strike: int,
        strike_type: str,
        trading_symbol: str,
        expiry: Any = None,
    ) -> None:
        try:
            instrument_registry.register_adhoc(
                key,
                trading_symbol=trading_symbol,
                underlying=ticker,
                strike_price=strike,
                strike_type=strike_type,
                expiry=expiry,
            )
        except Exception:
            logger.exception(
                "[strategy] register_adhoc failed for %s %s %s",
                ticker,
                strike,
                strike_type,
            )

    # ------------------------------------------------------------------ #
    # Upstox enrichment (history + last-day-cross + feed subscribe + OR) #
    # ------------------------------------------------------------------ #
    def _enrich_with_upstox(
        self,
        *,
        ticker: str,
        base_dir: Path,
        strikes: list[int],
        today_iso: str,
    ) -> dict[str, int]:
        """Enrich every strike folder with Upstox data.

        For each strike, this:
          1. resolves the real Upstox instrument_key,
          2. registers it (and its trading symbol / triple) in the registry,
          3. subscribes it to the live WebSocket feed (so the S1/S2
             trackers can read its LTP once the market opens),
          4. calculates the strike's OWN opening range from its own
             09:15 1-minute candle (via the existing REST fallback) and
             writes the result to `<strike folder>/opening_range.json`,
          5. fetches 30 days of history, computes the crosses, and writes
             `candles.json` / `ema_crosses.json` / `last_day_cross.json`.

        Steps (3) and (4) are the fix for the Oct 22 `no_activations`
        bug: without them the trackers saw None LTP and None level for
        every contract and Stage 4 reported zero placements.

        Returns a summary dict::

            {
              "resolved":   int,   # strikes whose real key was resolved
              "subscribed": int,   # strikes now subscribed to the live feed
              "or_written": int,   # strikes whose own OR was written
              "total":      int,   # total strikes considered
            }
        """
        token = token_service.get_access_token()
        if not token:
            logger.info(
                "[strategy] %s: no Upstox token; skipping history fetch "
                "(live-feed subscription + per-strike OR are also skipped)",
                ticker,
            )
            return {
                "resolved": 0,
                "subscribed": 0,
                "or_written": 0,
                "total": len(strikes) * 2,
            }

        work: list[tuple[int, str, Path]] = []
        for strike in strikes:
            for s_type in ("CE", "PE"):
                work.append((strike, s_type, base_dir / f"{strike}_{s_type}"))

        resolved = 0
        subscribed = 0
        or_written = 0
        workers = min(config.STRATEGY_MAX_WORKERS, max(1, len(work)))

        # Lazy import so this module stays importable even if the feed
        # service is being refactored — the try/except around the call
        # will log the failure rather than break Stage 1.
        try:
            from services.live_feed_service import live_feed_service
        except Exception:  # pragma: no cover
            live_feed_service = None
            logger.exception(
                "[strategy] %s: live_feed_service import failed — "
                "strikes will not be subscribed to the live feed",
                ticker,
            )

        def _task(
            strike: int,
            s_type: str,
            folder: Path,
        ) -> tuple[bool, bool, bool]:
            """Return (resolved, subscribed, or_written) for one strike."""
            try:
                key = self._resolve_upstox_key(
                    token=token,
                    ticker=ticker,
                    strike=strike,
                    strike_type=s_type,
                )
                if not key:
                    logger.info(
                        "[strategy] %s: could not resolve Upstox key "
                        "for strike=%s type=%s",
                        ticker, strike, s_type,
                    )
                    return False, False, False

                trading_symbol = f"{ticker} {strike} {s_type}"
                self._register_strike(
                    key=key,
                    ticker=ticker,
                    strike=strike,
                    strike_type=s_type,
                    trading_symbol=trading_symbol,
                    expiry=today_iso,
                )

                meta_path = folder / "strategy_meta.json"
                meta = _read_json(meta_path) or {}
                meta["upstox_instrument_key"] = key
                meta["resolved_at"] = _now_iso()
                _write_json(meta_path, meta)

                # ---- (3) Subscribe to the live feed ------------------ #
                sub_ok = False
                if live_feed_service is not None:
                    try:
                        if live_feed_service.is_subscribed(key):
                            sub_ok = True
                        else:
                            sub_ok = bool(
                                live_feed_service.subscribe_instrument(key)
                            )
                            if sub_ok:
                                logger.info(
                                    "[strategy] %s: subscribed to live feed "
                                    "(strike=%s type=%s key=%s)",
                                    ticker, strike, s_type, key,
                                )
                            else:
                                logger.warning(
                                    "[strategy] %s: live feed subscribe "
                                    "returned False (strike=%s type=%s key=%s)",
                                    ticker, strike, s_type, key,
                                )
                    except Exception:
                        logger.exception(
                            "[strategy] %s: live feed subscribe raised "
                            "(strike=%s type=%s key=%s)",
                            ticker, strike, s_type, key,
                        )

                # ---- (4) Calculate the strike's OWN OR and write it - #
                or_ok = False
                try:
                    or_result = opening_range_service.calculate_one(key)
                    if or_result.get("status") == "ok":
                        payload_for_strike = {
                            "instrument_key": key,
                            "underlying": ticker,
                            "strike_price": strike,
                            "strike_type": s_type,
                            "trading_symbol": trading_symbol,
                            "day": or_result.get("day"),
                            "first_candle": or_result.get("first_candle") or {},
                            "candle_source": or_result.get("candle_source"),
                            "levels": or_result.get("levels") or {},
                            "calculated_at": or_result.get("calculated_at"),
                            "source": "strategy_selection",
                            "generated_at": _now_iso(),
                        }
                        _write_json(
                            folder / "opening_range.json",
                            payload_for_strike,
                        )
                        or_ok = True
                        _levels = or_result.get("levels") or {}
                        logger.info(
                            "[strategy] %s: strike OR written "
                            "(strike=%s type=%s R2=%s S3=%s source=%s)",
                            ticker, strike, s_type,
                            _levels.get("R2"),
                            _levels.get("S3"),
                            or_result.get("candle_source"),
                        )
                    else:
                        logger.info(
                            "[strategy] %s: strike OR not available "
                            "(strike=%s type=%s reason=%s)",
                            ticker, strike, s_type,
                            or_result.get("reason"),
                        )
                except Exception:
                    logger.exception(
                        "[strategy] %s: strike OR calc failed "
                        "(strike=%s type=%s key=%s)",
                        ticker, strike, s_type, key,
                    )

                # ---- (5) History fetch + cross computation -------- #
                candles = self._fetch_history(token=token, instrument_key=key)

                _write_json(
                    folder / "candles.json",
                    {
                        "instrument_key": key,
                        "underlying": ticker,
                        "strike_price": strike,
                        "strike_type": s_type,
                        "trading_symbol": trading_symbol,
                        "day": today_iso,
                        "candles": candles,
                        "count": len(candles),
                        "fetched_at": _now_iso(),
                    },
                )
                crosses = _compute_crosses(candles)
                _write_json(
                    folder / "ema_crosses.json",
                    {
                        "instrument_key": key,
                        "underlying": ticker,
                        "strike_price": strike,
                        "strike_type": s_type,
                        "trading_symbol": trading_symbol,
                        "expiry": today_iso,
                        "day": today_iso,
                        "crosses": crosses,
                        "count": len(crosses),
                        "generated_at": _now_iso(),
                    },
                )

                last = _last_day_last_cross(
                    candles,
                    today_iso,
                    config.STRATEGY_ISOLATION_LOOKBACK_MINUTES,
                )
                _write_json(
                    folder / "last_day_cross.json",
                    (
                        last
                        if last is not None
                        else {
                            "type": None,
                            "timestamp": None,
                            "day": None,
                            "window_minutes": config.STRATEGY_ISOLATION_LOOKBACK_MINUTES,
                            "crosses_in_window": 0,
                            "generated_at": _now_iso(),
                        }
                    ),
                )
                return True, sub_ok, or_ok
            except Exception:
                logger.exception(
                    "[strategy] %s %d %s enrichment failed",
                    ticker,
                    strike,
                    s_type,
                )
                return False, False, False

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_task, s, t, f) for (s, t, f) in work]
            for fut in as_completed(futures):
                try:
                    r, s, o = fut.result()
                except Exception:
                    continue
                if r:
                    resolved += 1
                if s:
                    subscribed += 1
                if o:
                    or_written += 1

        logger.info(
            "[strategy] %s: enrichment complete — "
            "resolved=%d/%d subscribed=%d or_written=%d",
            ticker,
            resolved,
            len(work),
            subscribed,
            or_written,
        )
        return {
            "resolved": resolved,
            "subscribed": subscribed,
            "or_written": or_written,
            "total": len(work),
        }

    def _resolve_upstox_key(
        self,
        *,
        token: str,
        ticker: str,
        strike: int,
        strike_type: str,
    ) -> str | None:
        query = f"{ticker} {strike} {strike_type}"
        try:
            resp = requests.get(
                f"{_UPSTOX_BASE}/v2/instruments/search",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                params={
                    "query": query,
                    "exchanges": "NSE,BSE",
                    "segments": "FO",
                    "instrument_types": strike_type,
                    "records": 5,
                    "page_number": 1,
                },
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException:
            return None
        if resp.status_code >= 400:
            return None
        try:
            items = resp.json().get("data") or []
        except ValueError:
            return None

        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                sp = float(item.get("strike_price"))
            except (TypeError, ValueError):
                continue
            it = str(item.get("instrument_type") or "").upper()
            if int(sp) == int(strike) and it == strike_type.upper():
                key = item.get("instrument_key")
                if key:
                    return str(key)
        for item in items:
            key = item.get("instrument_key") if isinstance(item, dict) else None
            if key:
                return str(key)
        return None

    def _fetch_history(
        self,
        *,
        token: str,
        instrument_key: str,
    ) -> list[dict[str, Any]]:
        to_date = date.today()
        from_date = to_date - timedelta(days=config.STRATEGY_HISTORY_DAYS)
        url = (
            f"{_UPSTOX_BASE}/v3/historical-candle/{instrument_key}/minutes/1/"
            f"{to_date.isoformat()}/{from_date.isoformat()}"
        )
        try:
            resp = requests.get(
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException:
            return []
        if resp.status_code >= 400:
            return []
        try:
            body = resp.json()
        except ValueError:
            return []
        raw = (
            (body.get("data") or {}).get("candles")
            if isinstance(body.get("data"), dict)
            else body.get("candles")
        ) or []
        out: list[dict[str, Any]] = []
        for row in raw:
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                continue
            try:
                out.append(
                    {
                        "timestamp": str(row[0]),
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]) if len(row) >= 6 else 0.0,
                    }
                )
            except (TypeError, ValueError):
                continue
        return out


strategy_selection_service = StrategySelectionService()