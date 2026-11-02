# services/simulation_service.py
"""Post-isolation order simulation (t_feed_v1-market-engine-v3).

By default, replays today's order strategy end to end against the
``t_feed_v1-market-engine-v3`` specification:

    1. Fetch today's isolation record (bias-side instrument + T_iso).
    2. Resolve the opposite-leg instrument at the same strike
       (CE ↔ PE) from the registry.
    3. Fetch today's 1-min candles for the bias-side instrument and
       detect 15/3 pivots.
    4. Find the FIRST post-isolation SR level:
           pivot_time > T_iso  AND  confirmed_time > T_iso
    5. Apply the routing decision:
         • 09:30 backup reversal  → same isolated leg
             (post-isolation resistance, prior support already
             confirmed before T_iso, confirmation ≤ 09:30 IST)
         • post-isolation support → same isolated leg
         • post-isolation resistance (no reversal) → opposite leg
    6. Look up the traded instrument's own OR grid (first_candle.json
       → ``or_levels``) and classify the entry zone:
           P_entry > R2                    → SKIP
           P_entry < S2                    → milestones: OR Low / +50 pt
           S2 ≤ P_entry < R1               → R2, R3_Threshold, R3
           R1 ≤ P_entry ≤ R2 (Zone 2)      → R3_Threshold, R3
    7. If ``P_entry > R2``, mark the order SKIPPED and return the
       reason without replaying.
    8. Otherwise build the order (30 % stop-loss) and replay the day:
         • stop-loss hit              → STOP_HIT
         • Stage 1 milestone OR ≥50 pt → arm Stage 2
         • Stage 2 → exit at the NEXT 15/3 resistance confirmation
                     close on the traded instrument
         • end of session / mid-session without exit → END_OF_DAY
                                                       / OPEN
    9. Return routing + order + replay + verdict + notes as one
       payload.

The simulation is STATELESS and READ-ONLY. It performs no disk writes,
schedules no jobs, and sends no Telegram. It re-fetches intraday
candles from Upstox on every call, so the replay always reflects the
authoritative OHLC.

Never raises. Every expected failure mode lands in ``status``:

    ok             — replay completed (see ``verdict.outcome``)
    disabled       — SIMULATION_ENABLED is False
    no_isolation   — no isolated instrument recorded for today
    no_trigger     — no post-isolation 15/3 SR level confirmed yet
    no_or_levels   — traded instrument has no today-dated OR file
    no_candles     — either side's intraday candles unavailable
    no_opposite_leg — opposite leg cannot be resolved
    error          — unexpected exception
"""

from __future__ import annotations

import json
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


# =========================================================================== #
# Time helpers                                                                #
# =========================================================================== #
def _market_tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.MARKET_TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _now_market() -> datetime:
    return datetime.now(_market_tz())


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return _now_market().date().isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_market_tz())
    return dt.astimezone(_market_tz())


def _iso(dt: datetime) -> str:
    return dt.astimezone(_market_tz()).isoformat()


# =========================================================================== #
# Numeric helpers                                                             #
# =========================================================================== #
def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt(v: Any, fmt: str = ".4f") -> str:
    f = _safe_float(v)
    return format(f, fmt) if f is not None else "—"


# =========================================================================== #
# Data fetchers                                                               #
# =========================================================================== #
def _get_iso_record() -> dict | None:
    """Return today's isolation record (bias-side + T_iso), or None."""
    try:
        from services.level_isolated_instrument_service import (
            level_isolated_instrument_service,
        )
        status = level_isolated_instrument_service.last_status()
    except Exception:
        logger.exception("[simulation] isolation lookup failed")
        return None
    iso = (status or {}).get("isolated")
    return iso if isinstance(iso, dict) and iso else None


def _resolve_opposite_leg(iso: dict) -> dict | None:
    """Resolve the opposite-side option at the same strike/expiry."""
    try:
        from services.instrument_registry import instrument_registry
    except Exception:
        return None
    bias_key = str(iso.get("instrument_key") or "").strip()
    if not bias_key:
        return None
    bias_entry = instrument_registry.get(bias_key) or {}
    underlying = str(bias_entry.get("underlying") or "").strip()
    strike = bias_entry.get("strike_price")
    bias_type = str(
        bias_entry.get("strike_type") or iso.get("type") or ""
    ).upper()
    if not (underlying and strike is not None and bias_type in {"CE", "PE"}):
        return None
    opposite_type = "PE" if bias_type == "CE" else "CE"
    opp = instrument_registry.resolve(underlying, strike, opposite_type)
    if not opp:
        return None
    return {
        "instrument_key": opp.get("instrument_key"),
        "trading_symbol": opp.get("trading_symbol"),
        "underlying": opp.get("underlying"),
        "strike_price": opp.get("strike_price"),
        "strike_type": opposite_type,
        "expiry": opp.get("expiry"),
    }


def _fetch_candles(instrument_key: str) -> list[dict]:
    """Fetch today's intraday 1-min candles for one instrument."""
    try:
        from services.live_sr_calc_service import live_sr_calc_service
        return live_sr_calc_service.fetch_intraday_candles_for_entry(
            instrument_key,
        )
    except Exception:
        logger.exception(
            "[simulation] candle fetch failed for %s", instrument_key,
        )
        return []


def _read_or_levels(instrument_key: str) -> dict | None:
    """Read the traded instrument's own OR levels (today's file only)."""
    try:
        from services.instrument_registry import instrument_registry
        entry = instrument_registry.get(instrument_key)
    except Exception:
        entry = None
    if not entry:
        return None
    underlying = str(entry.get("underlying") or "").strip().lower()
    strike = entry.get("strike_price")
    strike_type = str(entry.get("strike_type") or "").upper()
    if not (underlying and strike is not None and strike_type):
        return None
    try:
        strike_int = int(float(strike))
    except (TypeError, ValueError):
        return None
    path = (
        Path(config.LIVE_DATA_DIR)
        / underlying
        / f"{strike_int}_{strike_type}"
        / "first_candle.json"
    )
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("[simulation] unreadable %s", path)
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("date") or "") != _today_iso():
        return None
    orl = payload.get("or_levels")
    return orl if isinstance(orl, dict) else None


# =========================================================================== #
# Pivot scan (15/3 strict)                                                    #
# =========================================================================== #
def _scan_pivots(
    candles: list[dict],
    left: int,
    right: int,
) -> list[dict]:
    n = len(candles)
    if n < left + right + 1:
        return []

    out: list[dict] = []
    for i in range(left, n - right):
        c = candles[i]
        try:
            high = float(c["high"])
            low = float(c["low"])
            pivot_time = c["time"]
        except (KeyError, TypeError, ValueError):
            continue

        try:
            is_res = all(
                float(candles[j]["high"]) < high
                for j in range(i - left, i)
            ) and all(
                float(candles[j]["high"]) < high
                for j in range(i + 1, i + right + 1)
            )
            is_sup = all(
                float(candles[j]["low"]) > low
                for j in range(i - left, i)
            ) and all(
                float(candles[j]["low"]) > low
                for j in range(i + 1, i + right + 1)
            )
        except (KeyError, TypeError, ValueError):
            continue

        conf_candle = candles[i + right]
        conf_time = conf_candle.get("time")
        conf_close = _safe_float(conf_candle.get("close"))

        if is_res:
            out.append({
                "level": "resistance",
                "price": high,
                "pivot_time": pivot_time,
                "confirmed_time": conf_time,
                "confirmed_close": conf_close,
                "pivot_index": i,
            })
        if is_sup:
            out.append({
                "level": "support",
                "price": low,
                "pivot_time": pivot_time,
                "confirmed_time": conf_time,
                "confirmed_close": conf_close,
                "pivot_index": i,
            })
    return out


# =========================================================================== #
# Trigger / routing                                                           #
# =========================================================================== #
def _find_trigger(
    pivots: list[dict],
    t_iso: datetime,
) -> dict | None:
    """First pivot whose pivot_time AND confirmed_time both exceed T_iso."""
    candidates: list[tuple[datetime, datetime, dict]] = []
    for p in pivots:
        pv_dt = _parse_iso(p.get("pivot_time"))
        cf_dt = _parse_iso(p.get("confirmed_time"))
        if pv_dt is None or cf_dt is None:
            continue
        if pv_dt <= t_iso or cf_dt <= t_iso:
            continue
        candidates.append((cf_dt, pv_dt, p))
    if not candidates:
        return None
    candidates.sort(key=lambda t: (t[0], t[1]))
    return candidates[0][2]


def _find_prior_support(
    pivots: list[dict],
    t_iso: datetime,
) -> dict | None:
    """Most-recently-confirmed support on or before T_iso."""
    candidates: list[tuple[datetime, dict]] = []
    for p in pivots:
        if str(p.get("level") or "").lower() != "support":
            continue
        cf_dt = _parse_iso(p.get("confirmed_time"))
        if cf_dt is None or cf_dt > t_iso:
            continue
        candidates.append((cf_dt, p))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def _decide_routing(
    trigger: dict,
    prior_support: dict | None,
    bias_type: str,
    cutoff_dt: datetime | None,
) -> dict:
    trigger_level = str(trigger.get("level") or "").lower()
    trigger_conf = _parse_iso(trigger.get("confirmed_time"))

    reversal_active = (
        cutoff_dt is not None
        and trigger_conf is not None
        and trigger_conf <= cutoff_dt
        and prior_support is not None
        and trigger_level == "resistance"
    )
    if reversal_active:
        return {
            "mode": "reversal_same_leg",
            "traded_side": bias_type,
            "reason": (
                "09:30 reversal — prior support "
                f"@{prior_support.get('confirmed_time')} + post-isolation "
                f"resistance @{trigger.get('confirmed_time')} ≤ cutoff"
            ),
        }
    if trigger_level == "support":
        return {
            "mode": "support_same_leg",
            "traded_side": bias_type,
            "reason": "post-isolation support → BUY the isolated instrument",
        }
    return {
        "mode": "resistance_opposite_leg",
        "traded_side": "PE" if bias_type == "CE" else "CE",
        "reason": (
            "post-isolation resistance → BUY the opposite leg "
            "(no prior support, or confirmation after 09:30 IST)"
        ),
    }


# =========================================================================== #
# Zone classification                                                         #
# =========================================================================== #
def _classify_zone(entry_price: float, or_levels: dict) -> dict:
    R1 = _safe_float(or_levels.get("R1"))
    R2 = _safe_float(or_levels.get("R2"))
    R3 = _safe_float(or_levels.get("R3"))
    R3T = _safe_float(or_levels.get("R3_Threshold"))
    L = _safe_float(or_levels.get("L"))
    S1 = _safe_float(or_levels.get("S1"))
    S2 = _safe_float(or_levels.get("S2"))
    if None in (R1, R2, R3, R3T, L, S1, S2):
        return {
            "zone": "UNCLASSIFIABLE",
            "milestones": [],
            "skip": True,
            "reason": "incomplete OR levels",
        }
    if entry_price > R2:
        return {
            "zone": "ABOVE_R2",
            "milestones": [],
            "skip": True,
            "reason": (
                f"entry {entry_price:.4f} > R2 {R2:.4f} — "
                f"no valid logic present above R2 level"
            ),
        }
    if entry_price < S2:
        return {
            "zone": "BELOW_S2",
            "milestones": [{"name": "OR_LOW", "price": L}],
            "skip": False,
        }
    if entry_price < R1:
        return {
            "zone": "BELOW_R1",
            "milestones": [
                {"name": "R2", "price": R2},
                {"name": "R3_Threshold", "price": R3T},
                {"name": "R3", "price": R3},
            ],
            "skip": False,
        }
    return {
        "zone": "ZONE_2",
        "milestones": [
            {"name": "R3_Threshold", "price": R3T},
            {"name": "R3", "price": R3},
        ],
        "skip": False,
    }


# =========================================================================== #
# Replay                                                                      #
# =========================================================================== #
def _replay(
    traded_candles: list[dict],
    entry_time_iso: str,
    entry_price: float,
    stop_price: float,
    milestones: list[dict],
    resistance_exits: dict[str, float],
    gain_threshold: float,
) -> dict:
    entry_dt = _parse_iso(entry_time_iso)
    if entry_dt is None:
        return {"status": "invalid_entry_time"}

    max_high = entry_price
    min_low = entry_price
    stage2_dt: datetime | None = None
    stage2_reason: str | None = None
    exit_reason: str | None = None
    exit_price: float | None = None
    exit_time: str | None = None
    scanned = 0

    for c in traded_candles:
        c_dt = _parse_iso(c.get("time"))
        if c_dt is None or c_dt <= entry_dt:
            continue
        scanned += 1
        hi = _safe_float(c.get("high"))
        lo = _safe_float(c.get("low"))
        if hi is None or lo is None:
            continue
        max_high = max(max_high, hi)
        min_low = min(min_low, lo)

        # 1. Stop-loss hit (highest priority)
        if lo <= stop_price:
            exit_reason = "STOP_HIT"
            exit_price = stop_price
            exit_time = str(c.get("time"))
            break

        # 2. Stage 2 arming (milestone touch OR ≥ gain_threshold)
        if stage2_dt is None:
            hit = None
            for m in milestones:
                m_price = _safe_float(m.get("price"))
                if m_price is not None and hi >= m_price:
                    hit = f"MILESTONE_{m.get('name')}"
                    break
            if hit is None:
                gain = hi - entry_price
                if gain >= gain_threshold:
                    hit = f"GAIN_{gain:.2f}PT"
            if hit is not None:
                stage2_dt = c_dt
                stage2_reason = hit

        # 3. Stage 2 exit at the NEXT 15/3 resistance confirmation close
        if stage2_dt is not None:
            for conf_iso, conf_close in resistance_exits.items():
                conf_dt = _parse_iso(conf_iso)
                if conf_dt is None or conf_dt <= stage2_dt:
                    continue
                if conf_dt == c_dt:
                    exit_reason = "STAGE_2_RESISTANCE_EXIT"
                    exit_price = conf_close
                    exit_time = conf_iso
                    break
            if exit_reason:
                break

    # Unresolved → last available close (OPEN if before EOD else END_OF_DAY)
    if exit_price is None:
        last_close: float | None = None
        last_time: str | None = None
        for c in reversed(traded_candles):
            c_dt = _parse_iso(c.get("time"))
            if c_dt is None or c_dt <= entry_dt:
                continue
            lc = _safe_float(c.get("close"))
            if lc is not None:
                last_close = lc
                last_time = str(c.get("time"))
                break
        if last_close is None:
            exit_price = entry_price
            exit_reason = "UNRESOLVED_NO_DATA"
        else:
            exit_price = last_close
            exit_time = last_time
            eod = dtime(
                int(getattr(config, "DUMMY_ORDER_EOD_HOUR", 15)),
                int(getattr(config, "DUMMY_ORDER_EOD_MINUTE", 35)),
            )
            exit_reason = (
                "END_OF_DAY"
                if _now_market().time() >= eod
                else "OPEN"
            )

    pnl_pct = (
        (exit_price - entry_price) / entry_price if entry_price > 0 else 0.0
    )
    profit_pts = exit_price - entry_price
    stop_pct = (
        (entry_price - stop_price) / entry_price if entry_price > 0 else 0.0
    )
    r_mult = pnl_pct / stop_pct if stop_pct > 0 else None

    return {
        "status": "replayed",
        "candles_scanned": scanned,
        "exit_reason": exit_reason,
        "exit_time": exit_time,
        "exit_price": round(exit_price, 4),
        "pnl_pct": round(pnl_pct, 6),
        "profit_points": round(profit_pts, 4),
        "r_multiple": round(r_mult, 4) if r_mult is not None else None,
        "max_high": round(max_high, 4),
        "min_low": round(min_low, 4),
        "mfe": round(max_high - entry_price, 4),
        "mae": round(min_low - entry_price, 4),
        "stage_2_triggered_at": _iso(stage2_dt) if stage2_dt else None,
        "stage_2_trigger_reason": stage2_reason,
    }


# =========================================================================== #
# Service                                                                     #
# =========================================================================== #
class SimulationService:
    def run(self) -> dict[str, Any]:
        if not bool(getattr(config, "SIMULATION_ENABLED", True)):
            return {
                "status": "disabled",
                "reason": "SIMULATION_ENABLED is False",
                "generated_at": _now_utc_iso(),
            }

        # ---- 1. Isolation record ---------------------------------- #
        iso = _get_iso_record()
        if not iso:
            return {
                "status": "no_isolation",
                "detail": "no isolated instrument recorded for today",
                "generated_at": _now_utc_iso(),
            }

        bias_key = str(iso.get("instrument_key") or "").strip()
        bias_type = str(iso.get("type") or "").upper()
        t_iso_str = iso.get("touched_at") or iso.get("isolated_at")
        t_iso = _parse_iso(t_iso_str)
        if (
            not bias_key
            or bias_type not in {"CE", "PE"}
            or t_iso is None
        ):
            return {
                "status": "no_isolation",
                "detail": "isolation record is incomplete",
                "isolated_key": bias_key,
                "generated_at": _now_utc_iso(),
            }

        logger.info(
            "[simulation] run — isolated=%s bias=%s t_iso=%s",
            bias_key, bias_type, t_iso_str,
        )

        # ---- 2. Opposite leg -------------------------------------- #
        opposite = _resolve_opposite_leg(iso)

        # ---- 3. Bias-side candles + 15/3 pivots ------------------- #
        left = int(getattr(config, "LIVE_SR_LEFT_BARS", 15))
        right = int(getattr(config, "LIVE_SR_RIGHT_BARS", 3))
        bias_candles = _fetch_candles(bias_key)
        if not bias_candles:
            return {
                "status": "no_candles",
                "detail": f"no intraday candles for {bias_key}",
                "isolated_key": bias_key,
                "generated_at": _now_utc_iso(),
            }
        pivots = _scan_pivots(bias_candles, left, right)

        # ---- 4. Trigger ------------------------------------------- #
        trigger = _find_trigger(pivots, t_iso)
        if trigger is None:
            return {
                "status": "no_trigger",
                "detail": (
                    "no post-isolation 15/3 SR level confirmed yet"
                ),
                "isolated_key": bias_key,
                "t_iso": t_iso_str,
                "pivots_total": len(pivots),
                "generated_at": _now_utc_iso(),
            }

        # ---- 5. Prior support + routing --------------------------- #
        prior_support = _find_prior_support(pivots, t_iso)
        cutoff_h = int(getattr(
            config, "STRATEGY_ORDER_REVERSAL_CUTOFF_HOUR", 9,
        ))
        cutoff_m = int(getattr(
            config, "STRATEGY_ORDER_REVERSAL_CUTOFF_MINUTE", 30,
        ))
        cutoff_dt = t_iso.replace(
            hour=cutoff_h, minute=cutoff_m, second=0, microsecond=0,
        )
        routing = _decide_routing(
            trigger, prior_support, bias_type, cutoff_dt,
        )

        # ---- 6. Resolve the traded instrument --------------------- #
        if routing["traded_side"] == bias_type:
            traded_key = bias_key
            traded_symbol = iso.get("trading_symbol")
            traded_candles = bias_candles
            traded_pivots = pivots
        else:
            if not opposite:
                return {
                    "status": "no_opposite_leg",
                    "detail": (
                        f"cannot resolve the opposite leg of {bias_key}"
                    ),
                    "routing": routing,
                    "generated_at": _now_utc_iso(),
                }
            traded_key = str(opposite.get("instrument_key") or "")
            traded_symbol = opposite.get("trading_symbol")
            traded_candles = _fetch_candles(traded_key)
            traded_pivots = _scan_pivots(traded_candles, left, right)

        if not traded_candles:
            return {
                "status": "no_candles",
                "detail": (
                    f"no intraday candles for traded instrument "
                    f"{traded_key}"
                ),
                "traded_instrument_key": traded_key,
                "generated_at": _now_utc_iso(),
            }

        # ---- 7. Traded-instrument OR levels ----------------------- #
        or_levels = _read_or_levels(traded_key)
        if not or_levels:
            return {
                "status": "no_or_levels",
                "detail": (
                    f"first_candle.json (today) missing for {traded_key}"
                ),
                "traded_instrument_key": traded_key,
                "routing": routing,
                "generated_at": _now_utc_iso(),
            }

        # ---- 8. Entry price = traded-instrument close at trigger -- #
        entry_time_iso = trigger.get("confirmed_time")
        entry_dt = _parse_iso(entry_time_iso)
        if entry_dt is None:
            return {
                "status": "no_candles",
                "detail": "trigger confirmed_time is unparseable",
                "generated_at": _now_utc_iso(),
            }
        entry_minute = entry_dt.replace(second=0, microsecond=0)
        entry_price: float | None = None
        entry_price_source: str | None = None
        for c in traded_candles:
            c_dt = _parse_iso(c.get("time"))
            if c_dt is None:
                continue
            if c_dt.replace(second=0, microsecond=0) == entry_minute:
                entry_price = _safe_float(c.get("close"))
                if entry_price is not None:
                    entry_price_source = "candle_close_at_trigger"
                break
        if entry_price is None or entry_price <= 0:
            return {
                "status": "no_candles",
                "detail": (
                    "no candle on the traded instrument at trigger "
                    "confirmed_time"
                ),
                "traded_instrument_key": traded_key,
                "trigger_confirmed_time": entry_time_iso,
                "generated_at": _now_utc_iso(),
            }

        # ---- 9. Zone classification ------------------------------- #
        zone = _classify_zone(entry_price, or_levels)

        # ---- 10. Build the base response (shared by skip + replay) #
        base: dict[str, Any] = {
            "day": _today_iso(),
            "isolated": {
                "instrument_key": bias_key,
                "trading_symbol": iso.get("trading_symbol"),
                "bias_type": bias_type,
                "t_iso": t_iso_str,
                "trigger_side": iso.get("trigger_side"),
                "trigger_level_value": iso.get("trigger_level_value"),
                "execution_path": iso.get("execution_path"),
            },
            "opposite": opposite,
            "trigger": {
                "level": trigger.get("level"),
                "price": trigger.get("price"),
                "pivot_time": trigger.get("pivot_time"),
                "confirmed_time": trigger.get("confirmed_time"),
                "confirmed_close": trigger.get("confirmed_close"),
            },
            "prior_support": (
                {
                    "price": prior_support.get("price"),
                    "confirmed_time": prior_support.get("confirmed_time"),
                }
                if prior_support else None
            ),
            "routing": routing,
            "traded_instrument": {
                "instrument_key": traded_key,
                "trading_symbol": traded_symbol,
                "side": "BUY",
            },
            "or_levels": or_levels,
            "regime": zone,
        }

        # ---- 11. Regime skip? ------------------------------------- #
        if zone.get("skip"):
            base["status"] = "ok"
            base["order"] = {
                "status": "SKIPPED",
                "entry_time": entry_time_iso,
                "entry_price": round(entry_price, 4),
                "entry_price_source": entry_price_source,
                "stop_price": None,
                "stop_pct": None,
                "skip_reason": zone.get("reason"),
            }
            base["replay"] = None
            base["verdict"] = {
                "outcome": "SKIPPED",
                "reason": zone.get("reason"),
            }
            base["notes"] = [
                f"Routing: {routing['mode']} — {routing['reason']}",
                f"Trigger: {str(trigger.get('level')).upper()} "
                f"@{_fmt(trigger.get('price'))} "
                f"confirmed {entry_time_iso}",
                f"Entry on traded instrument ₹{entry_price:.4f} "
                f"is above R2 — order rejected.",
            ]
            base["candles_total"] = len(traded_candles)
            base["generated_at"] = _now_utc_iso()
            logger.info(
                "[simulation] SKIPPED — routing=%s zone=%s reason=%s",
                routing["mode"], zone.get("zone"), zone.get("reason"),
            )
            return base

        # ---- 12. Stop-loss + resistance-exit map ------------------ #
        stop_pct_cfg = float(
            getattr(config, "STRATEGY_ORDER_STOP_LOSS_PCT", 0.30),
        )
        stop_price = round(entry_price * (1.0 - stop_pct_cfg), 4)

        resistance_exits: dict[str, float] = {}
        for p in traded_pivots:
            if str(p.get("level") or "").lower() != "resistance":
                continue
            conf_iso = p.get("confirmed_time")
            conf_close = _safe_float(p.get("confirmed_close"))
            if conf_iso and conf_close is not None:
                resistance_exits[str(conf_iso)] = conf_close

        # ---- 13. Replay ------------------------------------------- #
        gain_threshold = float(
            getattr(config, "STRATEGY_ORDER_MILESTONE_GAIN_POINTS", 50.0),
        )
        replay = _replay(
            traded_candles=traded_candles,
            entry_time_iso=entry_time_iso,
            entry_price=entry_price,
            stop_price=stop_price,
            milestones=zone.get("milestones") or [],
            resistance_exits=resistance_exits,
            gain_threshold=gain_threshold,
        )

        # ---- 14. Assemble order + verdict ------------------------- #
        exit_reason = str(replay.get("exit_reason") or "")
        pnl = _safe_float(replay.get("pnl_pct")) or 0.0

        base["status"] = "ok"
        base["order"] = {
            "status": exit_reason or "OPEN",
            "entry_time": entry_time_iso,
            "entry_price": round(entry_price, 4),
            "entry_price_source": entry_price_source,
            "stop_price": stop_price,
            "stop_pct": stop_pct_cfg,
        }
        base["replay"] = replay

        if exit_reason == "STOP_HIT":
            outcome = "LOSS"
        elif exit_reason == "STAGE_2_RESISTANCE_EXIT":
            outcome = "WIN" if pnl >= 0 else "LOSS"
        elif exit_reason in {"END_OF_DAY", "OPEN"}:
            if pnl > 0:
                outcome = "WIN"
            elif pnl < 0:
                outcome = "LOSS"
            else:
                outcome = "UNRESOLVED"
        else:
            outcome = "UNRESOLVED"

        base["verdict"] = {
            "outcome": outcome,
            "reason": (
                f"{exit_reason or 'OPEN'} @ "
                f"₹{_fmt(replay.get('exit_price'))} "
                f"({(pnl or 0) * 100:+.2f}%)"
            ),
        }

        # ---- 15. Notes -------------------------------------------- #
        notes: list[str] = []
        notes.append(
            f"Routing: {routing['mode']} — {routing['reason']}"
        )
        notes.append(
            f"Trigger: {str(trigger.get('level')).upper()} "
            f"@{_fmt(trigger.get('price'))} "
            f"confirmed {entry_time_iso}"
        )
        if prior_support:
            notes.append(
                f"Prior support: ₹{_fmt(prior_support.get('price'))} "
                f"@{prior_support.get('confirmed_time')}"
            )
        notes.append(
            f"Entry ₹{entry_price:.4f} on {traded_symbol or traded_key} "
            f"({entry_price_source})"
        )
        notes.append(
            f"Zone {zone.get('zone')} — milestones: "
            + ", ".join(
                f"{m['name']}={_fmt(m['price'])}"
                for m in (zone.get("milestones") or [])
            )
        )
        notes.append(
            f"Stop-loss ₹{stop_price:.4f} "
            f"({-stop_pct_cfg * 100:.1f}%)"
        )
        if replay.get("stage_2_triggered_at"):
            notes.append(
                f"Stage 2 armed @ {replay['stage_2_triggered_at']} "
                f"({replay.get('stage_2_trigger_reason')})"
            )
        notes.append(
            f"Exit: {replay.get('exit_reason')} @ "
            f"₹{_fmt(replay.get('exit_price'))} "
            f"({(pnl or 0) * 100:+.2f}%)"
        )
        base["notes"] = notes
        base["candles_total"] = len(traded_candles)
        base["generated_at"] = _now_utc_iso()

        logger.info(
            "[simulation] result — routing=%s zone=%s outcome=%s "
            "exit=%s pnl=%+.2f%%",
            routing["mode"], zone.get("zone"), outcome,
            exit_reason, pnl * 100.0,
        )
        return base


# =========================================================================== #
# Singleton                                                                   #
# =========================================================================== #
simulation_service = SimulationService()