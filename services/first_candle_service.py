# File: services/first_candle_service.py
"""First-candle OR + EMA-cross detection service.

Runs at 09:16:10 IST Mon–Fri (after the 09:15 first candle closes).

After the per-instrument OR + EMA-cross pass completes, this service
also triggers ``level_instrument_filter_service.run(...)`` which
converts the index OR levels into the CE/PE option instruments that
the live SR engine should track. That filter writes
``data/index/<nifty|sensex>/level_instruments.json`` and registers the
filtered instruments with ``live_sr_levels_service``.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger
from services.instrument_registry import instrument_registry
from services.market_holiday_service import market_holiday_service

logger = get_logger(__file__)

_MARKET_TZ = ZoneInfo("Asia/Kolkata")
_MAX_WORKERS = max(1, int(getattr(config, "FIRST_CANDLE_WORKERS", 8)))

_MARKET_OPEN = dtime(9, 15)
_MARKET_CLOSE = dtime(15, 30)
_VALID_WEEKDAYS = {0, 1, 2, 3, 4}  # Mon-Fri

# --------------------------------------------------------------------------- #
# Market-gate helpers                                                         #
# --------------------------------------------------------------------------- #
def _now_market() -> datetime:
    return datetime.now(_MARKET_TZ)

def _is_weekend(dt: datetime) -> bool:
    return dt.weekday() not in _VALID_WEEKDAYS

def _is_holiday_today() -> tuple[bool, str | None]:
    try:
        payload = market_holiday_service.get_today()
        if str(payload.get("status") or "").lower() == "holiday":
            holidays = payload.get("holidays") or []
            desc = None
            if holidays and isinstance(holidays[0], dict):
                desc = holidays[0].get("description") or None
            return (True, desc)
    except Exception:
        logger.exception("[first-candle] holiday gate raised; assuming not holiday")
    return (False, None)

def _next_market_open(after: datetime) -> datetime:
    candidate = after.replace(hour=9, minute=15, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    for _ in range(14):
        if candidate.weekday() in _VALID_WEEKDAYS:
            try:
                date_str = candidate.date().isoformat()
                payload = market_holiday_service.get_holiday(date_str)
                is_hol = str(payload.get("status") or "").lower() == "holiday"
            except Exception:
                is_hol = False
            if not is_hol:
                return candidate
        candidate += timedelta(days=1)
    return candidate

def _format_countdown(delta: timedelta) -> str:
    total_sec = max(0, int(delta.total_seconds()))
    days = total_sec // 86400
    hours = (total_sec % 86400) // 3600
    mins = (total_sec % 3600) // 60
    secs = total_sec % 60
    hms = f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{days}d {hms}" if days > 0 else hms

def _market_gate_check() -> dict[str, Any]:
    now = _now_market()

    if _is_weekend(now):
        nxt = _next_market_open(now)
        return {
            "allow": False,
            "skip_reason": "weekend",
            "skip_message": f"Non-trading day ({now.strftime('%A')})",
            "next_open": nxt,
            "countdown_str": _format_countdown(nxt - now),
            "countdown_label": nxt.strftime("%a %d %b 09:15 IST"),
        }

    is_hol, desc = _is_holiday_today()
    if is_hol:
        nxt = _next_market_open(now)
        return {
            "allow": False,
            "skip_reason": "holiday",
            "skip_message": f"Market holiday: {desc or 'unknown'}",
            "next_open": nxt,
            "countdown_str": _format_countdown(nxt - now),
            "countdown_label": nxt.strftime("%a %d %b 09:15 IST"),
        }

    now_t = now.time()
    if now_t < _MARKET_OPEN:
        today_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        return {
            "allow": False,
            "skip_reason": "pre_open",
            "skip_message": "Before market open (09:15 IST)",
            "next_open": today_open,
            "countdown_str": _format_countdown(today_open - now),
            "countdown_label": today_open.strftime("%a %d %b 09:15 IST"),
        }

    if now_t > _MARKET_CLOSE:
        nxt = _next_market_open(now)
        return {
            "allow": False,
            "skip_reason": "post_close",
            "skip_message": "After market close (15:30 IST)",
            "next_open": nxt,
            "countdown_str": _format_countdown(nxt - now),
            "countdown_label": nxt.strftime("%a %d %b 09:15 IST"),
        }

    return {
        "allow": True,
        "skip_reason": None,
        "skip_message": None,
        "next_open": None,
        "countdown_str": None,
        "countdown_label": None,
    }

# --------------------------------------------------------------------------- #
# Filesystem helpers                                                          #
# --------------------------------------------------------------------------- #
def _safe_folder(name: Any) -> str:
    text = str(name or "unknown").strip()
    for ch in ("|", "/", "\\", ":", "*", "?", '"', "<", ">", " "):
        text = text.replace(ch, "_")
    return text.strip("._") or "unknown"

def _folder_for_entry(entry: dict[str, Any]) -> Path:
    underlying = str(entry.get("underlying") or "").strip().lower()
    strike = entry.get("strike_price")
    strike_type = str(entry.get("strike_type") or "").upper()
    is_index = bool(entry.get("is_index")) or strike is None

    if is_index:
        idx_name = underlying or _safe_folder(entry.get("instrument_key"))
        return Path(config.LIVE_DATA_DIR) / "index" / idx_name

    if underlying and strike is not None and strike_type:
        try:
            strike_int = int(float(strike))
        except (TypeError, ValueError):
            strike_int = str(strike)
        return (
            Path(config.LIVE_DATA_DIR)
            / underlying
            / f"{strike_int}_{strike_type}"
        )

    return (
        Path(config.LIVE_DATA_DIR)
        / "index"
        / _safe_folder(entry.get("instrument_key"))
    )

# --------------------------------------------------------------------------- #
# Upstox intraday fetch                                                       #
# --------------------------------------------------------------------------- #
def _load_sdk():
    try:
        import upstox_client  # type: ignore
        return upstox_client
    except ImportError:
        return None

def _coerce_dict(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    to_dict = getattr(raw, "to_dict", None)
    if callable(to_dict):
        try:
            d = to_dict()
        except Exception:
            d = None
        if isinstance(d, dict):
            return d
    data_attr = getattr(raw, "data", None)
    if isinstance(data_attr, dict):
        return data_attr
    return None

def _fetch_first_candle_upstox(instrument_key: str) -> dict[str, Any] | None:
    sdk = _load_sdk()
    if sdk is None:
        logger.error("upstox_client not installed")
        return None

    try:
        cfg = sdk.Configuration()
        api = sdk.HistoryV3Api(sdk.ApiClient(cfg))
        response = api.get_intra_day_candle_data(
            instrument_key, "minutes", "1",
        )
    except Exception as exc:
        logger.warning(
            "[first-candle] Upstox fetch failed for %s: %s: %s",
            instrument_key, type(exc).__name__, exc,
        )
        return None

    body = _coerce_dict(response)
    if body is None:
        return None

    candles = (body.get("data") or {}).get("candles") or []
    if not isinstance(candles, list) or not candles:
        return None

    for raw in candles:
        if not isinstance(raw, list) or len(raw) < 5:
            continue
        try:
            ts_str = str(raw[0])
            dt = datetime.fromisoformat(ts_str)
            if dt.hour != 9 or dt.minute != 15:
                continue
            return {
                "time_iso": ts_str,
                "open": float(raw[1]),
                "high": float(raw[2]),
                "low":  float(raw[3]),
                "close": float(raw[4]),
            }
        except Exception:
            continue
    return None

# --------------------------------------------------------------------------- #
# OR level computation                                                        #
# --------------------------------------------------------------------------- #
def _compute_or_levels(fc: dict[str, Any]) -> dict[str, float]:
    H = float(fc["high"])
    L = float(fc["low"])
    A = (H + L) / 2.0

    D_H = abs(H - A)
    D_L = abs(A - L)

    R1 = A + D_H / 2.0
    S1 = A - D_L / 2.0

    R2 = H + D_H
    S2 = L - D_L

    R3 = R2 + D_H
    S3 = S2 - D_L

    return {
        "H": H, "L": L, "A": A,
        "D_H": D_H, "D_L": D_L,
        "R1": R1, "R2": R2, "R3": R3,
        "S1": S1, "S2": S2, "S3": S3,
        "R3_Threshold": (R2 + R3) / 2.0,
        "S3_Threshold": (S2 + S3) / 2.0,
    }

# --------------------------------------------------------------------------- #
# EMA-cross detection                                                         #
# --------------------------------------------------------------------------- #
def _read_historical_crosses(entry: dict[str, Any]) -> dict[str, Any] | None:
    folder = _folder_for_entry(entry)
    p = folder / "ema_crosses.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[first-candle] cannot parse %s", p)
        return None

def _detect_new_ema_cross(
    entry: dict[str, Any],
    historical: dict[str, Any] | None,
    fc: dict[str, Any],
) -> dict[str, Any]:
    if not historical:
        return {"detected": False, "type": None,
                "reason": "no_historical_crosses", "compared_against": None}

    crosses = historical.get("crosses") or []
    if not crosses:
        return {"detected": False, "type": None,
                "reason": "empty_crosses_list", "compared_against": None}

    last = crosses[-1]
    if not isinstance(last, dict):
        return {"detected": False, "type": None,
                "reason": "malformed_last_cross", "compared_against": None}

    last_ts = str(last.get("timestamp") or "")
    last_type = str(last.get("type") or "").lower()
    try:
        last_fast = float(last.get("fast_ema") or 0.0)
        last_slow = float(last.get("slow_ema") or 0.0)
    except (TypeError, ValueError):
        last_fast = 0.0
        last_slow = 0.0

    try:
        fc_close = float(fc["close"])
        fc_open = float(fc["open"])
    except (TypeError, ValueError):
        fc_close = 0.0
        fc_open = 0.0

    new_type: str | None = None
    if last_type == "bullish" and last_slow > 0 and fc_close < last_slow:
        new_type = "bearish"
    elif last_type == "bearish" and last_slow > 0 and fc_close > last_slow:
        new_type = "bullish"

    return {
        "detected": new_type is not None,
        "type": new_type,
        "reason": "reverse_cross_against_last_slow_ema",
        "last_cross_type": last_type,
        "last_cross_fast_ema": last_fast,
        "last_cross_slow_ema": last_slow,
        "first_candle_open": fc_open,
        "first_candle_close": fc_close,
        "compared_against": last_ts or None,
    }

# --------------------------------------------------------------------------- #
# Public service                                                              #
# --------------------------------------------------------------------------- #
class FirstCandleService:
    """Runs the OR + EMA-cross task, then triggers the level filter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_run: dict[str, Any] = {}

    def _one(self, entry: dict[str, Any]) -> tuple[str, dict[str, Any] | None, str | None]:
        key = str(entry.get("instrument_key") or "")
        if not key:
            return ("", None, "missing_instrument_key")
        try:
            fc = _fetch_first_candle_upstox(key)
            if fc is None:
                return (key, None, "no_first_candle")
            levels = _compute_or_levels(fc)
            historical = _read_historical_crosses(entry)
            ema = _detect_new_ema_cross(entry, historical, fc)
            payload = {
                "instrument_key": key,
                "trading_symbol": entry.get("trading_symbol"),
                "underlying": entry.get("underlying"),
                "strike_price": entry.get("strike_price"),
                "strike_type": entry.get("strike_type"),
                "date": datetime.now(_MARKET_TZ).date().isoformat(),
                "first_candle": fc,
                "or_levels": levels,
                "ema_cross_at_first_candle": ema,
                "computed_at": datetime.now(_MARKET_TZ).isoformat(),
            }
            folder = _folder_for_entry(entry)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "first_candle.json").write_text(
                json.dumps(payload, indent=2, default=str),
                encoding="utf-8",
            )
            return (key, payload, None)
        except Exception as exc:
            return (key, None, f"{type(exc).__name__}: {exc}")

    def run(self, reason: str = "scheduled") -> dict[str, Any]:
        gate = _market_gate_check()
        if not gate["allow"]:
            summary = {
                "reason": reason,
                "skipped": True,
                "skip_reason": gate["skip_reason"],
                "skip_message": gate["skip_message"],
                "next_open": gate["next_open"].isoformat() if gate["next_open"] else None,
                "countdown_str": gate["countdown_str"],
                "countdown_label": gate["countdown_label"],
                "ran_at": _now_market().isoformat(),
                "total": 0, "computed": 0, "errors": 0,
                "crosses_detected": 0, "elapsed_seconds": 0.0,
            }
            with self._lock:
                self._last_run = summary
            logger.info(
                "[first-candle] SKIPPED reason=%s (%s) — next open in %s (%s)",
                gate["skip_reason"], gate["skip_message"],
                gate["countdown_str"], gate["countdown_label"],
            )
            return summary

        entries = list(instrument_registry.all())
        total = len(entries)
        logger.info(
            "[first-candle] START reason=%s instruments=%d workers=%d",
            reason, total, _MAX_WORKERS,
        )

        results: dict[str, Any] = {}
        errors: list[str] = []
        skipped: list[str] = []
        crosses_detected: list[dict[str, Any]] = []

        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futures = [pool.submit(self._one, e) for e in entries]
            for fut in as_completed(futures):
                key, payload, err = fut.result()
                if err:
                    if err == "no_first_candle":
                        skipped.append(key)
                    else:
                        errors.append(f"{key}: {err}")
                    continue
                if payload is not None:
                    results[key] = payload
                    ema = payload.get("ema_cross_at_first_candle") or {}
                    if ema.get("detected"):
                        crosses_detected.append({
                            "instrument_key": key,
                            "trading_symbol": payload.get("trading_symbol"),
                            "type": ema.get("type"),
                        })

        elapsed = round(time.monotonic() - t0, 2)
        summary = {
            "reason": reason,
            "skipped": False,
            "skip_reason": None,
            "skip_message": None,
            "next_open": None,
            "countdown_str": None,
            "countdown_label": None,
            "total": total,
            "computed": len(results),
            "instrument_skipped": len(skipped),
            "errors": len(errors),
            "crosses_detected": len(crosses_detected),
            "elapsed_seconds": elapsed,
            "crosses_list": crosses_detected[:20],
            "error_list": errors[:10],
            "skipped_list": skipped[:10],
            "ran_at": _now_market().isoformat(),
        }
        with self._lock:
            self._last_run = summary
        logger.info(
            "[first-candle] COMPLETE total=%d computed=%d skipped=%d "
            "errors=%d crosses=%d elapsed=%ss",
            total, len(results), len(skipped),
            len(errors), len(crosses_detected), elapsed,
        )

        # ---- Level-instrument filter -------------------------------- #
        # Convert the freshly written OR levels into the CE/PE
        # instruments that the live SR engine should track.
        try:
            from services.level_instrument_filter_service import (
                level_instrument_filter_service,
            )
            filter_result = level_instrument_filter_service.run(
                reason=f"first_candle:{reason}"
            )
            summary["level_filter"] = {
                "status": filter_result.get("status"),
                "indexes": {
                    k: {
                        "status": v.get("status"),
                        "ce_count": v.get("ce_count"),
                        "pe_count": v.get("pe_count"),
                    }
                    for k, v in (filter_result.get("indexes") or {}).items()
                },
            }
            logger.info(
                "[first-candle] level filter: %s",
                summary["level_filter"],
            )
        except Exception as exc:
            logger.exception("[first-candle] level filter failed")
            summary["level_filter"] = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }

        return summary

    def last_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._last_run)

first_candle_service = FirstCandleService()